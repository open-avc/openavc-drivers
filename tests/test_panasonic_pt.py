"""Driver + simulator tests for panasonic_pt (NTCONTROL / Protocol 2).

No Panasonic projector on hand, so correctness is proven two ways: metadata/
shape assertions, and a dual-proof round trip wiring the real driver to the real
simulator over an in-memory TCP transport that mimics NTCONTROL's server-first
greeting (same approach as test_sony_vpl.py).

Covers the v1.4.0 additions:
  - device settings: input plus the full picture surface — brightness /
    contrast / color / tint / sharpness — write + read back through the
    pending-queue state_key (VXX setters + QVx queries confirmed in the
    Panasonic Control Command List);
  - quick actions + a kind:setup "Test Admin Credentials" wizard that runs the
    MD5 session-challenge auth out-of-band;
  - the connect-only NTCONTROL greeting discovery probe.

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

from _lifecycle_fake import LifecycleFake

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "projectors" / "panasonic_pt.py"
SIM_PATH = REPO_ROOT / "projectors" / "panasonic_pt_sim.py"


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


class _FakeTCPTransport:
    """Stand-in for TCPTransport that speaks NTCONTROL to the live sim: the
    base's _create_transport builds it during connect(), so create() registers
    a client on the sim, delivers the server-first greeting (BEFORE
    _post_connect awaits the auth verdict, same as the real transport), and
    pipes each reply line back."""

    def __init__(self) -> None:
        self.on_data = None
        self.connected = False
        self.delimiter = b"\r"
        self._sim = None

    @classmethod
    async def create(cls, *, host, port, on_data, on_disconnect,
                     delimiter=b"\r", frame_parser=None,
                     inter_command_delay=0.0, timeout=5.0, name=""):
        t = cls()
        t.on_data = on_data
        t.connected = True
        t.delimiter = delimiter
        t._sim = _CURRENT_SIM
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


class _FakeBaseDriver(LifecycleFake):
    """Functional stand-in for the platform BaseDriver: the driver supplies
    lifecycle hooks (_pre_connect / _post_connect / _initial_sync /
    _close_session / _transport_kwargs) and connect()/disconnect() here run
    them in the platform's order — clean slate, _pre_connect, transport
    build, _post_connect (with failure teardown), declare, _initial_sync
    (with full teardown on failure), then polling."""

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False
        self._setup_context = None
        self._bg_tasks: set = set()
        self.config_updates: list[dict] = []
        self.reconnects = 0

    def set_state(self, key, value) -> None:
        self.state.set(key, value)

    def set_states(self, updates) -> None:
        for k, v in updates.items():
            self.state.set(k, v)

    def get_state(self, key, default=None):
        return self.state.data.get(key, default)

    async def request_config_update(self, delta) -> None:
        self.config_updates.append(delta)
        self.config.update(delta)

    async def request_reconnect(self) -> None:
        self.reconnects += 1

    # -- lifecycle hooks (drivers override; defaults are no-ops) --

    async def _pre_connect(self) -> None:
        pass

    async def _post_connect(self) -> None:
        pass

    async def _initial_sync(self) -> None:
        pass

    async def _close_session(self) -> None:
        pass

    def _resolve_delimiter(self):
        return b"\r"  # platform default

    async def _create_transport(self, transport_type) -> None:
        kwargs = dict(
            host=self.config.get("host", ""),
            port=self.config.get("port", 1024),
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            delimiter=self._resolve_delimiter(),
            frame_parser=self._create_frame_parser(),
            timeout=self.config.get("timeout", 5.0),
            inter_command_delay=self.config.get("inter_command_delay", 0.0),
            name=self.device_id,
        )
        # Reference the module-level fake directly — a deferred
        # server.transport import at test-run time would miss the stubs
        # (conftest rolls them back after collection).
        self.transport = await _FakeTCPTransport.create(
            **self._transport_kwargs(transport_type, kwargs))

    # -- connection lifecycle (mirrors BaseDriver.connect/disconnect) --

    async def connect(self) -> None:
        # Clean slate: drop any stale session/transport from a previous
        # attempt before the hooks run.
        await self._close_session()
        if self.transport:
            try:
                await self.transport.close()
            except Exception:
                pass
            self.transport = None

        await self._pre_connect()
        await self._create_transport("tcp")

        try:
            await self._post_connect()
            self._connected = True
            self.set_state("connected", True)
            await self.events.emit(f"device.connected.{self.device_id}")
        except Exception:
            if self.transport:
                await self.transport.close()
                self.transport = None
            await self._close_session()
            self._connected = False
            raise

        try:
            await self._initial_sync()
        except Exception:
            transport = self.transport
            self.transport = None
            if transport is not None:
                try:
                    await transport.close()
                except Exception:
                    pass
            await self._close_session()
            self._connected = False
            self.set_state("connected", False)
            await self.events.emit(f"device.disconnected.{self.device_id}")
            raise

        if self.config.get("poll_interval", 0) > 0:
            await self.start_polling(self.config["poll_interval"])

    async def disconnect(self) -> None:
        await self.stop_polling()
        if self.transport:
            await self.transport.close()
            self.transport = None
        await self._close_session()
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")

    def _handle_transport_disconnect(self) -> None:
        # Platform behavior: flip the flags synchronously, then schedule the
        # async teardown (which also runs _close_session).
        self._connected = False
        self.set_state("connected", False)
        task = asyncio.get_running_loop().create_task(
            self._on_disconnect_cleanup())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _on_disconnect_cleanup(self) -> None:
        await self.stop_polling()
        transport = self.transport
        self.transport = None
        if transport is not None:
            await transport.close()
        await self._close_session()
        await self.events.emit(f"device.disconnected.{self.device_id}")


class _FakeTCPSimulator:
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


DRV = _load("panasonic_pt_under_test", DRIVER_PATH)
SIM = _load("panasonic_pt_sim_under_test", SIM_PATH)


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(driver_overrides=None, sim_password="", power="on"):
    global _CURRENT_SIM
    sim = SIM.PanasonicPtSimulator("sim1", {"password": sim_password})
    sim.set_state("power", power)
    _CURRENT_SIM = sim

    cfg = {"host": "10.0.0.9", "port": 1024, "poll_interval": 0,
           "username": "admin1", "password": ""}
    cfg.update(driver_overrides or {})
    driver = DRV.PanasonicPTDriver("proj1", cfg, _FakeState(), _FakeEvents())
    return driver, sim


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_version_bumped():
    assert DRV.PanasonicPTDriver.DRIVER_INFO["version"] == "1.4.1"
    assert DRV.PanasonicPTDriver.DRIVER_INFO["min_platform_version"] == "0.24.0"


def test_device_settings_declared():
    info = DRV.PanasonicPTDriver.DRIVER_INFO
    ds = info["device_settings"]
    assert set(ds) == {
        "input", "brightness", "contrast", "color", "tint", "sharpness",
    }
    state_vars = info["state_variables"]
    for key, spec in ds.items():
        assert spec["state_key"] in state_vars, key
        assert spec["setup"] is False
    assert ds["input"]["type"] == "enum"
    for k in ("brightness", "contrast", "color", "tint"):
        assert ds[k]["min"] == 1 and ds[k]["max"] == 63
    assert ds["sharpness"]["min"] == 0 and ds["sharpness"]["max"] == 15
    for c in ("brightness_set", "contrast_set", "color_set", "tint_set",
              "sharpness_set", "set_input"):
        assert c in info["commands"]


def test_actions_reference_real_commands_and_setup():
    info = DRV.PanasonicPTDriver.DRIVER_INFO
    cmds = set(info["commands"])
    setup = [a for a in info["actions"] if a["kind"] == "setup"]
    for a in info["actions"]:
        if a["kind"] == "command":
            assert a["id"] in cmds
    assert len(setup) == 1 and setup[0]["id"] == "test_ntcontrol"
    assert {"username", "password", "save"} <= set(setup[0]["params"])


def test_discovery_greeting_probe():
    disc = DRV.PanasonicPTDriver.DRIVER_INFO["discovery"]
    probe = disc["tcp_probe"]
    assert probe["port"] == 1024
    assert probe["expect"] == "NTCONTROL"
    # Connect-only banner read: no send declared.
    assert "send_ascii" not in probe and "send_hex" not in probe


# ── Round-trip: connect populates state ─────────────────────────────────────

def test_connect_populates_state():
    async def go():
        driver, sim = await _make_pair(
            sim_password="",
            power="on",
        )
        sim.state.update({"input": "HD2", "brightness": 40, "contrast": 55})
        await driver.connect()
        try:
            assert driver.get_state("auth_required") is False
            assert driver.get_state("power") == "on"
            assert driver.get_state("input") == "hdmi2"  # code -> friendly
            assert driver.get_state("brightness") == 40
            assert driver.get_state("contrast") == 55
            # The base declares connected via the canonical event topic.
            assert "device.connected.proj1" in driver.events.emitted
        finally:
            await driver.disconnect()
        assert "device.disconnected.proj1" in driver.events.emitted

    asyncio.run(go())


# ── Device settings: write + read-back ──────────────────────────────────────

_DS_CASES = [
    ("input", "hdmi2", "input", "hdmi2"),
    ("brightness", 40, "brightness", 40),
    ("contrast", 55, "contrast", 55),
    ("color", 20, "color", 20),
    ("tint", 33, "tint", 33),
    ("sharpness", 12, "sharpness", 12),
]


@pytest.mark.parametrize("key,value,state_key,expected", _DS_CASES)
def test_device_setting_round_trip(key, value, state_key, expected):
    async def go():
        driver, sim = await _make_pair(power="on")
        await driver.connect()
        try:
            await driver.set_device_setting(key, value)
            assert driver.get_state(state_key) == expected
            # Independent read-back through a fresh poll.
            driver.state.data.pop(state_key, None)
            await driver.poll()
            assert driver.get_state(state_key) == expected
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


# ── Setup wizard: real out-of-band NTCONTROL socket ─────────────────────────

async def _ntcontrol_server(username: str, password: str):
    """A tiny NTCONTROL-greeting server for the setup-wizard tests. Sends a
    protected challenge (or non-protected greeting when password is empty),
    validates the MD5 session prefix on the QPW command, replies 00000 / ERRA."""
    async def handle(reader, writer):
        if password:
            random_hex = "0011aabb"
            writer.write(f"NTCONTROL 1 {random_hex}\r".encode())
            await writer.drain()
            line = (await reader.readuntil(b"\r")).decode().strip()
            expected = hashlib.md5(
                f"{username}:{password}:{random_hex}".encode()).hexdigest()
            ok = line.startswith(expected) and line[32:] == "00QPW"
            writer.write(b"00000\r" if ok else b"ERRA\r")
            await writer.drain()
        else:
            writer.write(b"NTCONTROL 0\r")
            await writer.drain()
            await reader.readuntil(b"\r")  # 00QPW
            writer.write(b"00000\r")
            await writer.drain()
        await asyncio.sleep(0.05)
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    return server, host, port


async def _run_wizard(dev_user, dev_pass, typed_user, typed_pass, save=False):
    driver, _sim = await _make_pair()
    server, host, port = await _ntcontrol_server(dev_user, dev_pass)
    driver.config["host"] = host
    driver.config["port"] = port

    async def progress(step, pct=None):
        pass

    try:
        result = await driver.run_setup_action(
            "test_ntcontrol",
            {"username": typed_user, "password": typed_pass, "save": save},
            progress,
        )
    finally:
        server.close()
        await server.wait_closed()
    return driver, result


def test_setup_wizard_credentials_accepted():
    async def go():
        driver, result = await _run_wizard(
            "admin1", "secret", "admin1", "secret", save=True)
        assert result["auth_enabled"] is True
        assert result["auth_ok"] is True
        assert result["saved"] is True
        assert driver.config["password"] == "secret"
        assert driver.reconnects == 1

    asyncio.run(go())


def test_setup_wizard_wrong_password_raises():
    async def go():
        with pytest.raises(ConnectionError) as exc:
            await _run_wizard("admin1", "secret", "admin1", "wrong")
        assert "reject" in str(exc.value).lower()

    asyncio.run(go())


def test_setup_wizard_non_protected():
    async def go():
        driver, result = await _run_wizard("admin1", "", "admin1", "", save=False)
        assert result["auth_enabled"] is False
        assert result["auth_ok"] is True

    asyncio.run(go())
