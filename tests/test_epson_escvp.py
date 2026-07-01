"""Driver + simulator tests for epson_escvp (Epson ESC/VP21 over ESC/VP.net).

No Epson projector on hand, so correctness is proven two ways: metadata/shape
assertions, and a **dual-proof round trip** wiring the real driver to the real
simulator over an in-memory TCP transport that mimics ESC/VP.net — the fake
transport answers the 16-byte binary handshake and then pipes ESC/VP21 replies
back through the driver's own stateful frame parser (same approach as
test_ptzoptics.py, adapted for the handshake + colon framing).

Covers the v1.4.0 additions:
  - device settings: aspect / color_mode write + read back through the decoded
    *_name state_key, alongside the transient set_* commands they route to
    (brightness / contrast deliberately excluded — ESC/VP21 has no query verb,
    so no read-back);
  - quick actions;
  - the connection-fault fix: a handshake timeout now words as no_response and
    a rejected handshake (password set / version mismatch) as auth_failed.

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
DRIVER_PATH = REPO_ROOT / "projectors" / "epson_escvp.py"
SIM_PATH = REPO_ROOT / "projectors" / "epson_escvp_sim.py"

_ORIG_WAIT_FOR = asyncio.wait_for


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


# Harness knobs set per test.
_CURRENT_SIM: object | None = None
_DROP_HANDSHAKE = False   # projector never answers -> connect timeout
_REJECT_HANDSHAKE = False  # projector answers with a non-ok status byte


class _FakeTCPTransport:
    """Stand-in for TCPTransport that speaks ESC/VP.net to the live sim.

    The driver sends the 16-byte handshake first; this answers it (or drops /
    rejects it per the harness knobs), then routes ESC/VP21 lines to the sim's
    handle_command. Replies are fed through the driver's own frame parser so the
    handshake->colon framing state machine is exercised for real.
    """

    def __init__(self) -> None:
        self.on_data = None
        self.connected = False
        self._sim = None
        self._parser = None

    @classmethod
    async def create(cls, *, host, port, on_data, on_disconnect, delimiter=None,
                     frame_parser=None, inter_command_delay=0.0, timeout=5.0,
                     name=""):
        t = cls()
        t.on_data = on_data
        t.connected = True
        t._sim = _CURRENT_SIM
        t._parser = frame_parser
        return t

    async def send(self, data):
        if not self.connected:
            raise ConnectionError("transport closed")
        data = bytes(data)
        if data == DRV.HANDSHAKE_REQUEST:
            if _DROP_HANDSHAKE:
                return
            raw = (
                b"ESC/VP.net\x10\x03\x00\x00\x00\x00"  # status 0x00 = not ok
                if _REJECT_HANDSHAKE
                else DRV.HANDSHAKE_REPLY_OK
            )
        else:
            raw = self._sim.handle_command(data)
        if raw:
            for frame in self._parser.feed(raw):
                await self.on_data(frame)

    async def close(self):
        self.connected = False


class _FakeBaseDriver:
    """Functional stand-in for BaseDriver. epson_escvp overrides connect() and
    builds its own transport, so this only supplies the state/polling helpers."""

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False

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
    tcp = ModuleType("server.transport.tcp")
    tcp.TCPTransport = _FakeTCPTransport
    sys.modules["server.transport.tcp"] = tcp
    fp = ModuleType("server.transport.frame_parsers")
    fp.CallableFrameParser = _CallableFrameParser
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


DRV = _load("epson_escvp_under_test", DRIVER_PATH)
SIM = _load("epson_escvp_sim_under_test", SIM_PATH)


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(driver_overrides=None):
    global _CURRENT_SIM, _DROP_HANDSHAKE, _REJECT_HANDSHAKE
    _DROP_HANDSHAKE = False
    _REJECT_HANDSHAKE = False
    sim = SIM.EpsonEscVpSimulator("sim1", {})
    _CURRENT_SIM = sim

    cfg = {"host": "10.0.0.9", "port": 3629, "poll_interval": 0}
    cfg.update(driver_overrides or {})
    driver = DRV.EpsonEscVpDriver("proj1", cfg, _FakeState(), _FakeEvents())
    return driver, sim


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_version_bumped():
    assert DRV.EpsonEscVpDriver.DRIVER_INFO["version"] == "1.4.0"


def test_device_settings_declared():
    info = DRV.EpsonEscVpDriver.DRIVER_INFO
    ds = info["device_settings"]
    assert set(ds) == {"aspect", "color_mode"}
    # The read-back state_keys are the decoded friendly names, not the raw hex.
    assert ds["aspect"]["state_key"] == "aspect_name"
    assert ds["color_mode"]["state_key"] == "color_mode_name"
    state_vars = info["state_variables"]
    for spec in ds.values():
        assert spec["state_key"] in state_vars
        assert spec["type"] == "enum" and spec["values"]
        assert spec["setup"] is False
    # brightness / contrast are NOT settings — ESC/VP21 has no query verb.
    assert "brightness" not in ds and "contrast" not in ds
    # Transient set_* commands stay (dual surface).
    assert "set_aspect" in info["commands"]
    assert "set_color_mode" in info["commands"]


def test_quick_actions_reference_real_commands():
    info = DRV.EpsonEscVpDriver.DRIVER_INFO
    cmds = set(info["commands"])
    for qa in info["quick_actions"]:
        assert qa in cmds, f"quick_action {qa} is not a real command"


def test_discovery_tcp_probe_declared():
    disc = DRV.EpsonEscVpDriver.DRIVER_INFO["discovery"]
    probe = disc["tcp_probe"]
    assert probe["port"] == 3629
    # send = the 16-byte handshake; expect = the "ESC/VP.net" banner prefix.
    assert probe["send_hex"] == DRV.HANDSHAKE_REQUEST.hex()
    assert probe["expect_hex"] == b"ESC/VP.net".hex()
    assert disc["manufacturer_alias"] == ["EPSON", "Seiko Epson"]


# ── Round-trip: connect populates state (handshake + poll) ───────────────────

def test_connect_populates_state():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            assert driver._connected is True
            assert driver.get_state("power_state") == "on"
            # Aspect/CMODE hex decoded into the friendly *_name read-back.
            assert driver.get_state("aspect") == "00"
            assert driver.get_state("aspect_name") == "Normal"
            assert driver.get_state("color_mode") == "04"
            assert driver.get_state("color_mode_name") == "Presentation"
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Device settings: write + read-back through the decoded state_key ──────────

def test_aspect_device_setting_round_trip():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.set_device_setting("aspect", "16:9")
            assert sim.get_state("ASPECT") == "20"        # friendly -> hex
            driver.state.data.pop("aspect_name", None)
            await driver.poll()
            assert driver.get_state("aspect") == "20"      # raw hex read-back
            assert driver.get_state("aspect_name") == "16:9"  # decoded read-back
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_color_mode_device_setting_round_trip():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.set_device_setting("color_mode", "Theatre")
            assert sim.get_state("CMODE") == "05"
            driver.state.data.pop("color_mode_name", None)
            await driver.poll()
            assert driver.get_state("color_mode") == "05"
            assert driver.get_state("color_mode_name") == "Theatre"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_unknown_device_setting_raises():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            with pytest.raises(ValueError):
                await driver.set_device_setting("brightness", 50)
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Connection-fault fix: handshake failures are classifier-worded ──────────

def test_handshake_rejected_is_auth_failed():
    async def go():
        global _REJECT_HANDSHAKE
        driver, sim = await _make_pair()
        _REJECT_HANDSHAKE = True
        with pytest.raises(ConnectionError) as ei:
            await driver.connect()
        # The classifier maps "authentication failed" -> auth_failed.
        assert "authentication failed" in str(ei.value).lower()

    asyncio.run(go())


def test_handshake_timeout_is_no_response(monkeypatch):
    # Shrink the driver's 5s handshake wait so the drop path resolves fast.
    async def _fast(aw, timeout):
        return await _ORIG_WAIT_FOR(aw, 0.1)

    monkeypatch.setattr(DRV.asyncio, "wait_for", _fast)

    async def go():
        global _DROP_HANDSHAKE
        driver, sim = await _make_pair()
        _DROP_HANDSHAKE = True
        with pytest.raises(ConnectionError) as ei:
            await driver.connect()
        # The classifier maps "not responding" -> no_response.
        assert "not responding" in str(ei.value).lower()

    asyncio.run(go())
