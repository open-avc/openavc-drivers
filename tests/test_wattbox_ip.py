"""Driver + simulator tests for wattbox_ip (SnapAV WattBox PDU).

There is no WattBox hardware on hand, so correctness is proven two ways:
metadata/shape assertions on the driver, and a **dual-proof round trip** that
wires the real driver to the real simulator over an in-memory transport — the
simulator renders the WattBox Integration Protocol text, the driver parses it,
and the results are asserted on both sides. This substitutes for a hardware
fixture (same approach as test_racklink_rlnk.py).

Covers the v1.3.0 first-class adoption:
  - child entities: outlets registered + reconciled from the device's outlet
    count, status / name / metering dispatch routed into child state;
  - child_id command params + coercion;
  - device settings: auto-reboot writes + reads back through the pending-queue
    state_key;
  - connection-fault: a rejected login raises an auth-worded ConnectionError;
  - liveness: the awaited probe detects a silent device and forces reconnect.

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

import pytest

from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    ConnectionFaultError as _FakeConnectionFaultError,
    StubEvents as _FakeEvents,
    StubState as _FakeState,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "power" / "wattbox_ip.py"
SIM_PATH = REPO_ROOT / "power" / "wattbox_ip_sim.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeBaseDriver(LifecycleFake):
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
        self._health_task = None
        self._health_failures = 0
        self._bg_tasks: set = set()

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

    def set_states(self, updates) -> None:
        for k, v in updates.items():
            self.state.set(k, v)

    def get_state(self, key, default=None):
        return self.state.data.get(key, default)

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

    # -- liveness watchdog (mirrors the platform BaseDriver: probe every
    # HEALTH_INTERVAL_S under a HEALTH_TIMEOUT_S deadline; HEALTH_MAX_FAILURES
    # misses force a disconnect with a typed no_response fault) --

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
            port=self.config.get("port", 23),
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
                     delimiter=None, frame_parser=None,
                     inter_command_delay=0.0, timeout=5.0, name=""):
        t = cls(on_data, on_disconnect)

        async def _push(client_id, data):
            await t.on_data(data)

        t._sim.push_to = _push  # route sim pushes back into the driver
        # WattBox is server-initiated: it sends the login banner the moment
        # the socket opens, before any command. Deliver it after create()
        # returns (so connect() has assigned self.transport and is awaiting
        # the login result), mimicking the real transport delivering data on
        # the event loop.
        banner = t._sim.connect_banner()
        if banner:
            asyncio.ensure_future(t._deliver_initial(banner))
        return t

    async def _deliver_initial(self, banner) -> None:
        await asyncio.sleep(0)
        if self.connected:
            await self.on_data(banner)

    async def send(self, data) -> None:
        if not self.connected:
            raise ConnectionError("transport closed")
        resp = self._sim.handle_command(bytes(data))
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
    base.ConnectionFaultError = _FakeConnectionFaultError
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


DRV = _load("wattbox_under_test", DRIVER_PATH)
SIM = _load("wattbox_sim_under_test", SIM_PATH)


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(sim_config=None, driver_overrides=None):
    global _CURRENT_SIM, _SWALLOW
    _SWALLOW = False
    sim = SIM.WattBoxIPSimulator("sim1", sim_config or {})
    sim._clients["c1"] = object()
    await sim.on_client_connected("c1")
    _CURRENT_SIM = sim

    cfg = {
        "host": "10.0.0.5",
        "port": 23,
        "username": "wattbox",
        "password": "wb",
        "poll_interval": 0,
    }
    cfg.update(driver_overrides or {})
    driver = DRV.WattBoxIPDriver("pdu1", cfg, _FakeState(), _FakeEvents())
    return driver, sim


async def _settle(n: int = 4) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_version_bumped():
    assert DRV.WattBoxIPDriver.DRIVER_INFO["version"] == "1.3.4"


def test_child_entity_types_declared():
    types = DRV.WattBoxIPDriver.DRIVER_INFO["child_entity_types"]
    assert set(types) == {"outlet"}
    outlet = types["outlet"]
    # WattBox tops out at 18 outlets (WB-800-IPVM-18 / 800VS-IPVM-18).
    assert outlet["id_format"] == {"type": "integer", "min": 1, "max": 18, "pad_width": 2}
    # state is the operational live value (high tier); name/metering low.
    assert outlet["state_variables"]["state"]["cloud_priority"] == "high"
    assert outlet["state_variables"]["name"]["cloud_priority"] == "low"
    assert outlet["state_variables"]["power"]["cloud_priority"] == "low"
    # reserved props must NOT be declared by the driver.
    assert "online" not in outlet["state_variables"]
    assert "label" not in outlet["state_variables"]


def test_flat_outlet_keys_removed():
    sv = DRV.WattBoxIPDriver.DRIVER_INFO["state_variables"]
    assert not any(k.startswith("outlet_") and k != "outlet_count" for k in sv)
    # device-level telemetry stays flat.
    assert "outlet_count" in sv
    assert "power_watts" in sv
    assert "auto_reboot" in sv


def test_outlet_commands_use_child_id():
    cmds = DRV.WattBoxIPDriver.DRIVER_INFO["commands"]
    for cmd in (
        "outlet_on", "outlet_off", "outlet_toggle", "outlet_reset",
        "set_outlet_name", "set_outlet_power_on_delay", "set_outlet_mode",
    ):
        assert cmds[cmd]["params"]["outlet"]["type"] == "child_id"
        assert cmds[cmd]["params"]["outlet"]["child_type"] == "outlet"
    # reset_all is the parameterless "reset every outlet" surface.
    assert cmds["reset_all"]["params"] == {}


def test_device_settings_declared():
    info = DRV.WattBoxIPDriver.DRIVER_INFO
    ds = info["device_settings"]
    # Auto-reboot is the only real read-back setting (v1.7 has ?AutoReboot).
    assert set(ds) == {"auto_reboot"}
    assert ds["auto_reboot"]["state_key"] == "auto_reboot"
    assert ds["auto_reboot"]["type"] == "boolean"
    # Power-on delay / outlet mode are SET-only in v1.7 — they must stay
    # transient commands, never device settings.
    assert "set_outlet_power_on_delay" in info["commands"]
    assert "set_outlet_mode" in info["commands"]


def test_quick_actions_reference_real_commands():
    info = DRV.WattBoxIPDriver.DRIVER_INFO
    cmds = set(info["commands"])
    for qa in info["quick_actions"]:
        assert qa in cmds, f"quick_action {qa} is not a real command"


# ── Round-trip: connect registers + reconciles children ─────────────────────

def test_connect_registers_children_from_count():
    async def go():
        driver, sim = await _make_pair(sim_config={"outlets": 8})
        await driver.connect()
        try:
            assert driver.list_children("outlet") == list(range(1, 9))
            assert driver.get_state("outlet_count") == 8
            # names + states came back from the sim into child state.
            assert driver.get_child_state("outlet", 1)["name"] == "Outlet 1"
            assert driver.get_child_state("outlet", 1)["state"] is True
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_reconcile_smaller_unit_registers_fewer_children():
    async def go():
        driver, sim = await _make_pair(sim_config={"outlets": 3})
        await driver.connect()
        try:
            assert driver.list_children("outlet") == [1, 2, 3]
            assert driver.get_state("outlet_count") == 3
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_per_outlet_metering_routes_into_child_state():
    async def go():
        driver, sim = await _make_pair(sim_config={"outlets": 4})
        await driver.connect()
        try:
            # Sim formula: an ON outlet n reports watts = 50 + n*5, 120 V.
            o1 = driver.get_child_state("outlet", 1)
            assert o1["power"] == 55.0
            assert o1["voltage"] == 120.0
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Round-trip: commands mutate the sim and the driver's child state ─────────

def test_outlet_on_off_round_trip():
    async def go():
        driver, sim = await _make_pair(sim_config={"outlets": 8})
        await driver.connect()
        try:
            # WattBox SET returns only OK (no status echo), so the driver
            # confirms the new state on the next poll — verify both halves.
            await driver.send_command("outlet_off", {"outlet": 3})
            await _settle()
            assert sim._outlet_state[2] is False
            await driver.poll()
            await _settle()
            assert driver.get_child_state("outlet", 3)["state"] is False

            await driver.send_command("outlet_on", {"outlet": 3})
            await _settle()
            assert sim._outlet_state[2] is True
            await driver.poll()
            await _settle()
            assert driver.get_child_state("outlet", 3)["state"] is True
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_child_id_padded_string_coerced():
    async def go():
        driver, sim = await _make_pair(sim_config={"outlets": 8})
        await driver.connect()
        try:
            # The IDE child picker can hand back a zero-padded id ("05").
            await driver.send_command("outlet_off", {"outlet": "05"})
            await _settle()
            assert sim._outlet_state[4] is False
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_rename_outlet_round_trip():
    async def go():
        driver, sim = await _make_pair(sim_config={"outlets": 8})
        await driver.connect()
        try:
            await driver.send_command(
                "set_outlet_name", {"outlet": 2, "name": "Projector"}
            )
            await _settle()
            assert sim._outlet_name[1] == "Projector"
            await driver.poll()
            await _settle()
            assert driver.get_child_state("outlet", 2)["name"] == "Projector"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_reset_all_resets_every_outlet():
    async def go():
        driver, sim = await _make_pair(sim_config={"outlets": 4})
        await driver.connect()
        try:
            # Knock a couple off, then reset_all should turn them all on.
            sim._outlet_state[0] = False
            sim._outlet_state[1] = False
            await driver.send_command("reset_all")
            await _settle()
            assert all(sim._outlet_state[:4])
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Device settings: auto-reboot write + read-back ──────────────────────────

def test_auto_reboot_device_setting_round_trip():
    async def go():
        driver, sim = await _make_pair(sim_config={"outlets": 4})
        await driver.connect()
        try:
            assert driver.get_state("auto_reboot") is True  # sim default
            await driver.set_device_setting("auto_reboot", False)
            await _settle()
            assert sim._auto_reboot is False
            # Read-back: the polled ?AutoReboot must repopulate the state_key.
            await driver.poll()
            await _settle()
            assert driver.get_state("auto_reboot") is False
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
    """A device that accepts TCP but never completes the login handshake is
    a no_response fault ('is this really a WattBox?'), not auth — and not
    the generic 'connection dropped' the old wording fell into."""
    async def go():
        global _SWALLOW
        driver, sim = await _make_pair()
        old_timeout = DRV.LOGIN_TIMEOUT_S
        DRV.LOGIN_TIMEOUT_S = 0.05
        try:
            _SWALLOW = True  # login replies never arrive
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
        driver, sim = await _make_pair(sim_config={"outlets": 4})
        await driver.connect()
        try:
            await driver._liveness_probe()  # responds → no raise
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_health_loop_forces_reconnect_on_silent_device():
    async def go():
        global _SWALLOW
        driver, sim = await _make_pair(sim_config={"outlets": 4})
        await driver.connect()
        # connect() auto-started the watchdog at production cadence; restart
        # it at test speed (instance attrs; the loop reads self.HEALTH_*)
        # and make the device go silent.
        driver._stop_health_loop()
        driver.HEALTH_INTERVAL_S = 0.01
        driver.HEALTH_TIMEOUT_S = 0.05
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
            driver._stop_health_loop()

    asyncio.run(go())
