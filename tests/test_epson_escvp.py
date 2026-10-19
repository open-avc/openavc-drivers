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
  - the connection-fault fix: a handshake timeout is typed no_response.

And the v1.5.0 additions:
  - the Monitor Password, sent in the handshake's 18-byte password header,
    checked by the simulator (0x20 / 0x41 / 0x43);
  - each refusal status typed to its own offline reason and sentence;
  - the refusal judged while the reply is parsed, so it is the offline reason
    even when the projector's close is seen first (the EB-L530U answers 0x45
    and closes at once).

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
    CallableFrameParser as _CallableFrameParser,
    ConnectionFaultError as _ConnectionFaultError,
    StubEvents as _FakeEvents,
    StubState as _FakeState,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "projectors" / "epson_escvp.py"
SIM_PATH = REPO_ROOT / "projectors" / "epson_escvp_sim.py"

_ORIG_WAIT_FOR = asyncio.wait_for


# ── Platform stand-ins ──────────────────────────────────────────────────────

# Harness knobs set per test.
_CURRENT_SIM: object | None = None
_DROP_HANDSHAKE = False   # projector never answers -> connect timeout
# When set, the raw handshake reply sent instead of the simulator's verdict.
_HANDSHAKE_REPLY: bytes | None = None
# When a list, replies are held here instead of delivered, until _flush():
# a projector that answers after later lines have already gone out.
_HELD: list | None = None
# Every transport the harness built, newest last.
_TRANSPORTS: list = []


class _FakeTCPTransport:
    """Stand-in for TCPTransport that speaks ESC/VP.net to the live sim.

    The driver sends the handshake first; the simulator judges it (its
    password check, its error modes) unless a harness knob drops it or forces
    a reply, then ESC/VP21 lines go to the sim's handle_command. Replies are
    fed through the driver's own frame parser so the handshake->colon framing
    state machine is exercised for real.
    """

    def __init__(self) -> None:
        self.on_data = None
        self.connected = False
        self._sim = None
        self._parser = None
        self.lines: list[bytes] = []
        self.handshake: bytes | None = None

    @classmethod
    async def create(cls, *, host, port, on_data, on_disconnect, delimiter=None,
                     frame_parser=None, inter_command_delay=0.0, timeout=5.0,
                     name=""):
        t = cls()
        t.on_data = on_data
        t.connected = True
        t._sim = _CURRENT_SIM
        t._parser = frame_parser
        _TRANSPORTS.append(t)
        return t

    async def send(self, data):
        if not self.connected:
            raise ConnectionError("transport closed")
        data = bytes(data)
        if self.handshake is None and data.startswith(b"ESC/VP.net"):
            self.handshake = data
            if _DROP_HANDSHAKE:
                return
            raw = _HANDSHAKE_REPLY
            if raw is None:
                status = self._sim._handshake_status(data[DRV.HANDSHAKE_LEN:])
                raw = SIM._handshake_reply(status)
        else:
            raw = self._sim.handle_command(data)
            self.lines.append(data)
            if _HELD is not None:
                _HELD.append(raw)
                return
        if raw:
            for frame in self._parser.feed(raw):
                await self.on_data(frame)

    async def flush(self) -> None:
        global _HELD
        held, _HELD = _HELD or [], None
        for raw in held:
            if raw:
                for frame in self._parser.feed(raw):
                    await self.on_data(frame)

    async def close(self):
        self.connected = False


class _FakeBaseDriver(LifecycleFake):
    """Functional stand-in for BaseDriver with the platform's hook-driven
    connection lifecycle (epson_escvp arms its handshake state machine in
    _pre_connect, runs the ESC/VP.net handshake in _post_connect, and
    disarms in _close_session on every teardown)."""

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

    def _handle_transport_disconnect(self) -> None:
        self._connected = False
        self.set_state("connected", False)
        if self.transport is not None:
            self.transport.connected = False

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
            port=self.config.get("port", 3629),
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
        # 1. Clean slate: drop a previous attempt's driver session and
        #    stale transport.
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
        # 7. Polling (epson supplies no liveness probe, so no watchdog).
        if self.config.get("poll_interval", 0):
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


class _FakeTCPSimulator:
    """Stand-in for openavc.simulator.tcp_simulator.TCPSimulator."""

    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self.state = dict(self.SIMULATOR_INFO.get("initial_state", {}))
        self.active_errors: set[str] = set()

    def set_state(self, key, value) -> None:
        self.state[key] = value

    def get_state(self, key, default=None):
        return self.state.get(key, default)


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
    base.ConnectionFaultError = _ConnectionFaultError
    sys.modules["openavc.drivers.base"] = base
    tcp = ModuleType("openavc.transport.tcp")
    tcp.TCPTransport = _FakeTCPTransport
    sys.modules["openavc.transport.tcp"] = tcp
    fp = ModuleType("openavc.transport.frame_parsers")
    fp.CallableFrameParser = _CallableFrameParser
    sys.modules["openavc.transport.frame_parsers"] = fp
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


DRV = _load("epson_escvp_under_test", DRIVER_PATH)
SIM = _load("epson_escvp_sim_under_test", SIM_PATH)


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(driver_overrides=None, sim_config=None):
    global _CURRENT_SIM, _DROP_HANDSHAKE, _HANDSHAKE_REPLY
    _DROP_HANDSHAKE = False
    _HANDSHAKE_REPLY = None
    _TRANSPORTS.clear()
    sim = SIM.EpsonEscVpSimulator("sim1", sim_config or {})
    _CURRENT_SIM = sim

    cfg = {"host": "10.0.0.9", "port": 3629, "poll_interval": 0}
    cfg.update(driver_overrides or {})
    driver = DRV.EpsonEscVpDriver("proj1", cfg, _FakeState(), _FakeEvents())
    return driver, sim


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_version_bumped():
    assert DRV.EpsonEscVpDriver.DRIVER_INFO["version"] == "1.5.0"


def test_monitor_password_field_is_secret_with_no_default():
    info = DRV.EpsonEscVpDriver.DRIVER_INFO
    field = info["config_schema"]["password"]
    assert field["type"] == "string"
    assert field["secret"] is True
    assert field["label"] == "Monitor Password"
    assert "default" not in field
    assert "password" not in info["default_config"]


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


# ── The handshake: password header and refusals ─────────────────────────────

def test_handshake_bytes_without_and_with_a_password():
    assert DRV.handshake_request("") == bytes.fromhex(
        "4553432f56502e6e6574100300000000"
    )
    # Header count 1, then identifier 0x01 (Password), attribute 0x01 (plain
    # text) and the password NUL-padded to 16 bytes: 34 bytes in all.
    assert DRV.handshake_request("abc12345") == bytes.fromhex(
        "4553432f56502e6e6574100300000001"
        "0101" "6162633132333435" "0000000000000000"
    )


def _connect_error(driver_overrides=None, sim_config=None, before=None):
    """Connect a fresh pair that is expected to fail; ``before(sim)`` runs
    after the harness knobs are reset and before the connect."""
    async def go():
        driver, sim = await _make_pair(driver_overrides, sim_config)
        if before is not None:
            before(sim)
        with pytest.raises(_ConnectionFaultError) as ei:
            await driver.connect()
        assert driver._connected is False
        return ei.value

    return asyncio.run(go())


def _reply_with(raw: bytes):
    def before(sim) -> None:
        global _HANDSHAKE_REPLY
        _HANDSHAKE_REPLY = raw

    return before


def test_a_matching_password_connects_and_is_sent_in_the_handshake():
    async def go():
        driver, sim = await _make_pair(
            {"password": "abc12345"}, sim_config={"password": "abc12345"},
        )
        await driver.connect()
        try:
            assert driver._connected is True
            assert driver.get_state("power_state") == "on"
            assert _TRANSPORTS[-1].handshake == DRV.handshake_request("abc12345")
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_no_password_on_a_projector_that_has_one_is_auth_failed():
    err = _connect_error(sim_config={"password": "abc12345"})
    assert err.fault_code == "auth_failed"
    assert str(err) == DRV.HANDSHAKE_REFUSALS[0x41][1]


def test_a_wrong_password_is_auth_failed():
    err = _connect_error(
        {"password": "wrong123"}, sim_config={"password": "abc12345"},
    )
    assert err.fault_code == "auth_failed"
    assert str(err) == DRV.HANDSHAKE_REFUSALS[0x43][1]


def test_the_invalid_sentinel_is_refused_by_a_projector_with_no_password():
    assert _connect_error({"password": "invalid"}).fault_code == "auth_failed"


def test_network_control_off_is_connection_refused():
    err = _connect_error(
        before=lambda sim: sim.active_errors.add("control_refused"),
    )
    # Retried, not paused: the integrator fixes it on the projector.
    assert err.fault_code == "connection_refused"
    assert "Command Communication to Compatible" in str(err)


def test_every_documented_refusal_has_its_own_sentence():
    codes = {status: code for status, (code, _) in DRV.HANDSHAKE_REFUSALS.items()}
    assert codes == {
        0x41: "auth_failed",
        0x43: "auth_failed",
        0x45: "connection_refused",
        0x53: "connection_refused",
        0x55: "connection_refused",
    }
    sentences = [message for _, message in DRV.HANDSHAKE_REFUSALS.values()]
    assert len(set(sentences)) == len(sentences)
    help_block = DRV.EpsonEscVpDriver.DRIVER_INFO["help"]
    field = DRV.EpsonEscVpDriver.DRIVER_INFO["config_schema"]["password"]
    shown = sentences + [
        DRV.PASSWORD_INVALID_MESSAGE,
        help_block["overview"], help_block["setup"], help_block["connection"],
        field["description"], field["help"],
    ]
    for text in shown:
        assert "—" not in text, text


def test_an_unlisted_status_is_reported_in_hex():
    err = _connect_error(
        before=_reply_with(b"ESC/VP.net\x10\x03\x00\x00\x40\x00"),
    )
    assert err.fault_code == "connection_refused"
    assert "0x40" in str(err)


def test_a_reply_that_is_not_esc_vp_net_is_no_response():
    err = _connect_error(before=_reply_with(b"HTTP/1.1 400 Bad"))
    assert err.fault_code == "no_response"


@pytest.mark.parametrize("password", ["a" * 17, "cafépass"])
def test_a_password_the_header_cannot_hold_is_refused_before_dialing(password):
    err = _connect_error({"password": password})
    assert err.fault_code == "invalid_config"
    assert str(err) == DRV.PASSWORD_INVALID_MESSAGE
    assert _TRANSPORTS == []


def test_handshake_timeout_is_no_response(monkeypatch):
    # Shrink the driver's 5s handshake wait so the drop path resolves fast.
    async def _fast(aw, timeout):
        return await _ORIG_WAIT_FOR(aw, 0.1)

    monkeypatch.setattr(DRV.asyncio, "wait_for", _fast)

    def drop(sim) -> None:
        global _DROP_HANDSHAKE
        _DROP_HANDSHAKE = True

    err = _connect_error(before=drop)
    assert err.fault_code == "no_response"
    assert "not responding" in str(err)


@pytest.mark.parametrize("status", sorted(DRV.HANDSHAKE_REFUSALS))
def test_a_refusal_is_stashed_while_the_reply_is_parsed(status):
    # An EB-L530U closes right after answering 0x41 or 0x45. When the reply
    # and the close arrive in one read, the transport reports the close
    # before an on_data_received task for the reply has run, so the verdict
    # has to exist once the parser has read the reply, with nothing else run.
    driver, _ = asyncio.run(_make_pair())
    parser = driver._create_frame_parser()
    frames = parser.feed(b"ESC/VP.net\x10\x03\x00\x00" + bytes([status, 0]))
    assert len(frames) == 1
    assert driver.stashed_fault == DRV.HANDSHAKE_REFUSALS[status]


def test_an_accepted_handshake_stashes_nothing():
    driver, _ = asyncio.run(_make_pair())
    parser = driver._create_frame_parser()
    parser.feed(DRV.HANDSHAKE_REPLY_OK)
    assert getattr(driver, "stashed_fault", None) is None


def test_headers_on_the_reply_are_not_read_as_esc_vp21():
    driver, _ = asyncio.run(_make_pair())
    parser = driver._create_frame_parser()
    header = b"\x03\x00" + b"PROJ1".ljust(16, b"\x00")   # a projector name
    frames = parser.feed(
        b"ESC/VP.net\x10\x03\x00\x00\x20\x01" + header + b"PWR=01\r:"
    )
    assert frames[0] == b"ESC/VP.net\x10\x03\x00\x00\x20\x01" + header
    assert frames[1] == b"PWR=01\r"


# ── Replies: one per line, values routed by the key they name ───────────────
#
# ESC/VP21 answers every line with one colon-terminated reply: a bare colon
# for a set, KEY=VALUE for a get, ERR for an invalid command (guide 2.1-2.4).
# The driver once matched values to queries by order and queued only
# queries, so an ERR answering a setter popped a query's slot and every later
# value landed on the wrong state.

def _hold() -> None:
    global _HELD
    _HELD = []


def test_an_err_answering_a_setter_does_not_shift_later_values():
    async def go():
        driver, sim = await _make_pair()
        sim.set_state("ASPECT", "40")
        sim.set_state("MUTE", "ON")
        await driver.connect()
        try:
            _hold()
            await driver.send_command("raw_command", {"command": "NOSUCH 1"})
            await driver.poll()
            await driver.transport.flush()
            assert driver.get_state("power_state") == "on"
            assert driver.get_state("error_status") == "00"
            assert driver.get_state("video_mute") is True
            assert driver.get_state("aspect") == "40"
            assert driver._pending == []
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_polls_hold_while_power_on_waits_for_its_colon():
    async def go():
        driver, sim = await _make_pair()
        sim.set_state("PWR", "00")
        await driver.connect()
        try:
            assert driver.get_state("power_state") == "standby"
            _hold()
            await driver.send_command("power_on", {})
            sent = len(driver.transport.lines)
            await driver.poll()
            # The guide asks for PWR ON's colon before the next command.
            assert len(driver.transport.lines) == sent
            await driver.transport.flush()     # the lamp lit: the colon
            for _ in range(5):
                await asyncio.sleep(0)
            assert driver.get_state("power_state") == "on"
            await driver.poll()
            assert len(driver.transport.lines) > sent
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_polls_resume_once_power_on_has_had_its_longest_ignition(monkeypatch):
    # A lamp that fails three times never returns the colon; after the
    # guide's longest ignition the polls go out again, and silence shows.
    monkeypatch.setattr(DRV, "POWER_ON_MAX_S", 0.0)

    async def go():
        driver, sim = await _make_pair()
        sim.set_state("PWR", "00")
        await driver.connect()
        try:
            _hold()
            await driver.send_command("power_on", {})
            sent = len(driver.transport.lines)
            await driver.poll()
            assert len(driver.transport.lines) > sent
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
