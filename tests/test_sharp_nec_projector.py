"""Driver + simulator tests for sharp_nec_projector (NEC binary protocol).

The driver is hardware-verified (NP-PE456USL); these tests lock in the v2.5.0
additions without regressing that. Correctness is proven by a
dual-proof round trip wiring the real driver to the real simulator over an
in-memory TCP transport — the sim renders NEC binary reply frames, the driver's
own CallableFrameParser splits them, and both sides are asserted.

Covers:
  - the read-back FIX: 060-1 GAIN responses don't echo their target, so
    volume/brightness/contrast were declared but never populated. The driver now
    correlates each reply to the query order, so connect + poll actually read
    them back (a regression that would have caught the never-populated bug);
  - device settings: volume/brightness/contrast/eco_mode write + read back
    through the pending-queue state_key (aspect excluded — no read-back verb);
  - the discovery tcp_probe on 7142.

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
DRIVER_PATH = REPO_ROOT / "projectors" / "sharp_nec_projector.py"
SIM_PATH = REPO_ROOT / "projectors" / "sharp_nec_projector_sim.py"


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


class _CallableFrameParser:
    """Replica of server.transport.frame_parsers.CallableFrameParser.feed."""

    def __init__(self, parse_fn, max_buffer=65536):
        self._parse_fn = parse_fn
        self._buffer = b""

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer += data
        out: list[bytes] = []
        while True:
            msg, remaining = self._parse_fn(self._buffer)
            if msg is None:
                break
            out.append(msg)
            if len(remaining) >= len(self._buffer):
                self._buffer = remaining
                break
            self._buffer = remaining
        return out

    def reset(self) -> None:
        self._buffer = b""


class _FrameParser:  # marker base, like server.transport.frame_parsers.FrameParser
    pass


_CURRENT_SIM: object | None = None


class _FakeTransport:
    """In-memory transport: NEC packets in, sim frames back through the parser."""

    def __init__(self, driver, parser):
        self._driver = driver
        self._parser = parser
        self._sim = _CURRENT_SIM
        self.connected = False

    async def open(self):
        self.connected = True

    async def send(self, data):
        if not self.connected:
            raise ConnectionError("transport closed")
        resp = self._sim.handle_command(bytes(data))
        if resp:
            for frame in self._parser.feed(resp):
                await self._driver.on_data_received(frame)

    async def close(self):
        self.connected = False


class _FakeBaseDriver:
    """Functional stand-in mirroring BaseDriver.connect() for a binary TCP
    driver: builds the transport using the driver's own frame-parser hook."""

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False

    async def connect(self) -> None:
        parser = self._create_frame_parser()
        self.transport = _FakeTransport(self, parser)
        await self.transport.open()
        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        await self._initial_sync()

    async def _initial_sync(self) -> None:
        pass

    async def _close_session(self) -> None:
        pass

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

    async def start_polling(self, interval) -> None:
        pass

    async def stop_polling(self) -> None:
        pass


class _FakeTCPSimulator:
    """Stand-in for simulator.tcp_simulator.TCPSimulator."""

    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self.state = dict(self.SIMULATOR_INFO.get("initial_state", {}))

    def set_state(self, key, value) -> None:
        self.state[key] = value

    def get_state(self, key, default=None):
        return self.state.get(key, default)


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
    binary_helpers = ModuleType("server.transport.binary_helpers")
    binary_helpers.checksum_sum = lambda data, mask=0xFF: sum(data) & mask
    sys.modules["server.transport.binary_helpers"] = binary_helpers
    fp = ModuleType("server.transport.frame_parsers")
    fp.CallableFrameParser = _CallableFrameParser
    fp.FrameParser = _FrameParser
    sys.modules["server.transport.frame_parsers"] = fp
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


DRV = _load("sharp_nec_under_test", DRIVER_PATH)
SIM = _load("sharp_nec_sim_under_test", SIM_PATH)
# Neutralize the 600ms inter-command delay so tests don't take seconds.
DRV.MIN_CMD_DELAY = 0.0


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(sim_state=None):
    global _CURRENT_SIM
    sim = SIM.SharpNecProjectorSimulator("sim1", {})
    sim.set_state("power", "on")  # so the projector answers picture queries
    if sim_state:
        sim.state.update(sim_state)
    _CURRENT_SIM = sim
    driver = DRV.SharpNECProjectorDriver(
        "proj1", {"host": "10.0.0.9", "port": 7142, "poll_interval": 0},
        _FakeState(), _FakeEvents(),
    )
    return driver, sim


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_version_bumped():
    assert DRV.SharpNECProjectorDriver.DRIVER_INFO["version"] == "2.5.2"


def test_device_settings_declared():
    info = DRV.SharpNECProjectorDriver.DRIVER_INFO
    ds = info["device_settings"]
    assert set(ds) == {"volume", "brightness", "contrast", "eco_mode"}
    state_vars = info["state_variables"]
    for spec in ds.values():
        assert spec["state_key"] in state_vars
        assert spec["setup"] is False
    assert ds["volume"]["min"] == 0 and ds["volume"]["max"] == 63
    assert ds["eco_mode"]["type"] == "string"
    # aspect has no read-back verb -> not a setting.
    assert "aspect" not in ds
    # Transient set commands stay (dual surface).
    for c in ("volume_set", "brightness_set", "contrast_set", "eco_mode_set"):
        assert c in info["commands"]


def test_quick_actions_reference_real_commands():
    info = DRV.SharpNECProjectorDriver.DRIVER_INFO
    cmds = set(info["commands"])
    for qa in info["quick_actions"]:
        assert qa in cmds, f"quick_action {qa} is not a real command"


def test_discovery_tcp_probe_declared():
    disc = DRV.SharpNECProjectorDriver.DRIVER_INFO["discovery"]
    probe = disc["tcp_probe"]
    assert probe["port"] == 7142
    assert probe["send_hex"] == "00bf00000102c2"   # 305-3 BASIC INFO request
    assert probe["expect_hex"] == "20bf"           # status-ok header + cmd
    assert 7142 in disc["port_open"]


# ── The read-back FIX: GAIN responses now populate their state var ───────────

def test_connect_reads_back_gains():
    """Regression for the read-back fix. Before it, volume/brightness/contrast
    were declared state vars but the 060-1 GAIN response was never stored
    (the handler couldn't tell which target it answered). Now connect reads
    them back from the sim's defaults (50/50/50)."""
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            assert driver.get_state("volume") == 50
            assert driver.get_state("brightness") == 50
            assert driver.get_state("contrast") == 50
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_gain_targets_are_not_crossed():
    """Distinct sim values must land in the right state var — proves the
    request-order correlation, not just 'some value got stored'."""
    async def go():
        driver, sim = await _make_pair(
            {"volume": 12, "brightness": 34, "contrast": 56})
        await driver.connect()
        try:
            assert driver.get_state("volume") == 12
            assert driver.get_state("brightness") == 34
            assert driver.get_state("contrast") == 56
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Device settings: write + read-back ──────────────────────────────────────

_DS_CASES = [
    ("volume", 40, "volume", 40),
    ("brightness", 70, "brightness", 70),
    ("contrast", 25, "contrast", 25),
    ("eco_mode", "2", "eco_mode", "2"),
]


@pytest.mark.parametrize("key,value,sim_key,expected", _DS_CASES)
def test_device_setting_round_trip(key, value, sim_key, expected):
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.set_device_setting(key, value)
            # set_device_setting issues the setter then re-reads, so the state
            # var reflects the on-device value without waiting for a poll.
            assert str(sim.get_state(sim_key)) == str(expected)
            assert str(driver.get_state(key)) == str(expected)
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_unknown_device_setting_raises():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            with pytest.raises(ValueError):
                await driver.set_device_setting("aspect", 1)
        finally:
            await driver.disconnect()

    asyncio.run(go())
