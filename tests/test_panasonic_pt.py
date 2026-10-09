"""Driver + simulator tests for panasonic_pt (NTCONTROL / Protocol 2).

No Panasonic projector on hand, so correctness is proven two ways: metadata/
shape assertions, and a dual-proof round trip wiring the real driver to the real
simulator over an in-memory TCP transport that mimics NTCONTROL's server-first
greeting (same approach as test_sony_vpl.py).

Covers a refused credential: a wrong or blank one fails the connect as a
typed auth_failed, a refusal (ERRA) once connected drops the connection at
once and nothing more is sent, and a greeting that is not NTCONTROL's is a
protocol mismatch (no_response), not a refused credential.

Covers the v1.4.0 additions:
  - device settings: input plus the full picture surface — brightness /
    contrast / color / tint / sharpness — write + read back through the
    pending-queue state_key (VXX setters + QVx queries confirmed in the
    Panasonic Control Command List);
  - quick actions + a kind:setup "Test Admin Credentials" wizard that runs the
    MD5 session-challenge auth out-of-band;
  - the connect-only NTCONTROL greeting discovery probe.

Loads the driver + simulator with the ``openavc.*`` imports
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
from _platform_stubs import (
    ConnectionFaultError as _FakeConnectionFaultError,
    StubEvents as _FakeEvents,
    StubState as _FakeState,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "projectors" / "panasonic_pt.py"
SIM_PATH = REPO_ROOT / "projectors" / "panasonic_pt_sim.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

_CURRENT_SIM: object | None = None
# When a list, replies are held here instead of delivered, until flush():
# a projector that answers after later commands have already gone out.
_HELD: list | None = None


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
        if _HELD is not None:
            _HELD.append(resp)
            return
        if resp:
            await self.on_data(resp)

    async def flush(self) -> None:
        global _HELD
        held, _HELD = _HELD or [], None
        for resp in held:
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
        # openavc.transport import at test-run time would miss the stubs
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
        self._active_errors: set = set()

    def set_state(self, key, value) -> None:
        self.state[key] = value

    def get_state(self, key, default=None):
        return self.state.get(key, default)

    @property
    def active_errors(self) -> set:
        return set(self._active_errors)

    def inject_error(self, mode) -> None:
        # As the platform's: mark the mode active and apply its set_state.
        self._active_errors.add(mode)
        modes = self.SIMULATOR_INFO.get("error_modes", {})
        for key, value in modes[mode].get("set_state", {}).items():
            self.set_state(key, value)


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


DRV = _load("panasonic_pt_under_test", DRIVER_PATH)
SIM = _load("panasonic_pt_sim_under_test", SIM_PATH)


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(driver_overrides=None, sim_password="", power="on"):
    global _CURRENT_SIM, _HELD
    _HELD = None
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
    assert DRV.PanasonicPTDriver.DRIVER_INFO["version"] == "1.4.4"
    assert DRV.PanasonicPTDriver.DRIVER_INFO["min_platform_version"] == "0.25.0"


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


# ── A refused credential ────────────────────────────────────────────────────
#
# Protocol 2 protected mode answers a hash it does not accept with the bare
# token ERRA, "Mismatching state of a password" (LAN Control Protocol, Table
# 3-4). The hash goes with every command, so ERRA is a refused credential.

def _record_sent(sim):
    """Every line the simulator receives, and whether it carried a session
    hash and was refused."""
    sent: list[tuple[str, bool]] = []
    inner = sim.handle_command

    def recording(data):
        resp = inner(data)
        line = bytes(data).decode("ascii", "replace").strip()
        hashed = len(line) > 32 and all(
            c in "0123456789abcdef" for c in line[:32])
        sent.append((line[32:] if hashed else line,
                     hashed and resp is not None and resp.strip() == b"ERRA"))
        return resp

    sim.handle_command = recording
    return sent


def test_refusal_mid_session_drops_at_once_as_auth_failed():
    async def go():
        driver, sim = await _make_pair(
            sim_password="secret", power="on",
            driver_overrides={"password": "secret"})
        await driver.connect()
        assert driver._connected is True
        sent = _record_sent(sim)
        sim.inject_error("auth_fail")

        # With the projector on, a poll is ten queries; the power answer is
        # awaited first, and it is the refusal.
        await driver.poll()
        await driver.poll()
        await asyncio.sleep(0)

        assert getattr(driver, "stashed_fault", None) is not None, (
            "a refused hash left the connection up")
        code, message = driver.stashed_fault
        assert code == "auth_failed"
        assert "ERRA" in message
        assert sent == [("00QPW", True)], sent
        assert driver._connected is False
        assert driver.transport is None
        assert "device.disconnected.proj1" in driver.events.emitted

    asyncio.run(go())


def test_command_refused_mid_session_drops_and_sends_no_follow_up():
    async def go():
        driver, sim = await _make_pair(
            sim_password="secret", driver_overrides={"password": "secret"})
        await driver.connect()
        sent = _record_sent(sim)
        sim.inject_error("auth_fail")

        # power_on is PON then a QPW read-back; only PON goes out.
        with pytest.raises(ConnectionError):
            await driver.send_command("power_on")
        await asyncio.sleep(0)

        assert driver.stashed_fault[0] == "auth_failed"
        assert sent == [("00PON", True)], sent

    asyncio.run(go())


def test_wrong_password_fails_the_connect_as_auth_failed():
    async def go():
        driver, sim = await _make_pair(
            sim_password="secret", power="on",
            driver_overrides={"password": "wrong"})
        sent = _record_sent(sim)
        with pytest.raises(ConnectionError) as exc:
            await driver.connect()
        assert exc.value.fault_code == "auth_failed"
        assert "ERRA" in str(exc.value)
        # One command judged the hash; the device never reported connected.
        assert sent == [("00QPW", True)], sent
        assert driver._connected is False
        assert driver.transport is None
        assert "device.connected.proj1" not in driver.events.emitted

    asyncio.run(go())


def test_blank_password_in_protected_mode_sends_nothing():
    async def go():
        # Protected mode means a password is set on the projector; clearing it
        # puts the projector in non-protected mode. A blank one cannot pass.
        driver, sim = await _make_pair(
            sim_password="secret", driver_overrides={"password": ""})
        sent = _record_sent(sim)
        with pytest.raises(ConnectionError) as exc:
            await driver.connect()
        assert exc.value.fault_code == "auth_failed"
        assert sent == []

    asyncio.run(go())


def test_refusal_during_initial_sync_fails_the_connect_once():
    """A refusal that lands while connect() is still running its first sweep
    is raised from that stage: the connect fails typed and is torn down once,
    rather than dropped underneath a connect that then carries on."""
    async def go():
        driver, sim = await _make_pair(
            sim_password="secret", power="on",
            driver_overrides={"password": "secret"})
        inner = sim.handle_command
        seen_qpw = []

        def change_on_second_qpw(data):
            if bytes(data).endswith(b"00QPW\r"):
                seen_qpw.append(1)
                if len(seen_qpw) == 2:
                    sim.inject_error("auth_fail")
            return inner(data)

        sim.handle_command = change_on_second_qpw
        sent = _record_sent(sim)
        with pytest.raises(ConnectionError) as exc:
            await driver.connect()
        await asyncio.sleep(0)

        assert exc.value.fault_code == "auth_failed"
        assert [refused for _, refused in sent].count(True) == 1, sent
        assert getattr(driver, "stashed_fault", None) is None
        assert driver.events.emitted.count("device.disconnected.proj1") == 1

    asyncio.run(go())


def test_a_greeting_that_is_not_ntcontrol_is_not_auth_failed():
    """Something else answering on the port (here a Panasonic display left on
    Protocol 1) is a protocol mismatch: no_response, which keeps reconnecting,
    never auth_failed, which would pause it."""
    async def go():
        driver, sim = await _make_pair(
            sim_password="", driver_overrides={"password": "secret"})

        async def display_greeting(client_id):
            return b"PDPCONTROL 0\r"

        sim.on_client_connected = display_greeting
        sent = _record_sent(sim)
        with pytest.raises(ConnectionError) as exc:
            await driver.connect()
        assert exc.value.fault_code == "no_response"
        assert "PDPCONTROL 0" in str(exc.value)
        assert sent == []

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


# ── Responses: one per command, matched by order ────────────────────────────
#
# Protocol 2 answers a control command with its echo and a query with its
# bare value, which "it is not known what the sent command was" (LAN Control
# Protocol 4.3). The driver once queued only its queries, so a control's
# echo arriving while queries waited popped a query's slot and every later
# value landed on the wrong state.

def test_a_control_echo_does_not_shift_the_values_after_it(monkeypatch):
    monkeypatch.setattr(DRV, "_VERDICT_TIMEOUT_S", 0.01)

    async def go():
        global _HELD
        driver, sim = await _make_pair()
        sim.set_state("brightness", 40)
        sim.set_state("contrast", 21)
        await driver.connect()
        try:
            _HELD = []
            await driver.send_command("mute_video")
            await driver.poll()
            await driver.transport.flush()
            assert driver.get_state("mute_video") is True
            assert driver.get_state("power") == "on"
            assert driver.get_state("brightness") == 40
            assert driver.get_state("contrast") == 21
            assert driver._pending == []
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_a_send_that_fails_mid_poll_reaches_the_watchdog():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            async def dead(data) -> None:
                raise ConnectionError("transport closed")

            driver.transport.send = dead
            with pytest.raises(ConnectionError):
                await driver.poll()
        finally:
            await driver.disconnect()

    asyncio.run(go())
