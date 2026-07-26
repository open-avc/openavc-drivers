"""Driver + simulator tests for ptzoptics (raw VISCA-over-TCP PTZ).

No PTZOptics camera on hand, so correctness is proven two ways: metadata/shape
assertions on the driver, and a **dual-proof round trip** that wires the real
driver to the real simulator over an in-memory TCP transport — the simulator
renders the VISCA reply bytes, the driver's 0xFF frame parser splits them, the
driver parses each frame, and the results are asserted on both sides. This
substitutes for a hardware fixture (same approach as test_visca_ip.py, but the
PTZOptics wire form is raw VISCA on TCP/5678 with no Sony VISCA-over-IP wrapper).

Covers the device-settings + quick-actions upgrade:
  - ae_mode / wb_mode / backlight / flip / lr_reverse / picture_flip write +
    read back through the pending-queue state_key, alongside the transient
    set_* commands they share byte-building with.

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

from _lifecycle_fake import LifecycleFake

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "cameras" / "ptzoptics.py"
SIM_PATH = REPO_ROOT / "cameras" / "ptzoptics_sim.py"


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


# Set by the pairing harness so the stubbed transport reaches the live sim.
_CURRENT_SIM: object | None = None


class _FakeTCPTransport:
    """Stand-in for server.transport.tcp.TCPTransport over the live sim.

    PTZOptics frames on the trailing 0xFF, so the real frame parser strips the
    delimiter and delivers one VISCA packet body per ``on_data`` call. The sim
    returns concatenated ``... FF`` replies (an ACK+Completion is two frames),
    so split on 0xFF and deliver each non-empty body, mirroring the parser.
    """

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
        self.connected = True

    async def send(self, data):
        if not self.connected:
            raise ConnectionError("transport closed")
        resp = self._sim.handle_command(bytes(data))
        if resp:
            for frame in resp.split(b"\xff"):
                if frame:
                    await self.on_data(frame)

    async def close(self):
        self.connected = False


class _FakeBaseDriver(LifecycleFake):
    """Functional stand-in mirroring BaseDriver.connect() for a TCP driver.

    The real BaseDriver.connect() opens the socket and marks the device
    connected; the ptzoptics driver's connect() override then builds its inquiry
    lock and runs an initial poll. The stand-in reproduces just enough of that
    so the driver's override composes correctly.
    """

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False

    async def connect(self) -> None:
        self.transport = _FakeTCPTransport(
            host=self.config.get("host", ""),
            port=self.config.get("port"),
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            name=self.device_id,
        )
        await self.transport.open(local_addr=None)
        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        await self._initial_sync()

    async def disconnect(self) -> None:
        await self.stop_polling()
        if self.transport:
            await self.transport.close()
            self.transport = None
        await self._close_session()
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

    async def _initial_sync(self) -> None:
        pass

    async def _close_session(self) -> None:
        pass


class _FakeTCPSimulator:
    """Stand-in for simulator.tcp_simulator.TCPSimulator."""

    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self._state = dict(self.SIMULATOR_INFO.get("initial_state", {}))

    def get_state(self, key, default=None):
        return self._state.get(key, default)

    def set_state(self, key, value) -> None:
        self._state[key] = value

    def has_error_behavior(self, _name) -> bool:
        return False


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


DRV = _load("ptzoptics_under_test", DRIVER_PATH)
SIM = _load("ptzoptics_sim_under_test", SIM_PATH)


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(sim_state=None, driver_overrides=None):
    global _CURRENT_SIM
    sim = SIM.PTZOpticsSimulator("sim1", {})
    if sim_state:
        sim._state.update(sim_state)
    _CURRENT_SIM = sim

    cfg = {
        "host": "10.0.0.7",
        "port": 5678,
        "poll_interval": 0,
    }
    cfg.update(driver_overrides or {})
    driver = DRV.PTZOpticsDriver("cam1", cfg, _FakeState(), _FakeEvents())
    return driver, sim


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_version_bumped():
    assert DRV.PTZOpticsDriver.DRIVER_INFO["version"] == "1.3.1"


def test_device_settings_declared():
    ds = DRV.PTZOpticsDriver.DRIVER_INFO["device_settings"]
    assert set(ds) == {
        "ae_mode", "wb_mode", "backlight", "flip", "lr_reverse", "picture_flip",
    }
    # Every setting reads back from a real polled state var.
    state_vars = DRV.PTZOpticsDriver.DRIVER_INFO["state_variables"]
    for key, spec in ds.items():
        assert spec["state_key"] in state_vars, f"{key} state_key not polled"
    assert ds["ae_mode"]["type"] == "enum"
    assert ds["flip"]["type"] == "enum"
    assert ds["backlight"]["type"] == "boolean"
    assert ds["lr_reverse"]["type"] == "boolean"
    # The transient set_* commands stay (dual surface for macro use).
    cmds = DRV.PTZOpticsDriver.DRIVER_INFO["commands"]
    for c in (
        "set_ae_mode", "set_wb_mode", "set_backlight",
        "set_flip", "set_lr_reverse", "set_picture_flip",
    ):
        assert c in cmds


def test_quick_actions_reference_real_commands():
    info = DRV.PTZOpticsDriver.DRIVER_INFO
    cmds = set(info["commands"])
    for qa in info["quick_actions"]:
        assert qa in cmds, f"quick_action {qa} is not a real command"


# ── Round-trip: connect populates state ─────────────────────────────────────

def test_connect_populates_state():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            assert driver.get_state("ae_mode") == "full_auto"
            assert driver.get_state("wb_mode") == "auto"
            assert driver.get_state("backlight") is False
            assert driver.get_state("flip") == "off"
            assert driver.get_state("lr_reverse") is False
            assert driver.get_state("picture_flip") is False
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


def test_set_flip_round_trip():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command("set_flip", {"mode": "hv"})
            assert sim.get_state("flip") == "hv"
            await driver.poll()
            assert driver.get_state("flip") == "hv"
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


def test_flip_device_setting_round_trip():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.set_device_setting("flip", "v")
            assert sim.get_state("flip") == "v"
            await driver.poll()
            assert driver.get_state("flip") == "v"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_picture_flip_device_setting_round_trip():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.set_device_setting("picture_flip", True)
            assert sim.get_state("picture_flip") is True
            await driver.poll()
            assert driver.get_state("picture_flip") is True
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_boolean_device_setting_coerces_string():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            # The settings editor can hand back a string "true" / "false".
            await driver.set_device_setting("lr_reverse", "true")
            assert sim.get_state("lr_reverse") is True
            await driver.set_device_setting("lr_reverse", "false")
            assert sim.get_state("lr_reverse") is False
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
