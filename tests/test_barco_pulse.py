"""Driver + simulator tests for barco_pulse (Barco Pulse projectors).

No Pulse hardware on hand, so correctness is proven two ways: metadata /
shape assertions on the driver, and **dual-proof round trips** that wire
the real driver to the real simulator over an in-memory transport. The
simulator renders Pulse JSON-RPC (no framing delimiter), every reply and
push is split by the driver's own brace-balancing frame parser, and
results are asserted on both sides.

Covers the protocol essentials from the Pulse API catalogs:
  - id-correlated request/response (batched identity read, primed state);
  - property.subscribe + property.changed push fan-out (including pushes
    triggered from the Simulator UI's set_state path);
  - the documented power state machine walked via push, not polling;
  - optional authenticate (accepted / rejected -> typed auth_failed);
  - dynamic-API tolerance (absent properties and lens methods);
  - Wake-on-LAN assist when powering on from ECO;
  - device settings write -> sim mutates -> read-back via push and poll;
  - liveness: the awaited property.get probe resolves while the device
    answers and forces a typed no_response reconnect when it goes silent;
  - the offline Test Connection wizard against a real in-test TCP server.

Loads the driver + simulator with the ``openavc.*``
imports stubbed so the community CI stays self-contained (conftest.py
rolls the stubs back after this module is collected; an autouse fixture
re-installs them for each test because the driver imports UDPTransport
lazily inside ``_send_wol``).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    ConnectionFaultError as _FakeConnectionFaultError,
    FrameParser as _FrameParserBase,
    StubEvents as _FakeEvents,
    StubState as _FakeState,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "projectors" / "barco_pulse.py"
SIM_PATH = REPO_ROOT / "projectors" / "barco_pulse_sim.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeBaseDriver(LifecycleFake):
    """Functional stand-in for the platform BaseDriver surface this driver
    uses, including the liveness watchdog loop (mirrors base.py)."""

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False
        self._last_transport_error = ""
        self._last_fault = None
        self.disconnect_calls = 0
        self.stashed_fault: tuple[str, str] | None = None
        self.config_updates: list[dict] = []
        self.reconnect_requests = 0
        self._health_task = None
        self._health_failures = 0
        self._bg_tasks: set = set()

    def set_state(self, key, value) -> None:
        self.state.set(f"device.{self.device_id}.{key}", value)

    def get_state(self, key, default=None):
        return self.state.data.get(f"device.{self.device_id}.{key}", default)

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
            port=self.config.get("port", 9090),
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
        self._last_transport_error = ""
        self._last_fault = None
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

    async def request_config_update(self, delta) -> None:
        self.config_updates.append(dict(delta))
        self.config.update(delta)

    async def request_reconnect(self) -> None:
        self.reconnect_requests += 1


# Set by the pairing harness so the stubbed transport reaches the live sim.
_CURRENT_SIM: object | None = None
_CURRENT_TRANSPORT: object | None = None
# When True, the transport sends to the sim but DROPS the reply — simulating a
# silently-vanished projector for the liveness test.
_SWALLOW = False


class _FakeTCPTransport:
    def __init__(self, on_data, on_disconnect, frame_parser) -> None:
        self.on_data = on_data
        self.on_disconnect = on_disconnect
        self.parser = frame_parser
        self.connected = True
        self._sim = _CURRENT_SIM

    @classmethod
    async def create(cls, *, host, port, on_data, on_disconnect,
                     delimiter=None, frame_parser=None,
                     inter_command_delay=0.0, timeout=5.0, name=""):
        global _CURRENT_TRANSPORT
        inst = cls(on_data, on_disconnect, frame_parser)
        _CURRENT_TRANSPORT = inst
        return inst

    async def deliver(self, data: bytes) -> None:
        """Feed sim output through the driver's own frame parser."""
        if not self.connected or _SWALLOW:
            return
        for frame in self.parser.feed(data):
            await self.on_data(frame)

    async def send(self, data) -> None:
        if not self.connected:
            raise ConnectionError("transport closed")
        resp = self._sim.handle_command(bytes(data))
        if resp:
            await self.deliver(resp)

    async def close(self) -> None:
        self.connected = False


class _FakeUDPTransport:
    sent: list[tuple[bytes, str, int]] = []

    def __init__(self, name="") -> None:
        self.name = name

    async def open(self, allow_broadcast=False) -> None:
        self.allow_broadcast = allow_broadcast

    async def send(self, data, host, port) -> None:
        _FakeUDPTransport.sent.append((bytes(data), host, port))

    def close(self) -> None:
        pass


class _FakeTCPSimulator:
    """Stand-in for openavc.simulator.tcp_simulator.TCPSimulator."""

    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self._state = dict(self.SIMULATOR_INFO.get("initial_state", {}))
        self._error_modes = dict(self.SIMULATOR_INFO.get("error_modes", {}))
        self._active_errors: set = set()

    @property
    def state(self) -> dict:
        return self._state

    def set_state(self, key, value) -> None:
        if self._state.get(key) == value:
            return
        self._state[key] = value

    def get_state(self, key, default=None):
        return self._state.get(key, default)

    def inject_error(self, mode: str) -> None:
        self._active_errors.add(mode)
        for key, value in self._error_modes[mode].get(
                "set_state", {}).items():
            self.set_state(key, value)

    async def push(self, data: bytes) -> None:
        if _CURRENT_TRANSPORT is not None:
            await _CURRENT_TRANSPORT.deliver(data)


def _build_stub_modules() -> dict[str, ModuleType]:
    modules: dict[str, ModuleType] = {}
    server = ModuleType("openavc")
    server.__path__ = []  # type: ignore[attr-defined]
    modules["openavc"] = server
    for sub in ("drivers", "transport", "utils"):
        m = ModuleType(f"openavc.{sub}")
        m.__path__ = []  # type: ignore[attr-defined]
        modules[f"openavc.{sub}"] = m
    base = ModuleType("openavc.drivers.base")
    base.BaseDriver = _FakeBaseDriver
    base.ConnectionFaultError = _FakeConnectionFaultError
    modules["openavc.drivers.base"] = base
    tcp = ModuleType("openavc.transport.tcp")
    tcp.TCPTransport = _FakeTCPTransport
    modules["openavc.transport.tcp"] = tcp
    udp = ModuleType("openavc.transport.udp")
    udp.UDPTransport = _FakeUDPTransport
    modules["openavc.transport.udp"] = udp
    parsers = ModuleType("openavc.transport.frame_parsers")
    parsers.FrameParser = _FrameParserBase
    modules["openavc.transport.frame_parsers"] = parsers
    logger = ModuleType("openavc.utils.logger")
    logger.get_logger = lambda name="x": logging.getLogger(name)
    modules["openavc.utils.logger"] = logger

    sim_pkg = ModuleType("openavc.simulator")
    sim_pkg.__path__ = []  # type: ignore[attr-defined]
    modules["openavc.simulator"] = sim_pkg
    sim_tcp = ModuleType("openavc.simulator.tcp_simulator")
    sim_tcp.TCPSimulator = _FakeTCPSimulator
    modules["openavc.simulator.tcp_simulator"] = sim_tcp
    return modules


_STUB_MODULES = _build_stub_modules()


def _load(name: str, path: Path) -> ModuleType:
    for mod_name, mod in _STUB_MODULES.items():
        sys.modules[mod_name] = mod
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


DRV = _load("barco_pulse_under_test", DRIVER_PATH)
SIM = _load("barco_pulse_sim_under_test", SIM_PATH)


@pytest.fixture(autouse=True)
def _stub_platform_modules(monkeypatch):
    """Re-install the platform stubs for each test's runtime.

    The driver imports UDPTransport lazily inside _send_wol(), which
    resolves through sys.modules at call time — after conftest.py has
    rolled the collection-time stubs back. monkeypatch restores the
    originals after each test.
    """
    for name, mod in _STUB_MODULES.items():
        monkeypatch.setitem(sys.modules, name, mod)
    _FakeUDPTransport.sent = []


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(sim_config=None, driver_overrides=None):
    global _CURRENT_SIM, _CURRENT_TRANSPORT, _SWALLOW
    _SWALLOW = False
    _CURRENT_TRANSPORT = None
    cfg = {"transition_delay": 0}
    cfg.update(sim_config or {})
    sim = SIM.BarcoPulseSimulator("sim1", cfg)
    _CURRENT_SIM = sim

    dcfg = {
        "host": "10.0.0.80",
        "port": 9090,
        "auth_code": "",
        "poll_interval": 0,
    }
    dcfg.update(driver_overrides or {})
    driver = DRV.BarcoPulseDriver("pj1", dcfg, _FakeState(), _FakeEvents())
    return driver, sim


async def _settle(n: int = 4) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_version_and_platform_gate():
    info = DRV.BarcoPulseDriver.DRIVER_INFO
    assert info["version"] == "1.0.1"
    # The BaseDriver connection lifecycle hooks this driver overrides
    # (_pre_connect / _post_connect / _initial_sync / _close_session)
    # ship in 0.24.0.
    assert info["min_platform_version"] == "0.24.0"
    assert info["ports"] == [9090]


def test_probe_answer_coherence():
    """The declared tcp_probe's send must parse as JSON-RPC and its
    expect/extract regexes must match what the simulator answers."""
    probe = DRV.BarcoPulseDriver.DRIVER_INFO["discovery"]["tcp_probe"]
    assert probe["port"] == 9090
    request = json.loads(probe["send_ascii"])
    assert request["method"] == "property.get"

    sim = SIM.BarcoPulseSimulator("probe_sim", {})
    answer = sim.handle_command(probe["send_ascii"].encode("ascii"))
    text = answer.decode("utf-8")
    assert re.search(probe["expect_regex"], text)
    model = re.search(probe["extract"]["model"]["regex"], text)
    assert model and model.group(1) == "F80-4K12"


def test_device_settings_declared():
    info = DRV.BarcoPulseDriver.DRIVER_INFO
    ds = info["device_settings"]
    assert set(ds) == {
        "laser_power", "brightness", "contrast", "saturation",
        "orientation", "eco_mode",
    }
    assert set(ds) == set(DRV.SETTING_TO_PROPERTY)
    for key, entry in ds.items():
        assert entry["state_key"] in info["state_variables"], key
        assert entry["setup"] is False, key
    # Normalized ranges straight from the catalog.
    assert ds["brightness"]["min"] == -1 and ds["brightness"]["max"] == 1
    assert ds["contrast"]["min"] == 0 and ds["contrast"]["max"] == 2
    assert ds["saturation"]["min"] == 0 and ds["saturation"]["max"] == 2
    assert ds["laser_power"]["min"] == 0 and ds["laser_power"]["max"] == 100


def test_actions_reference_real_commands():
    info = DRV.BarcoPulseDriver.DRIVER_INFO
    commands = info["commands"]
    for entry in info["actions"]:
        if entry["kind"] == "command":
            assert entry["id"] in commands, entry["id"]
    setup = [a for a in info["actions"] if a["kind"] == "setup"]
    assert [a["id"] for a in setup] == ["test_pulse"]


def test_state_variables_cover_property_map():
    info = DRV.BarcoPulseDriver.DRIVER_INFO
    for state_key in DRV.PROPERTY_STATE_MAP.values():
        assert state_key in info["state_variables"], state_key
    for state_key in DRV.IDENTITY_PROPERTIES.values():
        assert state_key in info["state_variables"], state_key


# ── Frame parser ────────────────────────────────────────────────────────────

def test_json_stream_parser_split_and_coalesced():
    parser = DRV._JsonStreamParser()
    doc1 = b'{"jsonrpc":"2.0","id":1,"result":"a{b}c\\"{"}'
    doc2 = b'{"jsonrpc":"2.0","id":2,"result":{"nested":{"x":1}}}'

    # One document delivered in three chunks -> exactly one frame.
    assert parser.feed(doc1[:10]) == []
    assert parser.feed(doc1[10:30]) == []
    assert parser.feed(doc1[30:]) == [doc1]

    # Two documents (plus whitespace noise) in one chunk -> two frames.
    frames = parser.feed(b"  \r\n" + doc1 + b"\n" + doc2 + b"  ")
    assert frames == [doc1, doc2]

    # Garbage before the first brace is discarded.
    frames = parser.feed(b"garbage" + doc2)
    assert frames == [doc2]


# ── Connect: identity, prime, subscribe ─────────────────────────────────────

def test_connect_populates_state():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await _settle()
            assert driver.get_state("model") == "F80-4K12"
            assert driver.get_state("serial_number") == "2590123456"
            assert driver.get_state("firmware_version") == "2.1.4"
            assert driver.get_state("family_name") == "F80"
            assert driver.get_state("mac_address") == "00:0D:0A:01:64:39"
            assert driver.get_state("power_state") == "standby"
            assert driver.get_state("illumination") == "Off"
            assert driver.get_state("laser_power") == 80.0
            assert driver.get_state("source") == "HDMI"
            assert driver.get_state("shutter") == "Open"
            assert driver.get_state("brightness") == 0.0
            assert driver.get_state("contrast") == 1.0
            assert driver.get_state("saturation") == 1.0
            assert driver.get_state("eco_mode") is False
            assert driver.get_state("alarm_state") == "Ok"
            assert driver.get_state("temperature_inlet") == 25.5
            assert driver.get_state("temperature_outlet") == 29.4
            options = json.loads(driver.get_state("source_options"))
            assert {"value": "HDMI", "label": "HDMI"} in options
            assert len(options) == len(SIM.DEFAULT_SOURCES)
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_connect_subscribes_every_available_property():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            assert sim._subscribed == set(DRV.PROPERTY_STATE_MAP)
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_absent_property_tolerated(monkeypatch):
    """Dynamic API: a model without environment.alarmstate still connects;
    the state variable just stays unset and is excluded from the poll."""
    async def go():
        driver, sim = await _make_pair()
        monkeypatch.delitem(SIM.PROPERTIES, "environment.alarmstate")
        try:
            await driver.connect()
            assert driver.get_state("alarm_state") is None
            assert "environment.alarmstate" not in driver._available
            assert "environment.alarmstate" not in sim._subscribed
            await driver.poll()  # batched poll must not trip on it
        finally:
            await driver.disconnect()

    asyncio.run(go())
    # monkeypatch restores SIM.PROPERTIES after the test.


# ── Push notifications ──────────────────────────────────────────────────────

def test_simulator_ui_change_pushes_to_driver():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            sim.set_state("source", "SDI")
            await _settle()
            assert driver.get_state("source") == "SDI"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_power_on_walks_state_machine_via_push():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            assert driver.get_state("power_state") == "standby"
            await driver.send_command("power_on")
            await _settle()
            # With transition_delay 0 the sim walks conditioning -> on
            # inline; the driver saw both change notifications.
            assert sim.state["system_state"] == "on"
            assert driver.get_state("power_state") == "on"
            assert driver.get_state("illumination") == "On"
            # No ECO wake needed from standby.
            assert _FakeUDPTransport.sent == []

            await driver.send_command("power_off")
            await _settle()
            assert sim.state["system_state"] == "standby"
            assert driver.get_state("power_state") == "standby"
            assert driver.get_state("illumination") == "Off"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_multi_pair_notification_fans_out():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            frame = json.dumps({
                "jsonrpc": "2.0",
                "method": "property.changed",
                "params": {"property": [
                    {"image.brightness": 0.25},
                    {"image.contrast": 1.5},
                ]},
            }).encode("utf-8")
            await driver.on_data_received(frame)
            assert driver.get_state("brightness") == 0.25
            assert driver.get_state("contrast") == 1.5
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Commands ────────────────────────────────────────────────────────────────

def test_select_source_round_trip_and_unknown_source():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command("select_source", {"source": "SDI"})
            await _settle()
            assert sim.state["source"] == "SDI"
            assert driver.get_state("source") == "SDI"

            with pytest.raises(DRV.PulseError):
                await driver.send_command(
                    "select_source", {"source": "Composite 3"},
                )
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_shutter_commands():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command("shutter_close")
            await _settle()
            assert sim.state["shutter_position"] == "Closed"
            assert driver.get_state("shutter") == "Closed"

            await driver.send_command("shutter_toggle")
            await _settle()
            assert sim.state["shutter_position"] == "Open"
            assert driver.get_state("shutter") == "Open"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_lens_motion_steps():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command("zoom_in", {"steps": 50})
            assert sim.state["zoom_position"] == 50
            await driver.send_command("zoom_out", {"steps": 20})
            assert sim.state["zoom_position"] == 30
            await driver.send_command("focus_in", {"steps": 5})
            assert sim.state["focus_position"] == 5
            await driver.send_command("lens_shift_up", {"steps": 10})
            await driver.send_command("lens_shift_down", {"steps": 4})
            assert sim.state["lensshift_v"] == 6
            await driver.send_command("lens_shift_right", {"steps": 3})
            assert sim.state["lensshift_h"] == 3
            await driver.send_command("lens_center")
            assert sim.state["lensshift_h"] == 0
            assert sim.state["lensshift_v"] == 0
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_zoom_without_motorized_lens_errors_but_stays_connected():
    async def go():
        driver, sim = await _make_pair(sim_config={"motorized_lens": False})
        await driver.connect()
        try:
            with pytest.raises(DRV.PulseError):
                await driver.send_command("zoom_in", {"steps": 10})
            # Still connected and responsive afterwards.
            await driver.send_command("shutter_close")
            assert sim.state["shutter_position"] == "Closed"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_raw_escape_hatch_commands():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            value = await driver.send_command(
                "get_property", {"property": "system.modelname"},
            )
            assert value == "F80-4K12"

            await driver.send_command(
                "set_property",
                {"property": "image.brightness", "value": "0.5"},
            )
            assert sim.state["brightness"] == 0.5

            sources = await driver.send_command(
                "invoke_method", {"method": "image.source.list"},
            )
            assert sources == SIM.DEFAULT_SOURCES
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── ECO Wake-on-LAN assist ──────────────────────────────────────────────────

def test_power_on_from_eco_sends_wol():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            sim.set_state("system_state", "eco")
            await _settle()
            assert driver.get_state("power_state") == "eco"

            await driver.send_command("power_on")
            await _settle()
            assert len(_FakeUDPTransport.sent) == 1
            packet, host, port = _FakeUDPTransport.sent[0]
            assert host == "255.255.255.255" and port == 9
            mac = bytes.fromhex("000d0a016439")
            assert packet == b"\xff" * 6 + mac * 16
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Authentication ──────────────────────────────────────────────────────────

def test_auth_code_accepted():
    async def go():
        driver, sim = await _make_pair(
            sim_config={"auth_code": "98765"},
            driver_overrides={"auth_code": "98765"},
        )
        await driver.connect()
        try:
            assert driver.get_state("connected") is True
            assert driver.get_state("model") == "F80-4K12"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_auth_code_rejected_raises_typed_fault():
    async def go():
        driver, sim = await _make_pair(
            sim_config={"auth_code": "98765"},
            driver_overrides={"auth_code": "11111"},
        )
        with pytest.raises(_FakeConnectionFaultError) as excinfo:
            await driver.connect()
        assert excinfo.value.fault_code == "auth_failed"
        assert "pass code" in str(excinfo.value)
        assert driver._connected is False
        assert driver.transport is None

    asyncio.run(go())


def test_empty_auth_code_skips_authentication():
    """End-user access needs no authenticate call even when the projector
    has a code configured (per the catalog's Authentication section)."""
    async def go():
        driver, sim = await _make_pair(sim_config={"auth_code": "98765"})
        await driver.connect()
        try:
            assert driver.get_state("model") == "F80-4K12"
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Device settings ─────────────────────────────────────────────────────────

def test_device_setting_round_trips():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            cases = [
                ("laser_power", 55.0, "laser_power"),
                ("brightness", -0.25, "brightness"),
                ("contrast", 1.4, "contrast"),
                ("saturation", 0.9, "saturation"),
                ("orientation", "CEILING_FRONT", "orientation"),
                ("eco_mode", True, "eco_enable"),
            ]
            for key, value, sim_key in cases:
                await driver.set_device_setting(key, value)
                await _settle()
                # Sim side mutated…
                assert sim.state[sim_key] == value, key
                # …and the change notification updated the driver.
                assert driver.get_state(key) == value, key

            # Read-back path: wipe local state, re-poll, everything returns.
            for key, value, _sim_key in cases:
                driver.state.data.pop(f"device.pj1.{key}", None)
            await driver.poll()
            await _settle()
            for key, value, _sim_key in cases:
                assert driver.get_state(key) == value, key
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_out_of_range_setting_rejected_by_projector():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            with pytest.raises(DRV.PulseError):
                await driver.set_device_setting("brightness", 5)
            assert sim.state["brightness"] == 0.0
            with pytest.raises(ValueError):
                await driver.set_device_setting("nonexistent", 1)
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Liveness + poll propagation ─────────────────────────────────────────────

def test_health_probe_succeeds_when_alive():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await asyncio.wait_for(driver._liveness_probe(), 1.0)
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_health_loop_forces_reconnect_on_silent_projector():
    async def go():
        global _SWALLOW
        driver, sim = await _make_pair()
        await driver.connect()
        # connect() auto-started the watchdog at production cadence;
        # restart it at test speed (the loop reads self.HEALTH_*).
        driver._stop_health_loop()
        driver.HEALTH_INTERVAL_S = 0.01
        driver.HEALTH_TIMEOUT_S = 0.05
        try:
            _SWALLOW = True  # projector vanishes without a FIN
            driver._start_health_loop()
            await asyncio.sleep(0.5)
            assert driver.disconnect_calls >= 1
            assert driver.stashed_fault is not None
            assert driver.stashed_fault[0] == "no_response"
        finally:
            _SWALLOW = False
            driver._stop_health_loop()

    asyncio.run(go())


def test_poll_raises_when_transport_down():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            driver.transport.connected = False
            with pytest.raises(ConnectionError):
                await driver.poll()
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Setup wizard (real in-test TCP server) ──────────────────────────────────

async def _start_wizard_server(sim):
    async def handle(reader, writer):
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                resp = sim.handle_command(data)
                if resp:
                    writer.write(resp)
                    await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _progress(step, pct=None):
    pass


def test_setup_wizard_reads_identification():
    async def go():
        driver, sim = await _make_pair()
        server, port = await _start_wizard_server(sim)
        driver.config["host"] = "127.0.0.1"
        driver.config["port"] = port
        try:
            result = await driver.run_setup_action(
                "test_pulse", {"auth_code": "", "save": True}, _progress,
            )
            assert result["model"] == "F80-4K12"
            assert result["serial"] == "2590123456"
            assert result["state"] == "standby"
            assert result["authenticated"] is False
            # No code supplied -> nothing saved, no reconnect.
            assert driver.config_updates == []
            assert driver.reconnect_requests == 0
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(go())


def test_setup_wizard_saves_working_code():
    async def go():
        driver, sim = await _make_pair(sim_config={"auth_code": "4242"})
        server, port = await _start_wizard_server(sim)
        driver.config["host"] = "127.0.0.1"
        driver.config["port"] = port
        try:
            result = await driver.run_setup_action(
                "test_pulse", {"auth_code": "4242", "save": True}, _progress,
            )
            assert result["authenticated"] is True
            assert driver.config_updates == [{"auth_code": "4242"}]
            assert driver.reconnect_requests == 1
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(go())


def test_setup_wizard_rejected_code():
    async def go():
        driver, sim = await _make_pair(sim_config={"auth_code": "4242"})
        server, port = await _start_wizard_server(sim)
        driver.config["host"] = "127.0.0.1"
        driver.config["port"] = port
        try:
            with pytest.raises(ConnectionError) as excinfo:
                await driver.run_setup_action(
                    "test_pulse", {"auth_code": "9999", "save": True},
                    _progress,
                )
            assert "rejected the pass code" in str(excinfo.value)
            assert driver.config_updates == []
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(go())


def test_setup_wizard_unreachable_host():
    async def go():
        driver, sim = await _make_pair()
        driver.config["host"] = "127.0.0.1"
        driver.config["port"] = 1  # nothing listens here
        with pytest.raises(ConnectionError):
            await driver.run_setup_action(
                "test_pulse", {"auth_code": "", "save": False}, _progress,
            )

    asyncio.run(go())
