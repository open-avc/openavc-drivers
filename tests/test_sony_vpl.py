"""Driver + simulator tests for sony_vpl (Sony VPL professional, ADCP).

No Sony projector on hand, so correctness is proven two ways: metadata/shape
assertions, and a **dual-proof round trip** wiring the real driver to the real
simulator over an in-memory TCP transport — the sim renders the ADCP reply
lines, the driver parses them, both sides asserted (same approach as
test_ptzoptics.py, but ADCP is line-based on TCP/53595 behind a SHA-256
challenge-response greeting).

Covers the v1.4.0 additions:
  - device settings: contrast / brightness / color / sharpness (0-100) and
    picture_mode / aspect write + read back through the pending-queue state_key,
    alongside the transient set_* commands they route to;
  - quick actions + a kind:setup "Test ADCP Password" wizard that runs
    out-of-band against a real socket;
  - the auth path itself (NOKEY, challenge success, challenge failure worded
    for the connection-fault classifier).

Loads the driver + simulator with the ``server.*`` / ``simulator.*`` imports
stubbed so the community CI stays self-contained (conftest.py rolls the stubs
back after this module is collected).
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "projectors" / "sony_vpl.py"
SIM_PATH = REPO_ROOT / "projectors" / "sony_vpl_sim.py"


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

    ADCP is line-based (CR+LF). The driver builds its own transport in
    connect() via TCPTransport.create(), so this stub's create() registers a
    client on the sim, delivers the sim's greeting (challenge or NOKEY) through
    on_data, and returns a transport whose send() feeds the sim and pipes each
    reply line back.
    """

    def __init__(self) -> None:
        self.on_data = None
        self.on_disconnect = None
        self.connected = False
        self._sim = None

    @classmethod
    async def create(cls, *, host, port, on_data, on_disconnect,
                     delimiter=b"\r\n", inter_command_delay=0.0,
                     timeout=5.0, name=""):
        t = cls()
        t.on_data = on_data
        t.on_disconnect = on_disconnect
        t.connected = True
        t._sim = _CURRENT_SIM
        # Mimic a real client connecting: register it and deliver the greeting.
        _CURRENT_SIM._clients["c1"] = object()
        greeting = await _CURRENT_SIM.on_client_connected("c1")
        if greeting:
            await t.on_data(greeting)
        return t

    async def send(self, data):
        if not self.connected:
            raise ConnectionError("transport closed")
        resp = self._sim.handle_command(bytes(data))
        if resp:
            await self.on_data(resp)

    async def close(self):
        self.connected = False


class _FakeBaseDriver:
    """Functional stand-in for BaseDriver. sony_vpl overrides connect() and
    builds its own transport, so this only supplies the state/polling/setup
    helpers the driver calls."""

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False
        self._setup_context = None
        self.config_updates: list[dict] = []
        self.reconnects = 0

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

    async def request_config_update(self, delta) -> None:
        self.config_updates.append(delta)
        self.config.update(delta)

    async def request_reconnect(self) -> None:
        self.reconnects += 1


class _FakeTCPSimulator:
    """Stand-in for simulator.tcp_simulator.TCPSimulator.

    SonyVplSimulator uses ``self.state`` (dict), ``self.set_state``,
    ``self._clients`` and ``self.config`` from the base — provide those.
    """

    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self.state = dict(self.SIMULATOR_INFO.get("initial_state", {}))
        self._clients: dict = {}

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


DRV = _load("sony_vpl_under_test", DRIVER_PATH)
SIM = _load("sony_vpl_sim_under_test", SIM_PATH)


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(driver_overrides=None, sim_password="", power="on"):
    global _CURRENT_SIM
    sim = SIM.SonyVplSimulator("sim1", {"password": sim_password})
    sim.set_state("power_status", power)
    _CURRENT_SIM = sim

    cfg = {"host": "10.0.0.9", "port": 53595, "poll_interval": 0,
           "password": ""}
    cfg.update(driver_overrides or {})
    driver = DRV.SonyVPLDriver("proj1", cfg, _FakeState(), _FakeEvents())
    return driver, sim


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_version_bumped():
    assert DRV.SonyVPLDriver.DRIVER_INFO["version"] == "1.4.0"


def test_device_settings_declared():
    ds = DRV.SonyVPLDriver.DRIVER_INFO["device_settings"]
    assert set(ds) == {
        "contrast", "brightness", "color", "sharpness", "picture_mode", "aspect",
    }
    state_vars = DRV.SonyVPLDriver.DRIVER_INFO["state_variables"]
    for key, spec in ds.items():
        assert spec["state_key"] in state_vars, f"{key} state_key not polled"
        assert spec["setup"] is False
    # Numeric settings carry the 0-100 bounds for the editable field.
    for key in ("contrast", "brightness", "color", "sharpness"):
        assert ds[key]["type"] == "integer"
        assert ds[key]["min"] == 0 and ds[key]["max"] == 100
    assert ds["picture_mode"]["type"] == "enum" and ds["picture_mode"]["values"]
    assert ds["aspect"]["type"] == "enum" and ds["aspect"]["values"]
    # Transient set_* commands stay (dual surface for macros).
    cmds = DRV.SonyVPLDriver.DRIVER_INFO["commands"]
    for c in ("set_contrast", "set_brightness", "set_color", "set_sharpness",
              "set_picture_mode", "set_aspect"):
        assert c in cmds


def test_actions_reference_real_commands_and_setup():
    info = DRV.SonyVPLDriver.DRIVER_INFO
    cmds = set(info["commands"])
    setup_actions = []
    for a in info["actions"]:
        if a["kind"] == "command":
            assert a["id"] in cmds, f"action {a['id']} is not a real command"
        elif a["kind"] == "setup":
            setup_actions.append(a)
    assert len(setup_actions) == 1
    wiz = setup_actions[0]
    assert wiz["id"] == "test_adcp"
    assert "password" in wiz["params"] and "save" in wiz["params"]


# ── Round-trip: connect populates state (NOKEY / auth-disabled path) ─────────

def test_connect_nokey_populates_state():
    async def go():
        driver, sim = await _make_pair(sim_password="")  # NOKEY
        await driver.connect()
        try:
            assert driver.get_state("auth_required") is False
            assert driver.get_state("power_status") == "on"
            assert driver.get_state("contrast") == 50
            assert driver.get_state("picture_mode") == "standard"
            assert driver.get_state("aspect") == "normal"
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Auth path: SHA-256 challenge success / failure ──────────────────────────

def test_auth_challenge_success():
    async def go():
        driver, sim = await _make_pair(
            sim_password="secret", driver_overrides={"password": "secret"})
        await driver.connect()
        try:
            assert driver._connected is True
            assert driver.get_state("auth_required") is True
            assert driver.get_state("power_status") == "on"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_auth_challenge_failure_is_classifier_worded():
    async def go():
        driver, sim = await _make_pair(
            sim_password="secret", driver_overrides={"password": "wrong"})
        with pytest.raises(ConnectionError) as exc:
            await driver.connect()
        # The classifier maps "authentication failed" -> auth_failed.
        assert "authentication failed" in str(exc.value).lower()

    asyncio.run(go())


# ── Device settings: write + read-back through the state_key ─────────────────

_DS_CASES = [
    ("contrast", 75, "contrast", 75),
    ("brightness", 30, "brightness", 30),
    ("color", 62, "color", 62),
    ("sharpness", 12, "sharpness", 12),
    ("picture_mode", "cinema", "picture_mode", "cinema"),
    ("aspect", "16_9", "aspect", "16_9"),
]


@pytest.mark.parametrize("key,value,sim_key,expected", _DS_CASES)
def test_device_setting_round_trip(key, value, sim_key, expected):
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.set_device_setting(key, value)
            assert sim.get_state(sim_key) == expected
            # Independent read-back through a fresh poll.
            driver.state.data.pop(f"{key}", None)
            await driver.poll()
            assert driver.get_state(key) == expected
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_numeric_device_setting_coerces_string():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            # The settings editor can hand back a numeric string.
            await driver.set_device_setting("contrast", "88")
            assert sim.get_state("contrast") == 88
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


# ── Setup wizard: real out-of-band socket ───────────────────────────────────

async def _adcp_server(password: str):
    """A tiny ADCP-greeting server for the setup-wizard tests.

    Sends a challenge (or NOKEY when password is empty), validates the SHA-256
    digest, replies ok / err_auth. Returns (host, port, stop_coro).
    """
    async def handle(reader, writer):
        if password:
            challenge = "0011aabb"
            writer.write(f"{challenge}\r\n".encode())
            await writer.drain()
            line = (await reader.readline()).decode().strip()
            expected = hashlib.sha256(
                f"{challenge} {password}".encode()).hexdigest()
            writer.write(b"ok\r\n" if line == expected else b"err_auth\r\n")
            await writer.drain()
        else:
            writer.write(b"NOKEY\r\n")
            await writer.drain()
        await asyncio.sleep(0.05)
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    return server, host, port


async def _run_wizard(password_on_device, password_typed, save=False):
    driver, _sim = await _make_pair()
    server, host, port = await _adcp_server(password_on_device)
    driver.config["host"] = host
    driver.config["port"] = port
    steps: list = []

    async def progress(step, pct=None):
        steps.append((step, pct))

    try:
        result = await driver.run_setup_action(
            "test_adcp", {"password": password_typed, "save": save}, progress)
    finally:
        server.close()
        await server.wait_closed()
    return driver, result


def test_setup_wizard_password_accepted():
    async def go():
        driver, result = await _run_wizard("secret", "secret", save=True)
        assert result["reachable"] is True
        assert result["auth_enabled"] is True
        assert result["auth_ok"] is True
        assert result["saved"] is True
        # Saving persisted the tested password and reconnected.
        assert driver.config["password"] == "secret"
        assert driver.reconnects == 1

    asyncio.run(go())


def test_setup_wizard_wrong_password_raises():
    async def go():
        with pytest.raises(ConnectionError) as exc:
            await _run_wizard("secret", "wrong")
        assert "rejected" in str(exc.value).lower()

    asyncio.run(go())


def test_setup_wizard_nokey_no_auth_needed():
    async def go():
        driver, result = await _run_wizard("", "", save=False)
        assert result["auth_enabled"] is False
        assert result["auth_ok"] is True
        assert result["saved"] is False

    asyncio.run(go())


def test_setup_wizard_unreachable_raises():
    async def go():
        driver, _sim = await _make_pair()
        # Point at a closed port (nothing listening).
        driver.config["host"] = "127.0.0.1"
        driver.config["port"] = 1  # privileged/closed — connect refused

        async def progress(step, pct=None):
            pass

        with pytest.raises(ConnectionError):
            await driver.run_setup_action(
                "test_adcp", {"password": "x", "save": False}, progress)

    asyncio.run(go())
