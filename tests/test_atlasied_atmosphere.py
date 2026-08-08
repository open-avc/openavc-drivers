"""Driver + simulator tests for atlasied_atmosphere (AZM4/AZM8 JSON-RPC).

No Atmosphere hardware on hand, so correctness is proven as a **dual-proof
round trip** wiring the real driver to the real simulator over an in-memory
transport that speaks newline-delimited JSON-RPC.

Covers the v1.3.0 first-class adoption:
  - liveness upgrade: the old keepalive sent `get KeepAlive` every 4 minutes
    and never looked at the reply — a device that stopped answering stayed
    shown online forever. The probe now awaits the getResp (correlated by
    param name); a subscription update does NOT satisfy it, and a silent
    device forces a reconnect with a typed no_response fault;
  - quick actions promote the one-shot paging actions.

And the v2.0.0 child-entity conversion:
  - config-sized rosters registered per type (sources / zones / mixes /
    groups / messages / routines / scenes / GPO presets / GPOs / bell
    schedules), reconciled when a count shrinks;
  - subscription updates route into child props; entity names land in the
    child `name` prop that feeds `label_field`;
  - commands take child_id params; set_gpo is gone (GpoState is read-only
    per the ATS006993 §6 parameter table) and the simulator ignores
    third-party sets on GpoState / ZoneGrouped.

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
DRIVER_PATH = REPO_ROOT / "audio" / "atlasied_atmosphere.py"
SIM_PATH = REPO_ROOT / "audio" / "atlasied_atmosphere_sim.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeBaseDriver(LifecycleFake):
    """Functional stand-in for the platform BaseDriver surface this driver
    uses: the hook-driven connect()/disconnect() lifecycle, the liveness
    watchdog, and the child-entity registry (integer-id validation
    included)."""

    DRIVER_INFO: dict = {}

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
        tdef = self.DRIVER_INFO.get("child_entity_types", {}).get(
            child_type)
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

    def set_child_state_batch(self, child_type, local_id, updates):
        for prop, value in updates.items():
            self.set_child_state(child_type, local_id, prop, value)

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
            port=self.config.get("port", 5321),
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


# Set by the pairing harness so the stubbed transport reaches the live sim.
_CURRENT_SIM: object | None = None
# When True, the transport processes requests but DROPS every reply — a
# silently-vanished device for the liveness tests.
_SWALLOW = False


class _FakeTCPTransport:
    def __init__(self, on_data, on_disconnect) -> None:
        self.on_data = on_data
        self.on_disconnect = on_disconnect
        self.connected = True
        self._sim = _CURRENT_SIM

    @classmethod
    async def create(cls, *, host, port, on_data, on_disconnect,
                     delimiter=None, timeout=5.0, name="", **kw):
        t = cls(on_data, on_disconnect)
        t._sim._clients["c1"] = t
        return t

    async def send(self, data) -> None:
        if not self.connected:
            raise ConnectionError("transport closed")
        for raw in bytes(data).split(b"\n"):
            raw = raw.strip()
            if not raw:
                continue
            resp = self._sim.handle_command(raw)
            if resp and not _SWALLOW:
                await self.on_data(resp)

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


DRV = _load("atmosphere_under_test", DRIVER_PATH)
SIM = _load("atmosphere_sim_under_test", SIM_PATH)


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(driver_overrides=None, sim_overrides=None):
    global _CURRENT_SIM, _SWALLOW
    _SWALLOW = False
    sim = SIM.AtlasIEDAtmosphereSimulator("sim1", sim_overrides or {})
    _CURRENT_SIM = sim

    cfg = {"host": "10.0.0.5", "port": 5321}
    cfg.update(driver_overrides or {})
    driver = DRV.AtlasIEDAtmosphereDriver(
        "azm1", cfg, _FakeState(), _FakeEvents())
    return driver, sim


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_metadata_and_actions_shape():
    info = DRV.AtlasIEDAtmosphereDriver.DRIVER_INFO
    assert info["version"] == "2.0.2"
    # The connection lifecycle hooks this driver overrides ship in 0.24.0.
    # The 0.25.0 floor is the package move: this file imports openavc.*.
    assert info["min_platform_version"] == "0.25.0"
    for cid in info["quick_actions"]:
        assert cid in info["commands"], cid
    assert {a["id"] for a in info["actions"]} == set(info["quick_actions"])
    # GpoState is read-only per ATS006993 §6 — no direct GPO set command.
    assert "set_gpo" not in info["commands"]


def test_child_types_and_pickers():
    info = DRV.AtlasIEDAtmosphereDriver.DRIVER_INFO
    types = info["child_entity_types"]
    assert set(types) == {
        "source", "zone", "mix", "group", "message", "routine", "scene",
        "gpo_preset", "gpo", "bell_schedule",
    }
    # Zero-based ids matching the device's message table indexing.
    for tdef in types.values():
        assert tdef["id_format"]["min"] == 0
    # Every child_id param points at a declared type.
    for cid, cmd in info["commands"].items():
        for pname, pdef in cmd.get("params", {}).items():
            if pdef.get("type") == "child_id":
                assert pdef["child_type"] in types, f"{cid}.{pname}"
    # Flat state is down to the two device-level vars — everything indexed
    # lives on children now (the config-count sizing fix).
    assert set(info["state_variables"]) == {
        "firmware_version", "todays_bell_schedule",
    }


# ── Topology registration ───────────────────────────────────────────────────

def test_roster_registration_counts_follow_config():
    async def scenario():
        driver, _sim = await _make_pair(
            {"num_zones": 4, "num_groups": 2, "num_gpos": 2})
        await driver.connect()
        try:
            assert driver.count_children("source") == 8
            assert driver.count_children("zone") == 4
            assert driver.count_children("mix") == 8
            assert driver.count_children("group") == 2
            assert driver.count_children("message") == 8
            assert driver.count_children("gpo") == 2
            assert driver.count_children("bell_schedule") == 4
            # Placeholder labels are zero-based like the message table.
            assert driver.get_child_state("zone", 0)["label"] == "Zone 0"
        finally:
            await driver.disconnect()
    _run(scenario())


def test_topology_reconciles_shrunk_roster():
    async def scenario():
        driver, _sim = await _make_pair({"num_zones": 4})
        # A leftover child from an earlier, larger configuration.
        driver.register_child("zone", 7)
        driver._register_topology()
        assert 7 not in driver.list_children("zone")
        assert driver.count_children("zone") == 4
    _run(scenario())


def test_project_label_not_overridden_by_placeholder():
    async def scenario():
        driver, _sim = await _make_pair()
        driver._project_child_entities = {
            "zone": {"00": {"label": "Dining Room"}}}
        driver._register_topology()
        # No placeholder seeded — the platform would apply the project
        # label; the stub default is "" when no initial label is passed.
        assert driver.get_child_state("zone", 0)["label"] == ""
        assert driver.get_child_state("zone", 1)["label"] == "Zone 1"
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


def test_disconnect_unblocks_inflight_probe_and_clears_buffer():
    """Teardown must fail a probe still awaiting its KeepAlive reply (so
    the health loop never hangs on a dead link) and drop any half-received
    line so a reconnect starts from a clean buffer."""
    async def scenario():
        global _SWALLOW
        driver, _sim = await _make_pair()
        await driver.connect()
        _SWALLOW = True
        try:
            probe = asyncio.ensure_future(driver._liveness_probe())
            await asyncio.sleep(0.05)
            assert not probe.done()
            # A partial line still sitting in the reassembly buffer.
            await driver.on_data_received(b'{"jsonrpc":"2.0","meth')
            assert driver._line_buffer
            await driver.disconnect()
            assert driver._line_buffer == b""
            assert driver._probe_fut is None
            try:
                await asyncio.wait_for(probe, 1.0)
            except ConnectionError:
                return
            raise AssertionError("probe survived disconnect")
        finally:
            _SWALLOW = False
    _run(scenario())


def test_connect_populates_children_from_sim():
    async def scenario():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            assert driver._connected
            assert driver.get_state("firmware_version")
            # Subscribe answers with the sim's initial values.
            zone0 = driver.get_child_state("zone", 0)
            assert zone0["name"] == "Zone 1"
            assert zone0["gain"] == -10.0
            assert zone0["mute"] is False
            assert zone0["source"] == 0
            assert driver.get_child_state("message", 2)["name"] == "Message 3"
            assert driver.get_child_state("gpo", 0)["state"] is False
        finally:
            await driver.disconnect()
    _run(scenario())


def test_command_round_trip_updates_child_state():
    async def scenario():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command(
                "set_zone_gain", {"zone": 3, "gain_db": -25.0})
            assert sim._values["ZoneGain_3"] == -25.0
            assert driver.get_child_state("zone", 3)["gain"] == -25.0

            await driver.send_command(
                "set_source_mute", {"source": 1, "mute": True})
            assert sim._values["SourceMute_1"] == 1
            assert driver.get_child_state("source", 1)["mute"] is True

            await driver.send_command(
                "set_group_active", {"group": 0, "active": True})
            assert sim._values["GroupActive_0"] == 1
            assert driver.get_child_state("group", 0)["active"] is True

            await driver.send_command(
                "set_zone_source", {"zone": 2, "source": 5})
            assert sim._values["ZoneSource_2"] == 5
            assert driver.get_child_state("zone", 2)["source"] == 5

            await driver.send_command("clear_zone_source", {"zone": 2})
            assert sim._values["ZoneSource_2"] == -1
            assert driver.get_child_state("zone", 2)["source"] == -1
        finally:
            await driver.disconnect()
    _run(scenario())


def test_sim_ignores_set_on_read_only_params():
    async def scenario():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            # GpoState / ZoneGrouped / names are read-only per ATS006993
            # §6 — a third-party set must not change the sim's value.
            for param, value in (("GpoState_0", 1), ("ZoneGrouped_0", 1),
                                 ("ZoneName_0", "Hacked")):
                before = sim._values[param]
                await driver._send_set(param, "val", value)
                assert sim._values[param] == before, param
        finally:
            await driver.disconnect()
    _run(scenario())


# ── Liveness ────────────────────────────────────────────────────────────────

def test_probe_resolves_on_keepalive_reply():
    async def scenario():
        driver, _sim = await _make_pair()
        await driver.connect()
        try:
            await asyncio.wait_for(driver._liveness_probe(), 1.0)
        finally:
            await driver.disconnect()
    _run(scenario())


def test_probe_times_out_when_silent():
    """The pre-1.3.0 keepalive fired and forgot — a device that stopped
    answering was never noticed. The awaited probe must raise instead."""
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


def test_subscription_update_does_not_satisfy_probe():
    async def scenario():
        global _SWALLOW
        driver, _sim = await _make_pair()
        await driver.connect()
        try:
            _SWALLOW = True  # sim replies dropped; we inject lines by hand
            probe = asyncio.ensure_future(driver._liveness_probe())
            await asyncio.sleep(0.05)
            assert not probe.done()
            await driver.on_data_received(
                b'{"jsonrpc":"2.0","method":"update",'
                b'"params":{"param":"ZoneGain_0","val":-12.0}}\n')
            await asyncio.sleep(0.05)
            assert not probe.done()
            assert driver.get_child_state("zone", 0)["gain"] == -12.0
            await driver.on_data_received(
                b'{"jsonrpc":"2.0","method":"getResp",'
                b'"params":[{"param":"KeepAlive","str":"OK"}]}\n')
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
        driver._stop_health_loop()  # restart below with test-speed cadence
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
            assert "KeepAlive" in message
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
            # Device-side change while our updates were lost: mutate the
            # sim store directly (no push), then refresh via `get`.
            sim._values["MixGain_1"] = -30.0
            assert driver.get_child_state("mix", 1)["gain"] == -10.0
            result = await driver.refresh_children()
            assert result["mix"] == 8
            assert driver.get_child_state("mix", 1)["gain"] == -30.0
        finally:
            await driver.disconnect()
    _run(scenario())
