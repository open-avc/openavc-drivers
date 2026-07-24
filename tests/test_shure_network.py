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

Loads the driver + simulator with the ``server.*`` / ``simulator.*`` imports
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

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "audio" / "shure_network.py"
SIM_PATH = REPO_ROOT / "audio" / "shure_network_sim.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeState:
    def __init__(self) -> None:
        self.data: dict = {}

    def set(self, key, value, **_):
        self.data[key] = value


class _FakeEvents:
    async def emit(self, name, *args, **kwargs):
        pass


class _FakeBaseDriver:
    """Functional stand-in for the platform BaseDriver surface this driver
    uses: the hook-driven connect()/disconnect() lifecycle, the liveness
    watchdog, and the child-entity registry (integer-id validation
    included)."""

    DRIVER_INFO: dict = {}

    HEALTH_INTERVAL_S = 30.0
    HEALTH_TIMEOUT_S = 5.0
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = "Connected, but the device stopped answering probes."

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

    def _stash_fault(self, code, message="") -> None:
        self.stashed_fault = (code, message)

    def _stash_transport_error(self) -> None:
        pass

    async def _liveness_probe(self) -> None:
        raise NotImplementedError

    def _health_enabled(self) -> bool:
        return type(self)._liveness_probe is not _FakeBaseDriver._liveness_probe

    def _start_health_loop(self) -> None:
        if self._health_task is None or self._health_task.done():
            self._health_failures = 0
            self._health_task = asyncio.ensure_future(self._health_loop())

    def _stop_health_loop(self) -> None:
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
        self._health_task = None

    async def _health_loop(self) -> None:
        interval = float(self.HEALTH_INTERVAL_S)
        timeout = float(self.HEALTH_TIMEOUT_S)
        max_failures = max(int(self.HEALTH_MAX_FAILURES), 1)
        try:
            while self.transport is not None and getattr(
                    self.transport, "connected", False):
                await asyncio.sleep(interval)
                if not (self.transport is not None and getattr(
                        self.transport, "connected", False)):
                    return
                try:
                    await asyncio.wait_for(self._liveness_probe(), timeout)
                    self._health_failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._health_failures += 1
                    if self._health_failures >= max_failures:
                        self._force_disconnect(
                            "no_response", self.HEALTH_FAULT_MESSAGE)
                        return
        except asyncio.CancelledError:
            return

    def _force_disconnect(self, code="no_response", message="") -> None:
        self._health_task = None
        self._stash_fault(code, message)
        self._handle_transport_disconnect()

    async def start_polling(self, interval) -> None:
        pass

    async def stop_polling(self) -> None:
        pass

    # -- connection lifecycle (mirrors the platform's hook-driven connect) --

    async def _pre_connect(self) -> None:
        pass

    def _transport_kwargs(self, transport_type, kwargs):
        return kwargs

    def _create_frame_parser(self):
        return None

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
    """Stand-in for simulator.tcp_simulator.TCPSimulator."""

    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self.state = _FakeSimState(self.SIMULATOR_INFO.get("initial_state", {}))
        self._clients: dict = {}

    def set_state(self, key, value) -> None:
        self.state.data[key] = value


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
    server = ModuleType("server")
    server.__path__ = []  # type: ignore[attr-defined]
    sys.modules["server"] = server
    for sub in ("drivers", "transport", "utils"):
        m = ModuleType(f"server.{sub}")
        m.__path__ = []  # type: ignore[attr-defined]
        sys.modules[f"server.{sub}"] = m
    base = ModuleType("server.drivers.base")
    base.BaseDriver = _FakeBaseDriver
    sys.modules["server.drivers.base"] = base
    tcp = ModuleType("server.transport.tcp")
    tcp.TCPTransport = _FakeTCPTransport
    sys.modules["server.transport.tcp"] = tcp
    logger = ModuleType("server.utils.logger")
    logger.get_logger = lambda name="x": logging.getLogger(name)
    sys.modules["server.utils.logger"] = logger

    sim_pkg = ModuleType("simulator")
    sim_pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules["simulator"] = sim_pkg
    sim_tcp = ModuleType("simulator.tcp_simulator")
    sim_tcp.TCPSimulator = _FakeTCPSimulator
    sys.modules["simulator.tcp_simulator"] = sim_tcp

    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


DRV = _load("shure_under_test", DRIVER_PATH)
SIM = _load("shure_sim_under_test", SIM_PATH)


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(driver_overrides=None, sim_overrides=None):
    global _CURRENT_SIM, _SWALLOW
    _SWALLOW = False
    sim_cfg = {"channel_count": 9}
    sim_cfg.update(sim_overrides or {})
    sim = SIM.ShureNetworkSimulator("sim1", sim_cfg)
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
    assert info["version"] == "2.0.1"
    # The connection lifecycle hooks this driver overrides ship in 0.24.0.
    assert info["min_platform_version"] == "0.24.0"
    for cid in info["quick_actions"]:
        assert cid in info["commands"], cid
    assert {a["id"] for a in info["actions"]} == set(info["quick_actions"])
    # Device-level flat state; per-channel values live on children.
    assert set(info["state_variables"]) == {
        "device_name", "mute", "audio_mute", "led_brightness", "firmware"}


def test_child_type_shape_and_pickers():
    info = DRV.ShureNetworkDriver.DRIVER_INFO
    types = info["child_entity_types"]
    assert set(types) == {"channel"}
    ch = types["channel"]
    # 1-based ids (channel 0 is the "all" broadcast index, not an entity).
    assert ch["id_format"]["min"] == 1
    assert set(ch["state_variables"]) == {"mute", "gain_db", "name", "meter"}
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
    async def scenario():
        driver, _sim = await _make_pair({"channel_count": 4})
        await driver.connect()
        try:
            assert driver.count_children("channel") == 4
            assert driver.list_children("channel") == [1, 2, 3, 4]
        finally:
            await driver.disconnect()
    _run(scenario())


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
        driver, sim = await _make_pair()
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
        driver, sim = await _make_pair()
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
        driver, sim = await _make_pair()
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
        driver, sim = await _make_pair()
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
            {"meter_interval_ms": 500}, {"meter_interval_ms": 500})
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
    """An MXA array in automatic-coverage mode sends a 1-value SAMPLE (the
    automixer output) that can't be mapped to a specific channel — it must be
    skipped when more than one channel is configured, not written to ch1."""
    async def scenario():
        driver, _sim = await _make_pair()
        await driver.connect()
        try:
            before = driver.get_child_state("channel", 1)["meter"]
            await driver.on_data_received(b"< SAMPLE 042 ")
            assert driver.get_child_state("channel", 1)["meter"] == before
        finally:
            await driver.disconnect()
    _run(scenario())


# ── Push parsing ────────────────────────────────────────────────────────────

def test_unsolicited_channel_push_updates_state():
    """The device pushes a REP whenever a value changes elsewhere; the same
    parser must route it into child state."""
    async def scenario():
        driver, _sim = await _make_pair()
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
        driver, _sim = await _make_pair({"channel_count": 4})
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
        driver, sim = await _make_pair()
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
