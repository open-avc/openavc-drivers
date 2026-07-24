"""Driver + simulator tests for pjlink_class1 (PJLink Class 1, cross-vendor).

The driver is hardware-verified; these tests lock in the v2.5.0 adoption without
regressing it. Correctness is a dual-proof round trip wiring the real driver to
the real simulator over an in-memory TCP transport that delivers PJLink's
server-first greeting (same approach as test_sony_vpl.py).

Covers:
  - the connection-fault FIX: "PJLINK ERRA" (auth rejected) starts with
    "PJLINK", so the old greeting check swallowed it as a no-auth greeting and
    the device stayed "connected" on a bad password. connect() now probes and
    fails with an auth_failed-worded ConnectionError (a regression that would
    have caught the swallowed-ERRA bug);
  - the input param picker: INST is published as a {value,label} JSON list the
    Set Input dropdown reads via options_state;
  - quick actions; and that DS is (correctly) absent.

Loads the driver + simulator with the ``server.*`` / ``simulator.*`` imports
stubbed so the community CI stays self-contained (conftest.py rolls the stubs
back after this module is collected).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "projectors" / "pjlink_class1.py"
SIM_PATH = REPO_ROOT / "projectors" / "pjlink_class1_sim.py"


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
    """Stand-in for TCPTransport speaking PJLink to the live sim: the base's
    _create_transport builds it during connect(), so create() registers a
    client, delivers the server-first greeting (BEFORE _post_connect awaits
    it, same as the real transport), and pipes replies back."""

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


class _FakeBaseDriver:
    """Functional stand-in for the platform BaseDriver: the driver supplies
    lifecycle hooks (_pre_connect / _post_connect / _initial_sync /
    _close_session) and connect()/disconnect() here run them in the
    platform's order — clean slate, _pre_connect, transport build,
    _post_connect (with failure teardown), declare, _initial_sync (with full
    teardown on failure), then polling. _close_session runs on EVERY
    teardown path, including transport drops."""

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False
        self._bg_tasks: set = set()

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

    # -- lifecycle hooks (drivers override; defaults are no-ops) --

    async def _pre_connect(self) -> None:
        pass

    async def _post_connect(self) -> None:
        pass

    async def _initial_sync(self) -> None:
        pass

    async def _close_session(self) -> None:
        pass

    def _transport_kwargs(self, transport_type, kwargs):
        return kwargs

    def _create_frame_parser(self):
        return None

    def _resolve_delimiter(self):
        return b"\r"  # platform default

    async def _create_transport(self, transport_type) -> None:
        kwargs = dict(
            host=self.config.get("host", ""),
            port=self.config.get("port", 4352),
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


DRV = _load("pjlink_under_test", DRIVER_PATH)
SIM = _load("pjlink_sim_under_test", SIM_PATH)


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(driver_overrides=None, sim_password=""):
    global _CURRENT_SIM
    sim = SIM.PjlinkClass1Simulator("sim1", {"password": sim_password})
    _CURRENT_SIM = sim
    cfg = {"host": "10.0.0.9", "port": 4352, "poll_interval": 0, "password": ""}
    cfg.update(driver_overrides or {})
    driver = DRV.PJLinkDriver("proj1", cfg, _FakeState(), _FakeEvents())
    return driver, sim


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_version_bumped():
    assert DRV.PJLinkDriver.DRIVER_INFO["version"] == "2.5.1"
    assert DRV.PJLinkDriver.DRIVER_INFO["min_platform_version"] == "0.24.0"


def test_no_device_settings():
    # PJLink Class 1 has no persisted, device-reported writable value — DS is
    # correctly absent.
    assert "device_settings" not in DRV.PJLinkDriver.DRIVER_INFO


def test_quick_actions_reference_real_commands():
    info = DRV.PJLinkDriver.DRIVER_INFO
    cmds = set(info["commands"])
    for qa in info["quick_actions"]:
        assert qa in cmds, f"quick_action {qa} is not a real command"


def test_set_input_has_options_picker():
    info = DRV.PJLinkDriver.DRIVER_INFO
    param = info["commands"]["set_input"]["params"]["input"]
    assert param["options_state"] == "input_options"
    assert "input_options" in info["state_variables"]


# ── Round-trip: connect populates state + the input picker (PP) ─────────────

def test_connect_publishes_input_options():
    async def go():
        driver, sim = await _make_pair()  # no auth
        await driver.connect()
        try:
            assert driver._connected is True
            # INST -> input_options JSON, {value=code, label=friendly}.
            raw = driver.get_state("input_options")
            options = json.loads(raw)
            values = [o["value"] for o in options]
            assert values == ["11", "12", "31", "32", "51"]
            labels = {o["value"]: o["label"] for o in options}
            assert labels["31"] == "digital1"
            # The friendly display list is still published.
            assert "digital1" in driver.get_state("available_inputs")
            # The base declares connected via the canonical event topic.
            assert "device.connected.proj1" in driver.events.emitted
        finally:
            await driver.disconnect()
        assert "device.disconnected.proj1" in driver.events.emitted

    asyncio.run(go())


# ── Connection-fault FIX: PJLINK ERRA now fails the connect ─────────────────

def test_wrong_password_is_auth_failed():
    async def go():
        driver, sim = await _make_pair(
            sim_password="secret", driver_overrides={"password": "wrong"})
        with pytest.raises(ConnectionError) as exc:
            await driver.connect()
        # The classifier maps "authentication failed" -> auth_failed.
        assert "authentication failed" in str(exc.value).lower()
        # The bug: ERRA was swallowed as a no-auth greeting. The raise above
        # proves the fix flagged the failure. The failed _post_connect then
        # tore the attempt down — transport closed and _close_session
        # disarmed the auth state for the next attempt.
        assert driver._connected is False
        assert driver.transport is None
        assert driver._auth_failed is False

    asyncio.run(go())


def test_correct_password_connects():
    async def go():
        driver, sim = await _make_pair(
            sim_password="secret", driver_overrides={"password": "secret"})
        await driver.connect()
        try:
            assert driver._connected is True
            assert driver._auth_failed is False
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_missing_password_when_required_is_auth_failed():
    async def go():
        # Projector requires auth; user left the password blank.
        driver, sim = await _make_pair(
            sim_password="secret", driver_overrides={"password": ""})
        with pytest.raises(ConnectionError) as exc:
            await driver.connect()
        assert "authentication failed" in str(exc.value).lower()

    asyncio.run(go())


def test_no_auth_connects():
    async def go():
        driver, sim = await _make_pair(sim_password="")  # PJLINK 0 greeting
        await driver.connect()
        try:
            assert driver._connected is True
        finally:
            await driver.disconnect()

    asyncio.run(go())
