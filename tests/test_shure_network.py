"""Driver + simulator tests for shure_network (Shure DCS over TCP 2202).

No Shure hardware on hand, so correctness is proven as a **dual-proof round
trip** wiring the real driver to the real simulator over an in-memory
transport that mimics the real delimiter (``>``) framing.

Covers the v2.0.0 first-class child-entity conversion (was YAML v1.6.0):
  - one ``channel`` child type, config-sized roster (no enumeration query),
    labelled from the device's own CHAN_NAME reports;
  - per-channel mute / gain / name / meter as child props; gain is a real dB
    value translated to and from the device's 0-1400 wire scale; a SAMPLE
    frame fans out positionally into channel meter props;
  - commands take child_id params; device mute / preset / LED / firmware stay
    flat; the LED-brightness bound is widened to the documented 0-5;
  - the v1.6.0 liveness probe (< GET DEVICE_ID >) is preserved — a channel
    push does not satisfy it, and a silent link forces a typed no_response.

The platform stand-in mirrors the hook-driven connect()/disconnect()
lifecycle (clean slate per attempt, _post_connect abort, _initial_sync
failure teardown, _close_session on every teardown path, watchdog
auto-start), so the hooks this driver overrides run exactly as they do on
the real platform.

Loads the driver + simulator with the ``openavc.*`` imports
stubbed so the community CI stays self-contained (conftest.py rolls the stubs
back after this module is collected).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    StubEvents as _FakeEvents,
    StubState as _FakeState,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "audio" / "shure_network.py"
SIM_PATH = REPO_ROOT / "audio" / "shure_network_sim.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeBaseDriver(LifecycleFake):
    """Functional stand-in for the platform BaseDriver surface this driver
    uses: the hook-driven connect()/disconnect() lifecycle, the liveness
    watchdog, and the child-entity registry (integer-id validation
    included)."""

    DRIVER_INFO: dict = {}

    HEALTH_FAULT_MESSAGE = "The device stopped answering probes."

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False
        self.disconnect_calls = 0
        self.stashed_fault: tuple[str, str] | None = None
        self._health_task = None
        self._health_failures = 0
        self._bg_tasks: set = set()
        self._children: dict = {}
        self._order: dict = {}
        self._project_child_entities: dict = {}

    def set_state(self, key, value) -> None:
        self.state.set(key, value)

    def get_state(self, key, default=None):
        return self.state.data.get(key, default)

    # -- child registry (mirrors the platform's integer-id validation) --

    def _eff_schema(self, child_type, local_id):
        types = self.DRIVER_INFO.get("child_entity_types", {})
        sch = dict(types.get(child_type, {}).get("state_variables", {}))
        sch.setdefault("online", {"type": "boolean"})
        sch.setdefault("label", {"type": "string"})
        return sch

    def register_child(self, child_type, local_id, initial_state=None,
                       schema=None):
        tdef = self.DRIVER_INFO.get("child_entity_types", {}).get(child_type)
        if tdef is None:
            raise ValueError(f"unknown child type {child_type!r}")
        id_fmt = tdef.get("id_format", {})
        if not isinstance(local_id, int):
            raise TypeError(
                f"Child {child_type} local_id must be int, got "
                f"{type(local_id).__name__}: {local_id!r}")
        if local_id < id_fmt.get("min", 1):
            raise ValueError(
                f"Child {child_type} local_id {local_id} below id_format min")
        if (child_type, local_id) in self._children:
            return
        eff = self._eff_schema(child_type, local_id)
        st = {}
        for prop, vd in eff.items():
            t = vd.get("type")
            st[prop] = (True if prop == "online" else
                        False if t == "boolean" else
                        0 if t == "integer" else
                        0.0 if t in ("number", "float") else "")
        for prop, val in (initial_state or {}).items():
            if prop not in eff:
                raise ValueError(f"unknown prop {prop!r} for {child_type}")
            st[prop] = val
        self._children[(child_type, local_id)] = st
        self._order.setdefault(child_type, []).append(local_id)

    def deregister_child(self, child_type, local_id):
        self._children.pop((child_type, local_id), None)
        if local_id in self._order.get(child_type, []):
            self._order[child_type].remove(local_id)

    def list_children(self, child_type):
        return list(self._order.get(child_type, []))

    def get_child_state(self, child_type, local_id):
        return dict(self._children.get((child_type, local_id), {}))

    def set_child_state(self, child_type, local_id, prop, value):
        if (child_type, local_id) not in self._children:
            raise ValueError(f"unregistered child {child_type}/{local_id}")
        eff = self._eff_schema(child_type, local_id)
        if prop not in eff:
            raise ValueError(f"bad prop {prop!r} for {child_type}")
        self._children[(child_type, local_id)][prop] = value

    def count_children(self, child_type):
        return len(self._order.get(child_type, []))

    # -- disconnect bookkeeping + liveness watchdog (mirrors the platform) --

    def _handle_transport_disconnect(self) -> None:
        # Mirrors the platform: flip the flags synchronously, then schedule
        # the async teardown (stop loops, close transport, _close_session,
        # disconnect event).
        self._connected = False
        self.set_state("connected", False)
        self.disconnect_calls += 1
        if self.transport is not None:
            self.transport.connected = False
        task = asyncio.ensure_future(self._on_disconnect_cleanup())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _on_disconnect_cleanup(self) -> None:
        self._stop_health_loop()
        await self.stop_polling()
        transport = self.transport
        self.transport = None
        if transport is not None:
            await transport.close()
        await self._close_session()
        await self.events.emit(f"device.disconnected.{self.device_id}")

    # -- connection lifecycle (mirrors the platform's hook-driven connect) --

    async def _pre_connect(self) -> None:
        pass

    async def _post_connect(self) -> None:
        pass

    async def _initial_sync(self) -> None:
        pass

    async def _close_session(self) -> None:
        pass

    async def _create_transport(self, transport_type) -> None:
        kwargs = dict(
            host=self.config.get("host", ""),
            port=self.config.get("port", 2202),
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            delimiter=b"\r",
            frame_parser=self._create_frame_parser(),
            inter_command_delay=self.config.get("inter_command_delay", 0.0),
            timeout=self.config.get("timeout", 5.0),
            name=self.device_id,
        )
        self.transport = await _FakeTCPTransport.create(
            **self._transport_kwargs(transport_type, kwargs))

    async def connect(self) -> None:
        # 1. Clean slate: reset fault classification, drop a previous
        #    attempt's driver session and stale transport.
        self.stashed_fault = None
        await self._close_session()
        if self.transport:
            await self.transport.close()
            self.transport = None
        # 2-3. Establish: pre-connect hook, then the transport.
        await self._pre_connect()
        await self._create_transport("tcp")
        # 4. Handshake: a raise here aborts the connection.
        try:
            await self._post_connect()
        except Exception:
            self._stash_transport_error()
            if self.transport:
                await self.transport.close()
                self.transport = None
            await self._close_session()
            self._connected = False
            raise
        # 5. Declare connected.
        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        # 6. Initial sync: a raise here tears the connection back down.
        try:
            await self._initial_sync()
        except Exception:
            self._stash_transport_error()
            transport = self.transport
            self.transport = None
            if transport is not None:
                await transport.close()
            await self._close_session()
            self._connected = False
            self.set_state("connected", False)
            await self.events.emit(f"device.disconnected.{self.device_id}")
            raise
        # 7. Polling + liveness watchdog.
        if self.config.get("poll_interval", 0):
            await self.start_polling(self.config["poll_interval"])
        if self._health_enabled():
            self._start_health_loop()

    async def disconnect(self) -> None:
        self._stop_health_loop()
        await self.stop_polling()
        if self.transport:
            await self.transport.close()
            self.transport = None
        await self._close_session()
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")


class _FakeSimState:
    def __init__(self, initial) -> None:
        self.data = dict(initial)

    def get(self, key, default=None):
        return self.data.get(key, default)


class _FakeTCPSimulator:
    """Stand-in for openavc.simulator.tcp_simulator.TCPSimulator."""

    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self.state = _FakeSimState(self.SIMULATOR_INFO.get("initial_state", {}))
        self._clients: dict = {}

    def set_state(self, key, value) -> None:
        self.state.data[key] = value

    async def push(self, data: bytes) -> None:
        # Unsolicited frames to every client, delimiter-framed like a reply.
        for transport in list(self._clients.values()):
            for frame in data.split(b">"):
                if frame.strip():
                    await transport.on_data(frame)


_CURRENT_SIM: object | None = None
# When True, the transport processes requests but DROPS every reply — a
# silently-vanished device for the liveness tests.
_SWALLOW = False


class _FakeTCPTransport:
    """In-memory transport that mimics the real delimiter (``>``) framing:
    each ``>``-terminated frame is delivered to ``on_data`` with the delimiter
    stripped, exactly as DelimiterFrameParser does."""

    def __init__(self, on_data, on_disconnect) -> None:
        self.on_data = on_data
        self.on_disconnect = on_disconnect
        self.connected = True
        self._sim = _CURRENT_SIM
        self.sent: list[str] = []

    @classmethod
    async def create(cls, *, host, port, on_data, on_disconnect,
                     delimiter=None, timeout=5.0, name="", **kw):
        t = cls(on_data, on_disconnect)
        t._sim._clients["c1"] = t
        return t

    async def send(self, data) -> None:
        if not self.connected:
            raise ConnectionError("transport closed")
        self.sent.append(bytes(data).decode("ascii"))
        resp = self._sim.handle_command(bytes(data))
        if resp and not _SWALLOW:
            # Deliver one delimiter-stripped frame per ">" (real transport).
            for frame in resp.split(b">"):
                if frame.strip():
                    await self.on_data(frame)

    async def close(self) -> None:
        self.connected = False


def _load(name: str, path: Path) -> ModuleType:
    server = ModuleType("openavc")
    server.__path__ = []  # type: ignore[attr-defined]
    sys.modules["openavc"] = server
    for sub in ("drivers", "transport", "utils"):
        m = ModuleType(f"openavc.{sub}")
        m.__path__ = []  # type: ignore[attr-defined]
        sys.modules[f"openavc.{sub}"] = m
    base = ModuleType("openavc.drivers.base")
    base.BaseDriver = _FakeBaseDriver
    sys.modules["openavc.drivers.base"] = base
    tcp = ModuleType("openavc.transport.tcp")
    tcp.TCPTransport = _FakeTCPTransport
    sys.modules["openavc.transport.tcp"] = tcp
    logger = ModuleType("openavc.utils.logger")
    logger.get_logger = lambda name="x": logging.getLogger(name)
    sys.modules["openavc.utils.logger"] = logger

    sim_pkg = ModuleType("openavc.simulator")
    sim_pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules["openavc.simulator"] = sim_pkg
    sim_tcp = ModuleType("openavc.simulator.tcp_simulator")
    sim_tcp.TCPSimulator = _FakeTCPSimulator
    sys.modules["openavc.simulator.tcp_simulator"] = sim_tcp

    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


DRV = _load("shure_under_test", DRIVER_PATH)
SIM = _load("shure_sim_under_test", SIM_PATH)


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(driver_overrides=None, sim_overrides=None, lobes=False):
    """A driver wired to a simulator. The simulator plays an MXA920 for a
    count of 9 (its Model setting overrides), which starts with automatic
    coverage on as from the factory; ``lobes`` turns it off first, for the
    tests about the plain channel properties."""
    global _CURRENT_SIM, _SWALLOW
    _SWALLOW = False
    sim_cfg = {"channel_count": 9}
    sim_cfg.update(sim_overrides or {})
    sim = SIM.ShureNetworkSimulator("sim1", sim_cfg)
    if lobes:
        sim._settings["AUTO_COVERAGE"] = False
    _CURRENT_SIM = sim

    cfg = {"host": "10.0.0.5", "port": 2202, "channel_count": 9}
    cfg.update(driver_overrides or {})
    driver = DRV.ShureNetworkDriver("mxa1", cfg, _FakeState(), _FakeEvents())
    return driver, sim


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_metadata_and_actions_shape():
    info = DRV.ShureNetworkDriver.DRIVER_INFO
    assert info["version"] == "2.2.0"
    # learned_from on Channel Count and Model ships in 0.37.0.
    assert info["min_platform_version"] == "0.37.0"
    assert (info["config_schema"]["channel_count"]["learned_from"]
            == "channel_count_reported")
    assert info["config_schema"]["model"]["learned_from"] == "model_family"
    model_values = {v["value"] for v in info["config_schema"]["model"]["values"]}
    assert {"", "MXA925", "MXA320", "MXA902"} <= model_values
    for cid in info["quick_actions"]:
        assert cid in info["commands"], cid
    assert {a["id"] for a in info["actions"]} == set(info["quick_actions"])
    # Every device setting reads back from a declared state variable.
    for key, sdef in info["device_settings"].items():
        assert sdef["state_key"] in info["state_variables"], key
        assert key in DRV._SETTING_WIRE, key
    # Every command has a branch (a declared command that falls through
    # answers success and does nothing).
    import inspect
    src = inspect.getsource(DRV.ShureNetworkDriver.send_command)
    for cid in info["commands"]:
        assert f'"{cid}"' in src, cid
    # Every command says what the device does.
    for cid, cdef in info["commands"].items():
        assert cdef.get("help"), cid


def test_discovery_probe_claims_only_this_drivers_devices():
    import re
    disc = DRV.ShureNetworkDriver.DRIVER_INFO["discovery"]
    probe = re.compile(disc["tcp_probe"]["expect_regex"])
    assert probe.search("< REP MODEL {MXA920                          } >")
    assert probe.search("< REP MODEL {P300} >")
    # The wireless systems have their own drivers.
    for model in ("AD4Q", "ULXD4D", "SLXD4", "QLXD4", "MXWAPX8"):
        assert not probe.search("< REP MODEL {" + model + "} >"), model
    # Every Shure device whose document has no MODEL answers this way, so it
    # identifies nothing.
    assert not probe.search("< REP ERR >")
    assert not any(h.upper().startswith("^MXW") for h in disc["hostname"])


def test_child_type_shape_and_pickers():
    info = DRV.ShureNetworkDriver.DRIVER_INFO
    types = info["child_entity_types"]
    assert set(types) == {"channel"}
    ch = types["channel"]
    # 1-based ids (channel 0 is the "all" broadcast index, not an entity).
    assert ch["id_format"]["min"] == 1
    assert set(ch["state_variables"]) == {
        "mute", "gain_db", "name", "meter", "gate", "clip", "polar_pattern",
        "lobe_angle"}
    assert ch["label_field"] == "name"
    # Every child_id param points at the channel type.
    for cid, cmd in info["commands"].items():
        for pname, pdef in cmd.get("params", {}).items():
            if pdef.get("type") == "child_id":
                assert pdef["child_type"] == "channel", f"{cid}.{pname}"


def test_led_brightness_bound_widened_to_five():
    """MXA920 / MXA910 fw>3.0 document brightness 0-5; the old YAML driver
    capped at 2, which the runtime MM gate would use to block valid levels."""
    info = DRV.ShureNetworkDriver.DRIVER_INFO
    assert info["commands"]["set_led_brightness"]["params"]["level"]["max"] == 5
    assert info["device_settings"]["led_brightness"]["max"] == 5


def test_gain_param_bounds_match_wire_range():
    info = DRV.ShureNetworkDriver.DRIVER_INFO
    gp = info["commands"]["set_channel_gain"]["params"]["gain_db"]
    assert gp["min"] == -110.0
    assert gp["max"] == 30.0
    ch_gain = info["child_entity_types"]["channel"]["state_variables"]["gain_db"]
    assert ch_gain["unit"] == "dB"
    assert ch_gain["control"] is True


# ── Topology registration ───────────────────────────────────────────────────

def test_roster_follows_channel_count():
    """A model whose count the model does not fix (a P300 here) keeps the
    configured count."""
    async def scenario():
        driver, _sim = await _make_pair({"channel_count": 4},
                                        {"channel_count": 6})
        await driver.connect()
        try:
            await asyncio.sleep(0.05)
            assert driver.get_state("model") == "P300"
            assert driver.get_state("channel_count_reported") is None
            assert driver.count_children("channel") == 4
            assert driver.list_children("channel") == [1, 2, 3, 4]
        finally:
            await driver.disconnect()
    _run(scenario())


def test_an_mxa_sizes_the_roster_from_its_model():
    """Configured for 4, an MXA920 answers: the roster grows to its 9
    channels, the new ones are read, and the count is reported so the
    platform saves it into Channel Count."""
    async def scenario():
        driver, _sim = await _make_pair({"channel_count": 4},
                                        {"channel_count": 9})
        await driver.connect()
        try:
            await asyncio.sleep(0.1)
            assert driver.get_state("model") == "MXA920"
            assert driver.get_state("channel_count_reported") == 9
            assert driver.list_children("channel") == list(range(1, 10))
            assert driver.get_child_state("channel", 9).get("gain_db") is not None
        finally:
            await driver.disconnect()
    _run(scenario())


def test_an_ani4in_shrinks_an_oversized_roster():
    async def scenario():
        driver, _sim = await _make_pair({"channel_count": 8},
                                        {"channel_count": 4})
        await driver.connect()
        try:
            await asyncio.sleep(0.05)
            assert driver.get_state("model") == "ANI4IN"
            assert driver.get_state("channel_count_reported") == 4
            assert driver.list_children("channel") == [1, 2, 3, 4]
        finally:
            await driver.disconnect()
    _run(scenario())


def test_model_prefixes():
    assert DRV._channel_count_for_model("MXA910W-US") == 9
    assert DRV._channel_count_for_model("  mxa920  ") == 9
    assert DRV._channel_count_for_model("ANI4OUT") == 4
    assert DRV._channel_count_for_model("P300") is None
    assert DRV._channel_count_for_model("ANI22") is None


def test_topology_reconciles_shrunk_roster():
    async def scenario():
        driver, _sim = await _make_pair({"channel_count": 4})
        driver.register_child("channel", 8)  # leftover from a larger config
        driver._register_topology()
        assert 8 not in driver.list_children("channel")
        assert driver.count_children("channel") == 4
    _run(scenario())


def test_project_label_not_overridden_by_placeholder():
    async def scenario():
        driver, _sim = await _make_pair()
        driver._project_child_entities = {
            "channel": {"01": {"label": "Podium Mic"}}}
        driver._register_topology()
        # No placeholder seeded for ch1 — platform applies the project label
        # (stub default ""); ch2 gets the driver placeholder.
        assert driver.get_child_state("channel", 1)["label"] == ""
        assert driver.get_child_state("channel", 2)["label"] == "Channel 2"
    _run(scenario())


# ── Connect round trip / push into children ─────────────────────────────────

def test_missing_host_raises_before_transport():
    async def scenario():
        driver, _sim = await _make_pair({"host": ""})
        try:
            await driver.connect()
        except ConnectionError as exc:
            assert "host" in str(exc).lower()
            assert driver.transport is None
            assert not driver._connected
            return
        raise AssertionError("connect succeeded without a host")
    _run(scenario())


def test_disconnect_unblocks_inflight_probe():
    """Teardown must fail a probe still awaiting its DEVICE_ID reply so the
    health loop never hangs on a dead link."""
    async def scenario():
        global _SWALLOW
        driver, _sim = await _make_pair()
        await driver.connect()
        _SWALLOW = True
        try:
            probe = asyncio.ensure_future(driver._liveness_probe())
            await asyncio.sleep(0.05)
            assert not probe.done()
            await driver.disconnect()
            assert driver._probe_fut is None
            try:
                await asyncio.wait_for(probe, 1.0)
            except ConnectionError:
                return
            raise AssertionError("probe survived disconnect")
        finally:
            _SWALLOW = False
    _run(scenario())


def test_connect_populates_device_and_channels():
    async def scenario():
        driver, sim = await _make_pair(lobes=True)
        await driver.connect()
        try:
            assert driver._connected
            # Device-level.
            assert driver.get_state("device_name") == "MXA920-SIM"
            assert driver.get_state("mute") is False
            assert driver.get_state("firmware") == "4.6.11"
            assert driver.get_state("led_brightness") == 2
            # Channel names came via the index-0 CHAN_NAME fan-out ->
            # label_field; brace + 31-char padding stripped.
            assert driver.get_child_state("channel", 1)["name"] == "Channel 1"
            assert driver.get_child_state("channel", 9)["name"] == "Channel 9"
            # Mutes via the index-0 AUDIO_MUTE fan-out.
            assert driver.get_child_state("channel", 3)["mute"] is False
            # Gain per-channel: default wire 1100 = 0.0 dB.
            assert driver.get_child_state("channel", 5)["gain_db"] == 0.0
        finally:
            await driver.disconnect()
    _run(scenario())


def test_channel_mute_round_trip():
    async def scenario():
        driver, sim = await _make_pair(lobes=True)
        await driver.connect()
        try:
            await driver.send_command(
                "set_channel_mute", {"channel": 4, "mute": True})
            assert sim._ch_mute[4] is True
            assert driver.get_child_state("channel", 4)["mute"] is True
            await driver.send_command("toggle_channel_mute", {"channel": 4})
            assert sim._ch_mute[4] is False
            assert driver.get_child_state("channel", 4)["mute"] is False
        finally:
            await driver.disconnect()
    _run(scenario())


def test_channel_gain_db_wire_translation():
    async def scenario():
        driver, sim = await _make_pair(lobes=True)
        await driver.connect()
        try:
            # -12 dB -> wire round((-12+110)*10)=980; sim stores it; read back
            # -12.0 dB.
            await driver.send_command(
                "set_channel_gain", {"channel": 2, "gain_db": -12.0})
            assert sim._ch_gain[2] == 980
            assert "< SET 2 AUDIO_GAIN_HI_RES 980 >" in driver.transport.sent
            assert driver.get_child_state("channel", 2)["gain_db"] == -12.0
            # Full-scale extremes.
            await driver.send_command(
                "set_channel_gain", {"channel": 3, "gain_db": 30.0})
            assert sim._ch_gain[3] == 1400
            assert driver.get_child_state("channel", 3)["gain_db"] == 30.0
            await driver.send_command(
                "set_channel_gain", {"channel": 6, "gain_db": -110.0})
            assert sim._ch_gain[6] == 0
            assert driver.get_child_state("channel", 6)["gain_db"] == -110.0
        finally:
            await driver.disconnect()
    _run(scenario())


def test_step_channel_gain_inc_dec_bytes():
    async def scenario():
        driver, sim = await _make_pair(lobes=True)
        await driver.connect()
        try:
            # From the default 1100 (0 dB): +3 dB nudges the wire by 30.
            await driver.send_command(
                "step_channel_gain", {"channel": 1, "delta_db": 3.0})
            assert "< SET 1 AUDIO_GAIN_HI_RES inc 30 >" in driver.transport.sent
            assert sim._ch_gain[1] == 1130
            assert driver.get_child_state("channel", 1)["gain_db"] == 3.0
            await driver.send_command(
                "step_channel_gain", {"channel": 1, "delta_db": -1.5})
            assert "< SET 1 AUDIO_GAIN_HI_RES dec 15 >" in driver.transport.sent
            assert sim._ch_gain[1] == 1115
        finally:
            await driver.disconnect()
    _run(scenario())


def test_device_mute_and_preset_round_trip():
    async def scenario():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command("mute_on")
            assert driver.get_state("mute") is True
            assert driver.get_state("audio_mute") is True
            await driver.send_command("mute_toggle")
            assert driver.get_state("mute") is False
            await driver.send_command("recall_preset", {"preset": 4})
            assert "< SET PRESET 4 >" in driver.transport.sent
            await driver.send_command("flash_on")
            assert "< SET FLASH ON >" in driver.transport.sent
            await driver.send_command(
                "set_led_color", {"which": "MUTED", "color": "RED"})
            assert "< SET LED_COLOR_MUTED RED >" in driver.transport.sent
        finally:
            await driver.disconnect()
    _run(scenario())


def test_led_brightness_device_setting_round_trip():
    async def scenario():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            # Level 5 (would be blocked by the old max=2 gate) applies.
            await driver.set_device_setting("led_brightness", 5)
            assert sim._brightness == 5
            await driver.poll()  # device-level read-back
            assert driver.get_state("led_brightness") == 5
        finally:
            await driver.disconnect()
    _run(scenario())


def test_raw_command_passthrough():
    async def scenario():
        driver, _sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command(
                "raw_command", {"command": "< GET SERIAL_NUM >"})
            assert "< GET SERIAL_NUM >" in driver.transport.sent
        finally:
            await driver.disconnect()
    _run(scenario())


# ── Metering ────────────────────────────────────────────────────────────────

def test_metering_sample_fans_into_channel_meters():
    async def scenario():
        driver, sim = await _make_pair(
            {"meter_interval_ms": 500}, {"meter_interval_ms": 500},
            lobes=True)
        await driver.connect()
        try:
            # The sim answers the METER_RATE subscribe with one SAMPLE row of
            # 000-060 values (ch n -> 10+n), which map to -60..0 dBFS.
            assert driver.get_child_state("channel", 1)["meter"] == 11 - 60
            assert driver.get_child_state("channel", 9)["meter"] == 19 - 60
        finally:
            await driver.disconnect()
    _run(scenario())


def test_single_value_sample_is_not_positionally_attributed():
    """A one-value SAMPLE from a device whose channels the driver does not
    know (a P300 here) cannot be placed: it is skipped, not written to ch1."""
    async def scenario():
        driver, _sim = await _make_pair({"channel_count": 6},
                                        {"channel_count": 6})
        await driver.connect()
        try:
            assert driver.get_state("model") == "P300"
            before = driver.get_child_state("channel", 1)["meter"]
            await driver.on_data_received(b"< SAMPLE 042 ")
            assert driver.get_child_state("channel", 1)["meter"] == before
        finally:
            await driver.disconnect()
    _run(scenario())


def test_other_meters_and_talker_frames_never_reach_channel_meters():
    async def scenario():
        driver, _sim = await _make_pair(lobes=True)
        await driver.connect()
        try:
            for frame in (b"< SAMPLE_OUT 050 050 050 ",
                          b"< SAMPLE_PRECOMP 050 ",
                          b"< SAMPLE_AECREF 050 ",
                          b"< SAMPLE TALKER_POSITIONS 1 0 -1524 321 914 0 "):
                await driver.on_data_received(frame)
            for ch in (1, 2, 3):
                assert driver.get_child_state("channel", ch)["meter"] == 0.0
        finally:
            await driver.disconnect()
    _run(scenario())


# ── Push parsing ────────────────────────────────────────────────────────────

def test_unsolicited_channel_push_updates_state():
    """The device pushes a REP whenever a value changes elsewhere; the same
    parser must route it into child state."""
    async def scenario():
        driver, _sim = await _make_pair(lobes=True)
        await driver.connect()
        try:
            await driver.on_data_received(b"< REP 07 AUDIO_MUTE ON ")
            assert driver.get_child_state("channel", 7)["mute"] is True
            await driver.on_data_received(
                b"< REP 07 CHAN_NAME {Wireless Lav             } ")
            assert driver.get_child_state("channel", 7)["name"] == "Wireless Lav"
        finally:
            await driver.disconnect()
    _run(scenario())


def test_out_of_roster_index_ignored():
    async def scenario():
        # A P300 keeps the configured count.
        driver, _sim = await _make_pair({"channel_count": 4},
                                        {"channel_count": 6})
        await driver.connect()
        try:
            # Channel 9 isn't in a 4-channel roster — the REP must be dropped,
            # not create an orphan.
            await driver.on_data_received(b"< REP 09 AUDIO_MUTE ON ")
            assert 9 not in driver.list_children("channel")
        finally:
            await driver.disconnect()
    _run(scenario())


# ── Liveness ────────────────────────────────────────────────────────────────

def test_probe_resolves_on_device_id_reply():
    async def scenario():
        driver, _sim = await _make_pair()
        await driver.connect()
        try:
            await asyncio.wait_for(driver._liveness_probe(), 1.0)
        finally:
            await driver.disconnect()
    _run(scenario())


def test_probe_times_out_when_silent():
    async def scenario():
        global _SWALLOW
        driver, _sim = await _make_pair()
        await driver.connect()
        try:
            _SWALLOW = True
            try:
                await asyncio.wait_for(driver._liveness_probe(), 0.1)
            except asyncio.TimeoutError:
                return
            raise AssertionError("probe resolved with no reply")
        finally:
            _SWALLOW = False
            await driver.disconnect()
    _run(scenario())


def test_channel_push_does_not_satisfy_probe():
    async def scenario():
        global _SWALLOW
        driver, _sim = await _make_pair()
        await driver.connect()
        try:
            _SWALLOW = True  # sim replies dropped; inject lines by hand
            probe = asyncio.ensure_future(driver._liveness_probe())
            await asyncio.sleep(0.05)
            assert not probe.done()
            # A mute push is not a DEVICE_ID reply — it must not resolve it.
            await driver.on_data_received(b"< REP 02 AUDIO_MUTE ON ")
            await asyncio.sleep(0.05)
            assert not probe.done()
            assert driver.get_child_state("channel", 2)["mute"] is True
            # The real DEVICE_ID reply resolves it.
            await driver.on_data_received(b"< REP DEVICE_ID {MXA920-SIM} ")
            await asyncio.wait_for(probe, 1.0)
        finally:
            _SWALLOW = False
            await driver.disconnect()
    _run(scenario())


def test_health_loop_forces_reconnect_on_silent_device():
    async def scenario():
        global _SWALLOW
        driver, _sim = await _make_pair()
        await driver.connect()
        driver._stop_health_loop()  # restart at test-speed cadence
        driver.HEALTH_INTERVAL_S = 0.01
        driver.HEALTH_TIMEOUT_S = 0.05
        _SWALLOW = True
        try:
            driver._start_health_loop()
            for _ in range(100):
                await asyncio.sleep(0.05)
                if driver.disconnect_calls:
                    break
            assert driver.disconnect_calls == 1
            assert driver.stashed_fault is not None
            code, message = driver.stashed_fault
            assert code == "no_response"
            assert "DEVICE_ID" in message
        finally:
            _SWALLOW = False
            driver._stop_health_loop()
            await driver.disconnect()
    _run(scenario())


# ── Refresh ─────────────────────────────────────────────────────────────────

def test_refresh_children_resyncs_after_device_side_change():
    async def scenario():
        driver, sim = await _make_pair(lobes=True)
        await driver.connect()
        try:
            # Device-side change our push missed: mutate the sim, then refresh.
            sim._ch_gain[2] = 700  # -40.0 dB
            assert driver.get_child_state("channel", 2)["gain_db"] == 0.0
            result = await driver.refresh_children()
            assert result["channel"] == 9
            assert driver.get_child_state("channel", 2)["gain_db"] == -40.0
        finally:
            await driver.disconnect()
    _run(scenario())


# ── MXA925 / MXA320 / MXA902 and automatic coverage (2.2.0) ─────────────────

async def _settle():
    # Let tasks spawned from a push (a mode change, a model change) run.
    for _ in range(5):
        await asyncio.sleep(0)


def test_new_models_and_channel_numbers():
    assert DRV._family_of("MXA925-S") == "MXA925"
    assert DRV._family_of("mxa320") == "MXA320"
    assert DRV._family_of("MXA902-W") == "MXA902"
    assert DRV._family_of("MXA901") == ""
    assert DRV._channel_count_for_model("MXA925") == 9
    assert DRV._channel_count_for_model("MXA320") == 5
    assert DRV._channel_count_for_model("MXA902") == 4
    assert sorted(DRV._CHANNEL_MAPS["MXA902"]) == [9, 10, 11, 12]


def test_discovery_probe_matches_the_new_arrays():
    import re
    probe = re.compile(
        DRV.ShureNetworkDriver.DRIVER_INFO["discovery"]["tcp_probe"]["expect_regex"])
    for model in ("MXA925", "MXA320", "MXA902"):
        sim = SIM.ShureNetworkSimulator("p", {"model": model})
        reply = sim.handle_command(b"< GET MODEL >\r\n").decode()
        assert probe.search(reply), model


def test_mxa925_starts_in_automatic_coverage_and_addresses_areas():
    """Factory default: channels 1-8 are coverage areas (CA_MUTE / CA_GAIN);
    AUDIO_* would answer REP ERR for them."""
    async def scenario():
        driver, sim = await _make_pair({"model": "MXA925"}, {"model": "MXA925"})
        await driver.connect()
        try:
            assert driver.get_state("model") == "MXA925"
            assert driver.get_state("model_family") == "MXA925"
            assert driver.get_state("auto_coverage") is True
            assert driver.list_children("channel") == list(range(1, 10))
            ch3 = driver.get_child_state("channel", 3)
            assert ch3["label"] == "Coverage Area 3"
            assert driver.get_child_state("channel", 9)["label"] == "Automix Output"
            # Area 1 is the talker's: its gate reads open.
            assert driver.get_child_state("channel", 1)["gate"] is True
            assert ch3["gate"] is False
            sent = driver.transport.sent
            assert "< GET 0 CA_MUTE >" in sent
            assert "< GET 3 CA_GAIN >" in sent
            assert "< GET 3 AUDIO_GAIN_HI_RES >" not in sent

            await driver.send_command("set_channel_mute",
                                      {"channel": 3, "mute": True})
            assert "< SET 3 CA_MUTE ON >" in driver.transport.sent
            assert sim._ca_mute[3] is True
            assert driver.get_child_state("channel", 3)["mute"] is True
            await driver.send_command("set_channel_gain",
                                      {"channel": 2, "gain_db": -6.0})
            assert "< SET 2 CA_GAIN 1040 >" in driver.transport.sent
            assert driver.get_child_state("channel", 2)["gain_db"] == -6.0
            await driver.send_command("step_channel_gain",
                                      {"channel": 2, "delta_db": 1.0})
            assert "< SET 2 CA_GAIN inc 10 >" in driver.transport.sent
            # The automix output keeps the ordinary properties.
            await driver.send_command("set_channel_mute",
                                      {"channel": 9, "mute": True})
            assert "< SET 9 AUDIO_MUTE ON >" in driver.transport.sent
            assert driver.get_child_state("channel", 9)["mute"] is True
            # No channel answered REP ERR along the way.
            assert all(v is False for k, v in sim._ch_mute.items() if k <= 8)
        finally:
            await driver.disconnect()
    _run(scenario())


def test_a_shipped_mxa920_project_learns_the_mode_without_a_model_set():
    """Model on Detect automatically: the reported model and mode are waited
    for at connect, so the first read addresses the areas."""
    async def scenario():
        driver, _sim = await _make_pair()
        await driver.connect()
        try:
            assert driver.get_state("model_family") == "MXA920"
            assert driver.get_child_state("channel", 5)["label"] == "Coverage Area 5"
            assert not any(m.startswith("< GET 5 AUDIO_GAIN")
                           for m in driver.transport.sent)
        finally:
            await driver.disconnect()
    _run(scenario())


def test_coverage_metering_routes_the_automix_and_the_areas():
    async def scenario():
        driver, _sim = await _make_pair(
            {"model": "MXA925", "meter_interval_ms": 500}, {"model": "MXA925"})
        await driver.connect()
        try:
            assert "< SET CA_METER_RATE 500 >" in driver.transport.sent
            # The plain SAMPLE carries the automix output alone.
            assert driver.get_child_state("channel", 9)["meter"] == 19 - 60
            # SAMPLE_CA: one value per area, 20 + n.
            assert driver.get_child_state("channel", 1)["meter"] == 21 - 60
            assert driver.get_child_state("channel", 8)["meter"] == 28 - 60
        finally:
            await driver.disconnect()
    _run(scenario())


def test_turning_automatic_coverage_off_and_on_again():
    async def scenario():
        driver, sim = await _make_pair({"model": "MXA920"}, {"model": "MXA920"})
        driver._project_child_entities = {
            "channel": {"02": {"label": "Lectern"}}}
        await driver.connect()
        try:
            await driver.set_device_setting("auto_coverage", False)
            assert "< SET AUTO_COVERAGE Off >" in driver.transport.sent
            await _settle()
            assert driver.get_state("auto_coverage") is False
            assert driver.get_child_state("channel", 3)["label"] == "Lobe 3"
            # The project's own label stays.
            assert driver.get_child_state("channel", 2)["label"] != "Lobe 2"
            # Lobes report names and take the ordinary properties.
            assert driver.get_child_state("channel", 3)["name"] == "Channel 3"
            await driver.send_command("set_channel_mute",
                                      {"channel": 3, "mute": True})
            assert "< SET 3 AUDIO_MUTE ON >" in driver.transport.sent
            assert sim._ch_mute[3] is True

            # Turned back on in Designer (the Simulator UI here): the device
            # reports it and the areas come back, without the lobe names.
            sim.set_state("auto_coverage", True)
            await _settle()
            assert driver.get_state("auto_coverage") is True
            assert driver.get_child_state("channel", 3)["label"] == "Coverage Area 3"
            assert driver.get_child_state("channel", 3)["name"] in (None, "")
            assert driver.get_child_state("channel", 1)["gate"] is True
        finally:
            await driver.disconnect()
    _run(scenario())


def test_talker_positions_for_camera_tracking():
    async def scenario():
        driver, _sim = await _make_pair(
            {"model": "MXA925", "talker_position_interval_ms": 500},
            {"model": "MXA925"})
        await driver.connect()
        try:
            assert "< SET TALKER_POSITION_RATE 500 >" in driver.transport.sent
            assert driver.get_state("talker_lobe") == 1
            assert driver.get_state("talker_coverage_area") == 1
            assert driver.get_state("talker_x") == -152
            assert driver.get_state("talker_y") == 305
            assert driver.get_state("talker_z") == 140
            assert driver.get_state("talker_count") == 2
            # The page's own example, and its array-group example (spelled
            # TALKER_POSTIONS, without the reserved field).
            await driver.on_data_received(
                b"< SAMPLE TALKER_POSITIONS 1 2 -1524 321 914 0 2 3 123 -321 914 0 ")
            assert driver.get_state("talker_x") == -1524
            assert driver.get_state("talker_coverage_area") == 2
            assert driver.get_state("talker_count") == 2
            await driver.on_data_received(
                b"< SAMPLE TALKER_POSTIONS 1 2 137 -168 152 ")
            assert driver.get_state("talker_y") == -168
            assert driver.get_state("talker_count") == 1
        finally:
            await driver.disconnect()
    _run(scenario())


def test_talker_interval_zero_leaves_the_microphone_alone():
    async def scenario():
        driver, _sim = await _make_pair({"model": "MXA925"}, {"model": "MXA925"})
        await driver.connect()
        try:
            assert not any("TALKER_POSITION_RATE" in m
                           for m in driver.transport.sent)
        finally:
            await driver.disconnect()
    _run(scenario())


def test_mxa925_settings_read_back():
    async def scenario():
        driver, sim = await _make_pair({"model": "MXA925"}, {"model": "MXA925"})
        await driver.connect()
        try:
            assert driver.get_state("speech_gating") == "low"
            assert driver.get_state("vab") == 0
            assert driver.get_state("array_height") == 274
            assert driver.get_state("device_state") == "no_error"
            assert driver.get_state("rj45_mode") == "switched"
            assert driver.get_state("led_color_muted") == "red"
            assert driver.get_state("preset") == 1
            assert driver.get_state("serial_number") == "SIMMXA925"
            await driver.set_device_setting("speech_gating", "medium")
            assert "< SET 9 SPEECH_GATING Medium >" in driver.transport.sent
            assert driver.get_state("speech_gating") == "medium"
            await driver.set_device_setting("vab", 12)
            assert driver.get_state("vab") == 12
            await driver.set_device_setting("array_height", 300)
            assert driver.get_state("array_height") == 300
            await driver.set_device_setting("loudspeaker_null", True)
            assert "< SET LOUDSPEAKER_NULL ON >" in driver.transport.sent
            assert driver.get_state("loudspeaker_null") is True
            await driver.set_device_setting("led_color_muted", "blue")
            assert "< SET LED_COLOR_MUTED BLUE >" in driver.transport.sent
            assert driver.get_state("led_color_muted") == "blue"
            await driver.set_device_setting("led_state_unmuted", "flashing")
            assert driver.get_state("led_state_unmuted") == "flashing"
            await driver.send_command("recall_preset", {"preset": 3})
            assert driver.get_state("preset") == 3
            # An error state set on the device shows on the next poll.
            sim._device_state = "2"
            await driver.poll()
            assert driver.get_state("device_state") == "non_clearable_error"
        finally:
            await driver.disconnect()
    _run(scenario())


def test_each_model_is_offered_only_what_its_page_documents():
    def offered(model):
        d = DRV.ShureNetworkDriver(
            "x", {"host": "h", "model": model}, _FakeState(), _FakeEvents())
        return d.DRIVER_INFO
    mxa925 = offered("MXA925")
    assert "auto_coverage" in mxa925["device_settings"]
    assert "speaker_on" not in mxa925["commands"]
    assert "speaker_on" not in mxa925["quick_actions"]
    assert [v["value"] for v in mxa925["device_settings"]["speech_gating"]["values"]] \
        == ["off", "low", "medium", "high"]
    mxa902 = offered("MXA902")
    assert {"speaker_on", "speaker_off"} <= set(mxa902["commands"])
    assert {"speaker_on", "speaker_off"} <= set(mxa902["quick_actions"])
    assert {"speaker_delay_ms", "compressor_speaker", "noise_filter"} \
        <= set(mxa902["device_settings"])
    assert "auto_coverage" not in mxa902["device_settings"]
    assert [v["value"] for v in mxa902["device_settings"]["speech_gating"]["values"]] \
        == ["on", "off"]
    mxa320 = offered("MXA320")
    assert {"set_polar_pattern", "set_lobe_angle"} <= set(mxa320["commands"])
    assert "mute_control_func" in mxa320["device_settings"]
    # A model this driver knows nothing about: the common surface only.
    unknown = offered("")
    assert set(unknown["device_settings"]) == {"led_brightness"}
    assert "speaker_on" not in unknown["commands"]
    assert "set_polar_pattern" not in unknown["commands"]
    # The class keeps the full declaration for the catalog.
    full = DRV.ShureNetworkDriver.DRIVER_INFO
    assert "speaker_on" in full["commands"]
    assert "auto_coverage" in full["device_settings"]


def test_mxa320_microphones_polar_pattern_and_lobe_angle():
    async def scenario():
        driver, sim = await _make_pair({"model": "MXA320"}, {"model": "MXA320"})
        await driver.connect()
        try:
            assert driver.list_children("channel") == [1, 2, 3, 4, 5]
            assert driver.get_child_state("channel", 1)["label"] == "Microphone 1"
            assert driver.get_child_state("channel", 5)["label"] == "Automix Output"
            assert driver.get_child_state("channel", 2)["polar_pattern"] == "cardioid"
            assert driver.get_child_state("channel", 3)["lobe_angle"] == 180
            await driver.send_command("set_polar_pattern",
                                      {"channel": 2, "pattern": "hyper"})
            assert "< SET 2 POLAR_PATTERN HYPER >" in driver.transport.sent
            assert driver.get_child_state("channel", 2)["polar_pattern"] == "hyper"
            await driver.send_command("set_lobe_angle",
                                      {"channel": 4, "angle": 315})
            assert driver.get_child_state("channel", 4)["lobe_angle"] == 315
            try:
                await driver.send_command("set_lobe_angle",
                                          {"channel": 4, "angle": 20})
            except ValueError:
                pass
            else:
                raise AssertionError("20 degrees is not a 15-degree step")
            # Speech gating is on / off here, indexed by the automix output 5.
            assert driver.get_state("speech_gating") == "on"
            await driver.set_device_setting("speech_gating", "off")
            assert "< SET 5 SPEECH_GATING OFF >" in driver.transport.sent
            assert driver.get_state("speech_gating") == "off"
            await driver.set_device_setting("mute_control_func", "logic")
            assert "< SET MUTE_CONTROL_FUNC LOGIC >" in driver.transport.sent
            assert driver.get_state("mute_control_func") == "logic"
            assert driver.get_state("device_state") == "no_error"
            # Its lobes' gates (no coverage areas here).
            assert "< GET 0 AUTOMIX_GATE_OUT_EXT_SIG >" in driver.transport.sent
            await driver.on_data_received(b"< REP 02 AUTOMIX_GATE_OUT_EXT_SIG ON ")
            assert driver.get_child_state("channel", 2)["gate"] is True
        finally:
            await driver.disconnect()
    _run(scenario())


def test_mxa320_meter_row_drops_the_aec_reference():
    async def scenario():
        driver, _sim = await _make_pair(
            {"model": "MXA320", "meter_interval_ms": 500}, {"model": "MXA320"})
        await driver.connect()
        try:
            # Six values (5 channels + the AEC reference 6): 10 + n.
            assert driver.get_child_state("channel", 5)["meter"] == 15 - 60
            assert 6 not in driver.list_children("channel")
            assert "< SET METER_RATE_IN 500 >" not in driver.transport.sent
        finally:
            await driver.disconnect()
    _run(scenario())


def test_mxa902_channels_loudspeaker_and_meters():
    async def scenario():
        driver, sim = await _make_pair(
            {"model": "MXA902", "meter_interval_ms": 500}, {"model": "MXA902"})
        await driver.connect()
        try:
            assert driver.list_children("channel") == [9, 10, 11, 12]
            assert driver.get_child_state("channel", 10)["label"] == "Loudspeaker Output"
            assert driver.get_child_state("channel", 11)["label"] == "Dante Input 1"
            # Four meter values, in channel order 9-12.
            assert driver.get_child_state("channel", 9)["meter"] == 19 - 60
            assert driver.get_child_state("channel", 12)["meter"] == 22 - 60
            # The loudspeaker's level is channel 10's gain.
            await driver.send_command("set_channel_gain",
                                      {"channel": 10, "gain_db": -20.0})
            assert "< SET 10 AUDIO_GAIN_HI_RES 900 >" in driver.transport.sent
            assert driver.get_child_state("channel", 10)["gain_db"] == -20.0
            assert driver.get_state("speaker") is True
            await driver.send_command("speaker_off")
            assert "< SET SPEAKER OFF >" in driver.transport.sent
            assert driver.get_state("speaker") is False
            await driver.set_device_setting("speaker_delay_ms", 25)
            assert "< SET 10 DELAY 0025 >" in driver.transport.sent
            assert driver.get_state("speaker_delay_ms") == 25
            assert driver.get_state("compressor_speaker") is True
            await driver.set_device_setting("compressor_speaker", False)
            assert "< SET 10 COMPRESSOR OFF >" in driver.transport.sent
            assert driver.get_state("compressor_speaker") is False
            assert driver.get_state("compressor_automix") is True
            await driver.set_device_setting("noise_filter", True)
            assert "< SET 9 NOISE_FILTER ON >" in driver.transport.sent
            assert driver.get_state("noise_filter") is True
            # The loudspeaker toggled in Designer (the Simulator UI here).
            sim.set_state("speaker", True)
            await _settle()
            assert driver.get_state("speaker") is True
            # Nothing the 902 lacks was asked for.
            assert not any("AUTO_COVERAGE" in m or "CA_" in m
                           for m in driver.transport.sent)
        finally:
            await driver.disconnect()
    _run(scenario())


def test_a_project_built_before_the_device_exists():
    """Model picked by hand: the roster and the controls are the model's
    before anything connects."""
    driver = DRV.ShureNetworkDriver(
        "x", {"host": "h", "model": "MXA902", "channel_count": 8},
        _FakeState(), _FakeEvents())
    driver._register_topology()
    assert driver.list_children("channel") == [9, 10, 11, 12]
    assert "speaker_on" in driver.DRIVER_INFO["commands"]


def test_the_device_corrects_a_wrong_model_setting():
    async def scenario():
        driver, _sim = await _make_pair({"model": "MXA320"}, {"model": "MXA902"})
        await driver.connect()
        try:
            assert driver.get_state("model_family") == "MXA902"
            assert driver.list_children("channel") == [9, 10, 11, 12]
            assert "speaker_on" in driver.DRIVER_INFO["commands"]
            assert "set_polar_pattern" not in driver.DRIVER_INFO["commands"]
        finally:
            await driver.disconnect()
    _run(scenario())


def test_an_scm820_has_no_model_and_connect_does_not_wait():
    async def scenario():
        driver, _sim = await _make_pair({"channel_count": 8},
                                        {"model": "SCM820", "channel_count": 8})
        loop = asyncio.get_running_loop()
        start = loop.time()
        await driver.connect()
        try:
            # REP ERR answers the MODEL question at once.
            assert loop.time() - start < 1.0
            assert driver.get_state("model") is None
            assert driver.list_children("channel") == list(range(1, 9))
        finally:
            await driver.disconnect()
    _run(scenario())


def test_reboot_and_restore_defaults():
    info = DRV.ShureNetworkDriver.DRIVER_INFO["commands"]
    assert info["reboot"]["restarts_device_for"] > 0
    assert info["restore_defaults"]["confirm"]

    async def scenario():
        driver, _sim = await _make_pair(lobes=True)
        await driver.connect()
        try:
            await driver.send_command("restore_defaults")
            assert "< SET DEFAULT_SETTINGS >" in driver.transport.sent
            await driver.send_command("reboot")
            assert "< SET REBOOT >" in driver.transport.sent
        finally:
            await driver.disconnect()
    _run(scenario())


def test_clip_is_read_and_followed():
    async def scenario():
        driver, _sim = await _make_pair(lobes=True)
        await driver.connect()
        try:
            assert "< GET 0 AUDIO_OUT_CLIP_INDICATOR >" in driver.transport.sent
            assert driver.get_child_state("channel", 4)["clip"] is False
            await driver.on_data_received(b"< REP 04 AUDIO_OUT_CLIP_INDICATOR ON ")
            assert driver.get_child_state("channel", 4)["clip"] is True
        finally:
            await driver.disconnect()
    _run(scenario())
