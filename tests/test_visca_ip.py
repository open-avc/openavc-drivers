"""Driver + simulator tests for visca_ip (generic Sony-spec VISCA-over-IP PTZ).

No VISCA camera on hand, so correctness is proven two ways: metadata/shape
assertions on the driver, and a **dual-proof round trip** that wires the real
driver to the real simulator over an in-memory UDP transport — the simulator
renders the VISCA-over-IP reply bytes, the driver parses them, and the results
are asserted on both sides. This substitutes for a hardware fixture (same
approach as test_wattbox_ip.py / test_racklink_rlnk.py).

Covers the v1.3.0 first-class adoption:
  - never-offline fix: an unplugged camera (UDP black hole) now goes offline —
    poll() raises a no_response-worded ConnectionError when the camera answers
    nothing, and _post_connect() gates the connect on the same liveness probe
    so a dead camera surfaces offline instead of phantom-connected;
  - device settings: ae_mode / wb_mode / backlight write + read back through
    the pending-queue state_key, alongside the transient set_* commands.

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
    StubEvents as _FakeEvents,
    StubState as _FakeState,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "cameras" / "visca_ip.py"
SIM_PATH = REPO_ROOT / "cameras" / "visca_ip_sim.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

# Set by the pairing harness so the stubbed transport reaches the live sim.
_CURRENT_SIM: object | None = None
# When True, the transport sends to the sim but DROPS the reply — simulating a
# silently-vanished camera (UDP black hole) for the never-offline tests.
_SWALLOW = False


class _FakeUDPTransport:
    """Stand-in for openavc.transport.udp.UDPTransport over the live sim."""

    def __init__(self, *, host, port, on_data, on_disconnect,
                 inter_command_delay=0.0, name=""):
        self.host = host
        self.port = port
        self.on_data = on_data
        self.on_disconnect = on_disconnect
        self.connected = False
        self.last_error = ""
        self._sim = _CURRENT_SIM

    async def open(self, local_addr=None):
        # UDP "opens" unconditionally — it's connectionless. That's exactly why
        # the driver needs a protocol-level liveness probe.
        self.connected = True

    async def send(self, data):
        if not self.connected:
            raise ConnectionError("transport closed")
        resp = self._sim.handle_command(bytes(data))
        if resp and not _SWALLOW:
            await self.on_data(resp)

    async def close(self):
        self.connected = False


class _FakeBaseDriver(LifecycleFake):
    """Functional stand-in mirroring BaseDriver.connect() for a UDP driver.

    The real BaseDriver.connect() opens the UDP socket, runs _post_connect()
    BEFORE marking the device connected, and on a raise stashes the transport
    error / closes the socket / re-raises. The never-offline fix relies on that
    teardown-on-raise, so the stand-in reproduces it faithfully.
    """

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False
        self._last_transport_error = ""

    async def connect(self) -> None:
        self._last_transport_error = ""
        self.transport = _FakeUDPTransport(
            host=self.config.get("host", ""),
            port=self.config.get("port"),
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            name=self.device_id,
        )
        await self.transport.open(local_addr=None)
        try:
            await self._post_connect()
            self._connected = True
            self.set_state("connected", True)
            await self.events.emit(f"device.connected.{self.device_id}")
        except Exception:
            if self.transport:
                await self.transport.close()
                self.transport = None
            self._connected = False
            raise

    async def disconnect(self) -> None:
        await self.stop_polling()
        if self.transport:
            await self.transport.close()
            self.transport = None
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")

    def set_state(self, key, value) -> None:
        self.state.set(key, value)

    def set_states(self, updates) -> None:
        for k, v in updates.items():
            self.state.set(k, v)

    def get_state(self, key, default=None):
        return self.state.data.get(key, default)

    def _handle_transport_disconnect(self) -> None:
        if self.transport is not None:
            self.transport.connected = False


class _FakeUDPSimulator:
    """Stand-in for openavc.simulator.udp_simulator.UDPSimulator."""

    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self._state = dict(self.SIMULATOR_INFO.get("initial_state", {}))

    def get_state(self, key, default=None):
        return self._state.get(key, default)

    def set_state(self, key, value) -> None:
        self._state[key] = value


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
    logger = ModuleType("openavc.utils.logger")
    logger.get_logger = lambda name="x": logging.getLogger(name)
    sys.modules["openavc.utils.logger"] = logger

    sim_pkg = ModuleType("openavc.simulator")
    sim_pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules["openavc.simulator"] = sim_pkg
    sim_udp = ModuleType("openavc.simulator.udp_simulator")
    sim_udp.UDPSimulator = _FakeUDPSimulator
    sys.modules["openavc.simulator.udp_simulator"] = sim_udp

    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


DRV = _load("visca_ip_under_test", DRIVER_PATH)
SIM = _load("visca_ip_sim_under_test", SIM_PATH)


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(sim_state=None, driver_overrides=None):
    global _CURRENT_SIM, _SWALLOW
    _SWALLOW = False
    sim = SIM.VISCAIPSimulator("sim1", {})
    if sim_state:
        sim._state.update(sim_state)
    _CURRENT_SIM = sim

    cfg = {
        "host": "10.0.0.7",
        "port": 52381,
        "poll_interval": 0,
    }
    cfg.update(driver_overrides or {})
    driver = DRV.VISCAIPDriver("cam1", cfg, _FakeState(), _FakeEvents())
    return driver, sim


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_version_bumped():
    assert DRV.VISCAIPDriver.DRIVER_INFO["version"] == "1.3.2"


def test_zoom_and_preset_bounds_widened():
    # v1.3.1: zoom_direct covers the digital / Clear Image range to 0x7AC0
    # (Sony spec), and presets cover 0-0x7F (AVer models accept the full byte).
    # These bounds feed the platform's runtime param gate — too-narrow values
    # here block valid commands.
    cmds = DRV.VISCAIPDriver.DRIVER_INFO["commands"]
    pos = cmds["zoom_direct"]["params"]["position"]
    assert pos["min"] == 0 and pos["max"] == 31424
    for c in ("preset_recall", "preset_set", "preset_reset"):
        num = cmds[c]["params"]["number"]
        assert num["min"] == 0 and num["max"] == 127, c


def test_device_settings_declared():
    ds = DRV.VISCAIPDriver.DRIVER_INFO["device_settings"]
    assert set(ds) == {"ae_mode", "wb_mode", "backlight"}
    assert ds["ae_mode"]["state_key"] == "ae_mode"
    assert ds["ae_mode"]["type"] == "enum"
    assert ds["wb_mode"]["state_key"] == "wb_mode"
    assert ds["backlight"]["type"] == "boolean"
    # The transient set_* commands stay (dual surface for macro use).
    cmds = DRV.VISCAIPDriver.DRIVER_INFO["commands"]
    for c in ("set_ae_mode", "set_wb_mode", "set_backlight"):
        assert c in cmds


def test_quick_actions_reference_real_commands():
    info = DRV.VISCAIPDriver.DRIVER_INFO
    cmds = set(info["commands"])
    for qa in info["quick_actions"]:
        assert qa in cmds, f"quick_action {qa} is not a real command"


# ── Round-trip: connect populates state ─────────────────────────────────────

def test_connect_populates_state():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            assert driver.get_state("power") == "on"
            assert driver.get_state("ae_mode") == "full_auto"
            assert driver.get_state("wb_mode") == "auto1"
            assert driver.get_state("backlight") is False
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Round-trip: command mutates the sim and the driver reads it back ─────────

def test_set_ae_mode_round_trip():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command("set_ae_mode", {"mode": "manual"})
            assert sim.get_state("ae_mode") == "manual"
            await driver.poll()
            assert driver.get_state("ae_mode") == "manual"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_set_backlight_round_trip():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command("set_backlight", {"enabled": True})
            assert sim.get_state("backlight") is True
            await driver.poll()
            assert driver.get_state("backlight") is True
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_zoom_direct_full_digital_range_round_trip():
    # Regression: the send clamp used to cap at 0x4000, silently rewriting a
    # digital-zoom position of 31424 to 16384 before it hit the wire.
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command("zoom_direct", {"position": 31424})
            assert sim.get_state("zoom_position") == 31424
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_preset_slot_127_round_trip():
    # Regression: preset slots used to clamp at 99, so slot 127 (valid on the
    # AVer models in compatible_models) silently saved/recalled slot 99.
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command("zoom_direct", {"position": 1000})
            await driver.send_command("preset_set", {"number": 127})
            await driver.send_command("zoom_direct", {"position": 0})
            assert sim.get_state("zoom_position") == 0
            await driver.send_command("preset_recall", {"number": 127})
            assert sim.get_state("zoom_position") == 1000
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Device settings: write + read-back through the state_key ─────────────────

def test_ae_mode_device_setting_round_trip():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.set_device_setting("ae_mode", "iris")
            assert sim.get_state("ae_mode") == "iris"
            await driver.poll()
            assert driver.get_state("ae_mode") == "iris"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_backlight_device_setting_coerces_string():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            # The settings editor can hand back a string "true".
            await driver.set_device_setting("backlight", "true")
            assert sim.get_state("backlight") is True
            await driver.set_device_setting("backlight", "false")
            assert sim.get_state("backlight") is False
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_unknown_device_setting_raises():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            with pytest.raises(ValueError):
                await driver.set_device_setting("nonsense", 1)
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Never-offline regression: a silent camera must surface offline ──────────

def test_poll_raises_no_response_when_camera_goes_silent():
    """Regression for the never-offline bug: a connected camera that stops
    answering must make poll() raise a no_response-worded ConnectionError so
    the missed-poll watchdog flips it offline. The old poll() swallowed the
    UDP timeout and returned cleanly, leaving an unplugged camera ONLINE."""
    async def go():
        global _SWALLOW
        driver, sim = await _make_pair()
        await driver.connect()
        DRV.PROBE_TIMEOUT_S = 0.05
        try:
            _SWALLOW = True  # camera goes dark
            with pytest.raises(ConnectionError) as ei:
                await driver.poll()
            # The shared connection-fault classifier maps this wording to
            # offline_reason=no_response.
            assert "not responding" in str(ei.value).lower()
        finally:
            _SWALLOW = False
            DRV.PROBE_TIMEOUT_S = 1.5
            await driver.disconnect()

    asyncio.run(go())


def test_connect_raises_no_response_when_camera_dead():
    """A camera that never answers must make connect() raise (so it shows
    offline with no_response, not a phantom-connected session, and reconnect
    attempts keep that reason instead of flapping back online)."""
    async def go():
        global _SWALLOW
        driver, sim = await _make_pair()
        DRV.PROBE_TIMEOUT_S = 0.05
        DRV.RESET_TIMEOUT_S = 0.05
        DRV.PROBE_RETRIES = 2
        try:
            _SWALLOW = True  # dead from the start
            with pytest.raises(ConnectionError) as ei:
                await driver.connect()
            assert "not responding" in str(ei.value).lower()
            # The socket was torn down on the failed connect.
            assert driver.transport is None
        finally:
            _SWALLOW = False
            DRV.PROBE_TIMEOUT_S = 1.5
            DRV.RESET_TIMEOUT_S = 2.0
            DRV.PROBE_RETRIES = 3

    asyncio.run(go())


def test_connect_succeeds_when_camera_alive():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()  # must not raise
        try:
            assert driver._connected is True
        finally:
            await driver.disconnect()

    asyncio.run(go())
