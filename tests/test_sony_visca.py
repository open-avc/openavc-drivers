"""Driver + simulator tests for sony_visca (Sony SRG/BRC/EVI VISCA-over-IP).

No Sony camera on hand, so correctness is proven two ways: metadata/shape
assertions, and a **dual-proof round trip** wiring the real driver to the real
simulator over an in-memory UDP transport — the sim renders the VISCA-over-IP
reply bytes, the driver parses them, both sides asserted (same approach as
test_visca_ip.py / test_wattbox_ip.py).

Covers the v1.3.0 first-class adoption:
  - never-offline fix: an unplugged camera (UDP black hole) now goes offline —
    poll() and _post_connect() gate on the power-inquiry liveness probe and
    raise a no_response-worded ConnectionError when the camera answers nothing;
  - device settings: the full picture / image-quality surface writes + reads
    back through the pending-queue state_keys, alongside the transient set_*
    commands.

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
DRIVER_PATH = REPO_ROOT / "cameras" / "sony_visca.py"
SIM_PATH = REPO_ROOT / "cameras" / "sony_visca_sim.py"


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


_CURRENT_SIM: object | None = None
# When True, the transport sends to the sim but DROPS the reply — simulating a
# silently-vanished camera (UDP black hole) for the never-offline tests.
_SWALLOW = False


class _FakeUDPTransport:
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
        if resp and not _SWALLOW:
            await self.on_data(resp)

    async def close(self):
        self.connected = False


class _FakeBaseDriver(LifecycleFake):
    """Mirrors BaseDriver.connect() for a UDP driver, including the
    teardown-on-raise the never-offline fix relies on."""

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
    sim_udp = ModuleType("simulator.udp_simulator")
    sim_udp.UDPSimulator = _FakeUDPSimulator
    sys.modules["simulator.udp_simulator"] = sim_udp

    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


DRV = _load("sony_visca_under_test", DRIVER_PATH)
SIM = _load("sony_visca_sim_under_test", SIM_PATH)


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(driver_overrides=None):
    global _CURRENT_SIM, _SWALLOW
    _SWALLOW = False
    sim = SIM.SonyVISCASimulator("sim1", {})
    _CURRENT_SIM = sim

    cfg = {"host": "10.0.0.9", "port": 52381, "poll_interval": 0}
    cfg.update(driver_overrides or {})
    driver = DRV.SonyVISCADriver("cam1", cfg, _FakeState(), _FakeEvents())
    return driver, sim


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_version_bumped():
    assert DRV.SonyVISCADriver.DRIVER_INFO["version"] == "1.3.1"


def test_zoom_direct_bound_widened():
    # v1.3.1: zoom_direct covers Clear Image Zoom / digital positions to
    # 0x7AC0 (Sony spec); the old 16384 cap blocked the digital range once
    # bounds became runtime-enforced.
    pos = DRV.SonyVISCADriver.DRIVER_INFO["commands"]["zoom_direct"]["params"]["position"]
    assert pos["min"] == 0 and pos["max"] == 31424


def test_zoom_direct_full_clear_image_range_round_trip():
    # Regression: the send clamp used to cap at 0x4000, silently rewriting a
    # Clear Image Zoom position of 31424 to 16384 before it hit the wire.
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command("zoom_direct", {"position": 31424})
            assert sim.get_state("zoom_position") == 31424
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_device_settings_declared():
    ds = DRV.SonyVISCADriver.DRIVER_INFO["device_settings"]
    # The full persisted, read-back picture surface (tally is excluded — it's
    # production-driven, not a set-once setting).
    expected = {
        "picture_profile", "ae_mode", "ae_speed", "wb_mode", "rgain", "bgain",
        "chroma_suppress", "backlight", "spotlight", "visibility_enhancer",
        "defog", "defog_level", "low_light", "low_light_level",
        "high_sensitivity", "min_shutter", "max_shutter", "exp_comp",
        "exp_comp_level", "flicker_cancel", "ir_correction", "ir_cut_filter",
        "auto_icr", "af_mode", "preset_mode",
    }
    assert set(ds) == expected
    assert "tally_level" not in ds  # operational, stays a command
    # Every setting's state_key must be a declared, read-back state var.
    state_vars = DRV.SonyVISCADriver.DRIVER_INFO["state_variables"]
    for key, defn in ds.items():
        assert defn["state_key"] in state_vars, key
    # Integer settings carry bounds for the editable number field.
    assert ds["rgain"]["min"] == 0 and ds["rgain"]["max"] == 255
    assert ds["ae_speed"]["min"] == 1 and ds["ae_speed"]["max"] == 48


def test_quick_actions_reference_real_commands():
    info = DRV.SonyVISCADriver.DRIVER_INFO
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
            assert driver.get_state("picture_profile") == "std"
            assert driver.get_state("ae_mode") == "full_auto"
            assert driver.get_state("rgain") == 0x80
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Device settings: write + read-back across the surface ───────────────────

# (setting key, value to write, sim state key, expected sim/driver value)
_DS_CASES = [
    ("picture_profile", "movie", "picture_profile", "movie"),
    ("ae_mode", "iris", "ae_mode", "iris"),
    ("ae_speed", 20, "ae_speed", 20),
    ("wb_mode", "indoor", "wb_mode", "indoor"),
    ("rgain", 200, "rgain", 200),
    ("bgain", 60, "bgain", 60),
    ("chroma_suppress", 2, "chroma_suppress", 2),
    ("backlight", True, "backlight", True),
    ("spotlight", True, "spotlight", True),
    ("visibility_enhancer", True, "visibility_enhancer", True),
    ("low_light", True, "low_light", True),
    ("low_light_level", 8, "low_light_level", 8),
    ("high_sensitivity", True, "high_sensitivity", True),
    ("min_shutter", 0x20, "min_shutter", 0x20),
    ("max_shutter", 0x55, "max_shutter", 0x55),
    ("exp_comp", True, "exp_comp", True),
    ("exp_comp_level", 10, "exp_comp_level", 10),
    ("flicker_cancel", True, "flicker_cancel", True),
    ("ir_correction", "ir_light", "ir_correction", "ir_light"),
    ("ir_cut_filter", "night", "ir_cut_filter", "night"),
    ("auto_icr", True, "auto_icr", True),
    ("af_mode", "interval", "af_mode", "interval"),
    ("preset_mode", "mode2", "preset_mode", "mode2"),
]


@pytest.mark.parametrize("key,value,sim_key,expected", _DS_CASES)
def test_device_setting_round_trip(key, value, sim_key, expected):
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.set_device_setting(key, value)
            assert sim.get_state(sim_key) == expected, f"sim {sim_key}"
            await driver.poll()
            assert driver.get_state(key) == expected, f"driver {key}"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_defog_compound_setting_round_trip():
    """defog is compound — set_defog carries on/off AND level together, so
    writing either side re-sends both (reading the sibling from state)."""
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.set_device_setting("defog", True)
            assert sim.get_state("defog") is True
            await driver.poll()
            assert driver.get_state("defog") is True
            # Now that defog is on, set the level — the sim only stores the
            # level while defog is on (matching real hardware).
            await driver.set_device_setting("defog_level", 3)
            assert sim.get_state("defog_level") == 3
            await driver.poll()
            assert driver.get_state("defog_level") == 3
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_backlight_device_setting_coerces_string():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
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


# ── Command round-trip (dual surface kept) ──────────────────────────────────

def test_set_picture_profile_command_round_trip():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command("set_picture_profile", {"profile": "cinema"})
            assert sim.get_state("picture_profile") == "cinema"
            await driver.poll()
            assert driver.get_state("picture_profile") == "cinema"
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Never-offline regression: a silent camera must surface offline ──────────

def test_poll_raises_no_response_when_camera_goes_silent():
    async def go():
        global _SWALLOW
        driver, sim = await _make_pair()
        await driver.connect()
        DRV.PROBE_TIMEOUT_S = 0.05
        try:
            _SWALLOW = True
            with pytest.raises(ConnectionError) as ei:
                await driver.poll()
            assert "not responding" in str(ei.value).lower()
        finally:
            _SWALLOW = False
            DRV.PROBE_TIMEOUT_S = 1.5
            await driver.disconnect()

    asyncio.run(go())


def test_connect_raises_no_response_when_camera_dead():
    async def go():
        global _SWALLOW
        driver, sim = await _make_pair()
        DRV.PROBE_TIMEOUT_S = 0.05
        DRV.RESET_TIMEOUT_S = 0.05
        DRV.PROBE_RETRIES = 2
        try:
            _SWALLOW = True
            with pytest.raises(ConnectionError) as ei:
                await driver.connect()
            assert "not responding" in str(ei.value).lower()
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
        await driver.connect()
        try:
            assert driver._connected is True
        finally:
            await driver.disconnect()

    asyncio.run(go())
