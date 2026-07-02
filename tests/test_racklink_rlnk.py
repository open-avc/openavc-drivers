"""Driver + simulator tests for racklink_rlnk (RackLink PDU).

There is no RackLink hardware on hand, so correctness is proven two ways:
metadata/shape assertions on the driver, and a **dual-proof round trip** that
wires the real driver to the real simulator over an in-memory transport — the
simulator renders the binary RackLink frames, the driver parses them, and the
results are asserted on both sides. This substitutes for a hardware fixture
(same approach as test_chazy_control_sim.py).

Covers the v1.3.0 first-class adoption:
  - child entities: outlets / contacts registered + reconciled from the
    device's counts, status/name dispatch routed into child state;
  - child_id command params + coercion;
  - connection-fault: a rejected login raises an auth-worded ConnectionError;
  - liveness: the awaited probe detects a silent device and forces reconnect.

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

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "power" / "racklink_rlnk.py"
SIM_PATH = REPO_ROOT / "power" / "racklink_rlnk_sim.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeState:
    def __init__(self) -> None:
        self.data: dict = {}

    def set(self, key, value, **_):
        self.data[key] = value


class _FakeEvents:
    def __init__(self) -> None:
        self.emitted: list[str] = []

    async def emit(self, name, *args, **kwargs):
        self.emitted.append(name)


class _FakeConnectionFaultError(ConnectionError):
    """Stand-in for the platform's typed connection fault (0.22.0+)."""

    def __init__(self, message: str = "", *, code: str) -> None:
        super().__init__(message)
        self.fault_code = code


class _FakeBaseDriver:
    """Functional stand-in for the platform BaseDriver child API."""

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._children: dict[str, dict[int, dict]] = {}
        self._connected = False
        self.disconnect_calls = 0
        self.stashed_fault: tuple[str, str] | None = None

    def _eff_schema(self, ctype: str) -> dict:
        schema = dict(self.DRIVER_INFO["child_entity_types"][ctype]["state_variables"])
        schema.setdefault("online", {"type": "boolean"})
        schema.setdefault("label", {"type": "string"})
        return schema

    def get_child_entity_types(self) -> dict:
        out = {}
        for ct, d in self.DRIVER_INFO.get("child_entity_types", {}).items():
            md = dict(d)
            md["state_variables"] = self._eff_schema(ct)
            out[ct] = md
        return out

    def register_child(self, ctype, lid, initial_state=None) -> None:
        bucket = self._children.setdefault(ctype, {})
        if lid in bucket:
            return  # idempotent — does not re-init existing state
        schema = self._eff_schema(ctype)
        ov = dict(initial_state or {})
        for prop in ov:
            if prop not in schema:
                raise ValueError(f"unknown child prop {prop!r}")
        st: dict = {}
        for prop in schema:
            if prop == "online":
                st[prop] = ov.get("online", True)
            elif prop == "label":
                st[prop] = ov.get("label", "")
            else:
                st[prop] = ov.get(prop)
        bucket[lid] = st

    def deregister_child(self, ctype, lid) -> None:
        self._children.get(ctype, {}).pop(lid, None)

    def is_child_registered(self, ctype, lid) -> bool:
        return lid in self._children.get(ctype, {})

    def list_children(self, ctype) -> list:
        return sorted(self._children.get(ctype, {}).keys())

    def get_child_state(self, ctype, lid) -> dict:
        return dict(self._children.get(ctype, {}).get(lid, {}))

    def set_child_state(self, ctype, lid, prop, value) -> None:
        schema = self._eff_schema(ctype)
        if prop not in schema:
            raise ValueError(f"unknown child prop {prop!r}")
        if lid not in self._children.get(ctype, {}):
            raise ValueError(f"child {ctype}/{lid} not registered")
        self._children[ctype][lid][prop] = value

    def set_child_state_batch(self, ctype, lid, updates) -> None:
        schema = self._eff_schema(ctype)
        for prop in updates:
            if prop not in schema:
                raise ValueError(f"unknown child prop {prop!r}")
        if lid not in self._children.get(ctype, {}):
            raise ValueError(f"child {ctype}/{lid} not registered")
        self._children[ctype][lid].update(updates)

    def set_state(self, key, value) -> None:
        self.state.set(key, value)

    def get_state(self, key, default=None):
        return self.state.data.get(key, default)

    def _handle_transport_disconnect(self) -> None:
        self.disconnect_calls += 1
        if self.transport is not None:
            self.transport.connected = False

    def _stash_fault(self, code, message="") -> None:
        self.stashed_fault = (code, message)

    async def start_polling(self, interval) -> None:
        pass

    async def stop_polling(self) -> None:
        pass


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

    async def push_to(self, client_id, data) -> None:  # overridden per pairing
        pass


# Set by the pairing harness so the stubbed transport reaches the live sim.
_CURRENT_SIM: object | None = None
# When True, the transport sends to the sim but DROPS the reply — simulating a
# silently-vanished device for the liveness test.
_SWALLOW = False


class _FakeTCPTransport:
    def __init__(self, on_data, on_disconnect) -> None:
        self.on_data = on_data
        self.on_disconnect = on_disconnect
        self.connected = True
        self.last_error = None
        self._sim = _CURRENT_SIM

    @classmethod
    async def create(cls, *, host, port, on_data, on_disconnect,
                     delimiter=None, timeout=5.0, name=""):
        t = cls(on_data, on_disconnect)

        async def _push(client_id, data):
            await t.on_data(data)

        t._sim.push_to = _push  # route sim pushes back into the driver
        return t

    async def send(self, data) -> None:
        if not self.connected:
            raise ConnectionError("transport closed")
        resp = self._sim.handle_command(bytes(data))
        if resp and not _SWALLOW:
            await self.on_data(resp)

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
    base.ConnectionFaultError = _FakeConnectionFaultError
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


DRV = _load("racklink_under_test", DRIVER_PATH)
SIM = _load("racklink_sim_under_test", SIM_PATH)


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(sim_config=None, driver_overrides=None):
    global _CURRENT_SIM, _SWALLOW
    _SWALLOW = False
    sim = SIM.RackLinkRLNKSimulator("sim1", sim_config or {})
    sim._clients["c1"] = object()
    await sim.on_client_connected("c1")
    _CURRENT_SIM = sim

    cfg = {
        "host": "10.0.0.9",
        "port": 60000,
        "username": "user",
        "password": "pw",
        "poll_interval": 0,
    }
    cfg.update(driver_overrides or {})
    driver = DRV.RackLinkRLNKDriver("pdu1", cfg, _FakeState(), _FakeEvents())
    return driver, sim


async def _settle(n: int = 4) -> None:
    # Let scheduled push tasks (asyncio.create_task in the sim) run.
    for _ in range(n):
        await asyncio.sleep(0)


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_version_bumped():
    assert DRV.RackLinkRLNKDriver.DRIVER_INFO["version"] == "1.3.1"


def test_child_entity_types_declared():
    types = DRV.RackLinkRLNKDriver.DRIVER_INFO["child_entity_types"]
    assert set(types) == {"outlet", "contact"}
    outlet = types["outlet"]
    assert outlet["id_format"] == {"type": "integer", "min": 1, "max": 16, "pad_width": 2}
    # state is the operational live value (high tier); name/controllable low.
    assert outlet["state_variables"]["state"]["cloud_priority"] == "high"
    assert outlet["state_variables"]["name"]["cloud_priority"] == "low"
    assert outlet["state_variables"]["controllable"]["cloud_priority"] == "low"
    assert types["contact"]["state_variables"]["state"]["cloud_priority"] == "high"
    # reserved props must NOT be declared by the driver.
    for ctype in types.values():
        assert "online" not in ctype["state_variables"]
        assert "label" not in ctype["state_variables"]


def test_flat_outlet_keys_removed():
    sv = DRV.RackLinkRLNKDriver.DRIVER_INFO["state_variables"]
    assert not any(k.startswith("outlet_") and k.endswith("_state") for k in sv)
    assert not any(k.startswith("contact_") and k.endswith("_state") for k in sv)
    # device-level telemetry stays flat.
    assert "voltage_rms" in sv and "outlet_count" in sv


def test_outlet_commands_use_child_id():
    cmds = DRV.RackLinkRLNKDriver.DRIVER_INFO["commands"]
    for cmd in ("outlet_on", "outlet_off", "outlet_cycle", "set_outlet_name"):
        assert cmds[cmd]["params"]["outlet"]["type"] == "child_id"
        assert cmds[cmd]["params"]["outlet"]["child_type"] == "outlet"
    for cmd in ("contact_open", "contact_close", "set_contact_name"):
        assert cmds[cmd]["params"]["contact"]["type"] == "child_id"
        assert cmds[cmd]["params"]["contact"]["child_type"] == "contact"


def test_quick_actions_reference_real_commands():
    info = DRV.RackLinkRLNKDriver.DRIVER_INFO
    cmds = set(info["commands"])
    for qa in info["quick_actions"]:
        assert qa in cmds, f"quick_action {qa} is not a real command"


# ── Round-trip: connect registers + populates children ──────────────────────

def test_connect_registers_children_from_counts():
    async def go():
        driver, sim = await _make_pair(sim_config={"outlets": 8, "contacts": 4})
        await driver.connect()
        try:
            assert driver.list_children("outlet") == list(range(1, 9))
            assert driver.list_children("contact") == list(range(1, 5))
            assert driver.get_state("outlet_count") == 8
            assert driver.get_state("contact_count") == 4
            # names came back from the sim into child state.
            assert driver.get_child_state("outlet", 1)["name"] == "Outlet 1"
            assert driver.get_child_state("outlet", 1)["controllable"] is True
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_reconcile_smaller_unit_registers_fewer_children():
    async def go():
        driver, sim = await _make_pair(sim_config={"outlets": 4, "contacts": 0})
        await driver.connect()
        try:
            assert driver.list_children("outlet") == [1, 2, 3, 4]
            assert driver.list_children("contact") == []
            assert driver.get_state("outlet_count") == 4
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Round-trip: commands mutate the sim and the driver's child state ─────────

def test_outlet_on_off_round_trip():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command("outlet_on", {"outlet": 3})
            await _settle()
            assert sim._outlet_state[2] == SIM.OUTLET_ON
            assert driver.get_child_state("outlet", 3)["state"] is True

            await driver.send_command("outlet_off", {"outlet": 3})
            await _settle()
            assert sim._outlet_state[2] == SIM.OUTLET_OFF
            assert driver.get_child_state("outlet", 3)["state"] is False
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_child_id_padded_string_coerced():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            # The IDE child picker can hand back a zero-padded id ("05").
            await driver.send_command("outlet_on", {"outlet": "05"})
            await _settle()
            assert sim._outlet_state[4] == SIM.OUTLET_ON
            assert driver.get_child_state("outlet", 5)["state"] is True
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_rename_outlet_round_trip():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command(
                "set_outlet_name", {"outlet": 2, "name": "Projector"}
            )
            await _settle()
            assert sim._outlet_name[1] == "Projector"
            assert driver.get_child_state("outlet", 2)["name"] == "Projector"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_unsolicited_push_updates_child_state():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            assert driver.get_child_state("outlet", 6)["state"] is False
            sim.trigger_outlet_push(6, True)  # device-side change
            await _settle()
            assert driver.get_child_state("outlet", 6)["state"] is True
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Connection-fault: rejected login → auth-worded ConnectionError ──────────

def test_rejected_login_raises_auth_error():
    async def go():
        driver, sim = await _make_pair(sim_config={"reject_auth": True})
        with pytest.raises(ConnectionError) as ei:
            await driver.connect()
        # The typed fault code maps straight to offline_reason=auth_failed.
        assert ei.value.fault_code == "auth_failed"
        assert "authentication failed" in str(ei.value).lower()

    asyncio.run(go())


def test_login_timeout_raises_no_response_fault():
    """A device that accepts TCP but never answers the login request is a
    no_response fault ('is this really a RackLink?'), not auth — and not the
    generic 'connection dropped' the old wording fell into."""
    async def go():
        global _SWALLOW
        driver, sim = await _make_pair()
        old_timeout = DRV.LOGIN_TIMEOUT_S
        DRV.LOGIN_TIMEOUT_S = 0.05
        try:
            _SWALLOW = True  # login reply never arrives
            with pytest.raises(ConnectionError) as ei:
                await driver.connect()
            assert ei.value.fault_code == "no_response"
        finally:
            _SWALLOW = False
            DRV.LOGIN_TIMEOUT_S = old_timeout

    asyncio.run(go())


# ── Liveness: awaited probe detects a silent device + forces reconnect ───────

def test_health_probe_succeeds_when_alive():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver._probe_once()  # responds → no raise
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_health_loop_forces_reconnect_on_silent_device():
    async def go():
        global _SWALLOW
        driver, sim = await _make_pair()
        await driver.connect()
        # Speed the watchdog up and make the device go silent.
        DRV.KEEPALIVE_INTERVAL_S = 0.01
        DRV.KEEPALIVE_TIMEOUT_S = 0.05
        try:
            _SWALLOW = True  # device stops replying
            driver._start_health_loop()
            await asyncio.sleep(0.3)
            assert driver.disconnect_calls >= 1
            # The typed no_response fault is stashed for the classifier —
            # this disconnect has no exception to carry the cause.
            assert driver.stashed_fault is not None
            assert driver.stashed_fault[0] == "no_response"
        finally:
            _SWALLOW = False
            DRV.KEEPALIVE_INTERVAL_S = 30.0
            DRV.KEEPALIVE_TIMEOUT_S = 5.0
            driver._stop_health_loop()

    asyncio.run(go())
