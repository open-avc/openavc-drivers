"""Driver + simulator tests for qlab (Figure 53 QLab over OSC).

The real driver is wired to the real simulator over an in-memory link: every
message the driver sends is decoded, handed to ``QLabSimulator.handle_message``
exactly as the simulator's datagram server does, and every reply is encoded
and fed back through the driver's own ``on_data_received``. A minimal OSC 1.0
codec (strings, int32, float32: all QLab uses) stands in for the platform's,
so the community CI needs no ``openavc`` install.

What the tests pin is how the driver treats QLab's answers to logging in and
to its heartbeat, the reason 2.0.0 is Python:

  * any reply to the heartbeat, "denied" and "error" included, is QLab
    answering, so the liveness check fails only on silence;
  * "denied" on a session that was logged in logs in again on the same
    connection and re-arms /alwaysReply and /updates (a QLab restart forgets
    all three), at most once per interval;
  * a refused passcode fails the connect as ``auth_failed`` after one
    /connect, and a workspace that is not open as ``no_response`` naming it.
"""

from __future__ import annotations

import asyncio
import struct
from pathlib import Path

import pytest
from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    StubBaseDriver,
    StubEvents,
    StubState,
    install_stubs,
    load_module,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "video" / "qlab.py"
SIM_PATH = REPO_ROOT / "video" / "qlab_sim.py"


# ── A minimal OSC 1.0 codec ─────────────────────────────────────────────────

def _pad(b: bytes) -> bytes:
    return b + b"\x00" * (-len(b) % 4)


def _osc_string(s: str) -> bytes:
    return _pad(s.encode("utf-8") + b"\x00")


def _encode(address: str, args=None) -> bytes:
    args = list(args or [])
    out = _osc_string(address) + _osc_string("," + "".join(t for t, _ in args))
    for tag, value in args:
        if tag == "i":
            out += struct.pack(">i", int(value))
        elif tag == "f":
            out += struct.pack(">f", float(value))
        elif tag == "s":
            out += _osc_string(str(value))
        else:  # pragma: no cover - QLab uses nothing else
            raise ValueError(tag)
    return out


def _read_string(data: bytes, offset: int) -> tuple[str, int]:
    end = data.index(b"\x00", offset)
    text = data[offset:end].decode("utf-8")
    return text, offset + ((end - offset) // 4 + 1) * 4


def _decode_one(data: bytes) -> tuple[str, list]:
    address, offset = _read_string(data, 0)
    tags, offset = _read_string(data, offset)
    args = []
    for tag in tags[1:]:
        if tag == "i":
            args.append(("i", struct.unpack_from(">i", data, offset)[0]))
            offset += 4
        elif tag == "f":
            args.append(("f", struct.unpack_from(">f", data, offset)[0]))
            offset += 4
        elif tag == "s":
            text, offset = _read_string(data, offset)
            args.append(("s", text))
    return address, args


def _decode(data: bytes) -> list[tuple[str, list]]:
    return [_decode_one(bytes(data))]


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeBaseDriver(LifecycleFake, StubBaseDriver):
    """The platform's hook-driven connect, with the in-memory link as the
    transport; state and the watchdog come from the shared stubs."""

    LAST_ERROR_PROPERTY = "last_error"

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self._health_task = None
        self._health_failures = 0
        self.transport = None
        self._connected = False
        self.transport_factory = None
        self.stashed_fault = None

    async def _pre_connect(self):
        return None

    async def _post_connect(self):
        return None

    async def _initial_sync(self):
        return None

    async def _close_session(self):
        return None

    async def connect(self):
        await self._close_session()
        await self._pre_connect()
        self.transport = self.transport_factory(self)
        try:
            await self._post_connect()
        except Exception:
            transport, self.transport = self.transport, None
            if transport is not None:
                await transport.close()
            await self._close_session()
            self._connected = False
            raise
        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        await self._initial_sync()

    async def disconnect(self):
        self._stop_health_loop()
        if self.transport:
            await self.transport.close()
        self.transport = None
        await self._close_session()
        self._connected = False
        self.set_state("connected", False)

    def _handle_transport_disconnect(self):
        self._connected = False
        if self.transport is not None:
            self.transport.connected = False
        self.set_state("connected", False)


class _FakeOSCSimulator:
    """Stand-in for openavc.simulator.osc_simulator.OSCSimulator: state and
    error injection, as BaseSimulator provides them."""

    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None):
        self.device_id = device_id
        self.config = config or {}
        self._state = dict(self.SIMULATOR_INFO.get("initial_state", {}))
        self._error_modes = dict(self.SIMULATOR_INFO.get("error_modes", {}))
        self._active_errors: set[str] = set()

    @property
    def state(self):
        return dict(self._state)

    def set_state(self, key, value):
        self._state[key] = value

    def inject_error(self, mode):
        if mode in self._error_modes:
            self._active_errors.add(mode)

    def clear_error(self, mode):
        self._active_errors.discard(mode)

    def has_error_behavior(self, behavior):
        return any(self._error_modes.get(m, {}).get("behavior") == behavior
                   for m in self._active_errors)


install_stubs(
    {
        "openavc.transport.osc_codec": {
            "osc_encode_message": _encode,
            "osc_decode_bundle": _decode,
        },
        "openavc.simulator.osc_simulator": {"OSCSimulator": _FakeOSCSimulator},
    },
    base_driver=_FakeBaseDriver,
)
DRV = load_module("qlab_under_test", DRIVER_PATH)
SIMM = load_module("qlab_sim_under_test", SIM_PATH)


class _Link:
    """The OSC transport: the driver's datagrams reach the simulator, and its
    replies come back through the driver's own on_data_received."""

    def __init__(self, driver, sim):
        self.driver = driver
        self.sim = sim
        self.connected = True
        self.sent: list[tuple[str, list]] = []

    async def send(self, data: bytes) -> None:
        if not self.connected:
            raise ConnectionError("link closed")
        for address, args in _decode(data):
            self.sent.append((address, args))
            if self.sim.has_error_behavior("no_response"):
                continue
            for r_address, r_args in self.sim.handle_message(address, args) or []:
                await self.driver.on_data_received(_encode(r_address, r_args))

    async def close(self) -> None:
        self.connected = False

    def addresses(self) -> list[str]:
        return [a for a, _ in self.sent]


async def _pair(driver_config=None, sim_config=None, *, connect=True):
    sim = SIMM.QLabSimulator("sim-qlab", dict(sim_config or {}))
    cfg = {"host": "127.0.0.1", "port": 53000, "workspace_id": "",
           "passcode": "", "transport_mode": "udp", "poll_interval": 0}
    cfg.update(driver_config or {})
    drv = DRV.QLabDriver("qlab1", cfg, StubState(), StubEvents())
    box = {}

    def factory(driver):
        box["link"] = _Link(driver, sim)
        return box["link"]

    drv.transport_factory = factory
    if connect:
        await drv.connect()
    return drv, sim, box


def _run(coro):
    return asyncio.run(coro)


async def _settle(drv):
    """Let a login or refresh the driver scheduled finish."""
    for task in (drv._relogin_task, drv._refresh_task):
        if task is not None:
            await task


# ── Metadata ────────────────────────────────────────────────────────────────

def test_version_and_platform_gate():
    info = DRV.QLabDriver.DRIVER_INFO
    assert info["id"] == "qlab"
    assert info["version"] == "2.0.0"
    assert info["transport"] == "osc"
    # LAST_ERROR_PROPERTY is the newest platform surface the driver uses.
    assert info["min_platform_version"] == "0.29.0"


def test_every_declared_command_is_dispatched_and_nothing_else():
    assert set(DRV.QLabDriver.DRIVER_INFO["commands"]) == set(DRV._COMMANDS)


def test_the_yaml_drivers_config_survives_the_rewrite():
    """Existing projects keep working: same config keys, state and commands."""
    info = DRV.QLabDriver.DRIVER_INFO
    assert set(info["default_config"]) >= {
        "host", "port", "workspace_id", "passcode", "transport_mode",
        "poll_interval", "listen_port", "verify_timeout"}
    assert info["default_config"]["listen_port"] == 53001
    assert info["default_config"]["verify_timeout"] == 0
    assert set(info["state_variables"]) >= {
        "qlab_version", "connected_ok", "current_cue_id", "current_cue_number",
        "current_cue_name", "is_running"}
    assert info["quick_actions"] == ["go", "stop", "panic"]


# ── Logging in ──────────────────────────────────────────────────────────────

def test_connect_logs_in_arms_the_session_and_reads_state():
    async def go():
        drv, sim, box = await _pair()
        link = box["link"]
        # No passcode configured: /connect goes without the argument.
        assert link.sent[0] == ("/connect", [])
        assert ("/alwaysReply", [("i", 1)]) in link.sent
        assert ("/updates", [("i", 1)]) in link.sent
        assert drv.get_state("connected_ok") == "ok:view|edit|control"
        assert drv.get_state("qlab_version") == "5.4.5"
        assert drv.get_state("current_cue_name") == "Preshow Music"
        assert drv.get_state("current_cue_number") == "1"
        assert drv.get_state("is_running") is False
        await drv.disconnect()
    _run(go())


def test_the_passcode_and_the_workspace_are_sent():
    async def go():
        drv, sim, box = await _pair(
            {"passcode": "5775", "workspace_id": "SIMWS"}, {"passcode": "5775"})
        link = box["link"]
        assert link.sent[0] == ("/workspace/SIMWS/connect", [("s", "5775")])
        # The update subscription is application-wide, so rootless.
        assert ("/updates", [("i", 1)]) in link.sent
        assert "/workspace/SIMWS/updates" not in link.addresses()
        await drv.disconnect()
    _run(go())


def test_a_refused_passcode_is_auth_failed_after_one_login():
    async def go():
        drv, sim, box = await _pair(
            {"passcode": "1234"}, {"passcode": "5775"}, connect=False)
        with pytest.raises(DRV.ConnectionFaultError) as info:
            await drv.connect()
        assert info.value.fault_code == "auth_failed"
        assert "refused the OSC passcode" in str(info.value)
        assert drv.get_state("connected_ok") == "badpass"
        assert box["link"].addresses().count("/connect") == 1
    _run(go())


def test_a_locked_workspace_with_no_passcode_says_it_needs_one():
    async def go():
        drv, sim, box = await _pair({}, {"passcode": "5775"}, connect=False)
        with pytest.raises(DRV.ConnectionFaultError) as info:
            await drv.connect()
        assert info.value.fault_code == "auth_failed"
        assert "needs an OSC passcode" in str(info.value)
    _run(go())


def test_a_workspace_that_is_not_open_is_named_and_retried():
    """Not a permanent fault: a show the operator closed comes back."""
    async def go():
        drv, sim, box = await _pair({"workspace_id": "NOPE"}, connect=False)
        with pytest.raises(DRV.ConnectionFaultError) as info:
            await drv.connect()
        assert info.value.fault_code == "no_response"
        assert "NOPE" in str(info.value)
    _run(go())


def test_qlab_that_does_not_answer_the_login_is_no_response(monkeypatch):
    monkeypatch.setattr(DRV, "LOGIN_TIMEOUT_S", 0.05)

    async def go():
        drv, sim, box = await _pair(connect=False)
        sim.inject_error("communication_timeout")
        with pytest.raises(DRV.ConnectionFaultError) as info:
            await drv.connect()
        assert info.value.fault_code == "no_response"
        assert "did not answer" in str(info.value)
    _run(go())


# ── The liveness check (backlog-era bug: a denial dropped the link) ─────────

def test_the_heartbeat_answered_is_alive():
    async def go():
        drv, sim, box = await _pair({"workspace_id": "SIMWS"})
        await drv._liveness_probe()
        assert box["link"].sent[-1] == ("/workspace/SIMWS/thump", [])
        await drv.disconnect()
    _run(go())


def test_silence_is_a_miss():
    async def go():
        drv, sim, box = await _pair()
        sim.inject_error("communication_timeout")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(drv._liveness_probe(), 0.1)
        await drv.disconnect()
    _run(go())


def test_after_a_restart_a_denial_is_an_answer_and_logs_in_again():
    """The heartbeat answered "denied" is QLab answering: no miss, no drop.

    The YAML driver counted it as a miss so the reconnect would re-send the
    passcode; the driver now logs in again on the connection it has, and
    re-arms what QLab forgot. At once, even seconds after connecting.
    """
    async def go():
        drv, sim, box = await _pair(
            {"passcode": "5775", "workspace_id": "SIMWS"}, {"passcode": "5775"})
        link = box["link"]
        sim.inject_error("qlab_restarted")
        before = len(link.sent)
        await drv._liveness_probe()          # must NOT raise
        await _settle(drv)
        again = link.sent[before:]
        assert ("/workspace/SIMWS/connect", [("s", "5775")]) in again
        assert ("/alwaysReply", [("i", 1)]) in again
        assert ("/updates", [("i", 1)]) in again
        assert drv.stashed_fault is None     # never dropped
        # Service is back: the heartbeat answers ok again.
        await drv._liveness_probe()
        assert drv.get_state("connected_ok") == "ok:view|edit|control"
        await drv.disconnect()
    _run(go())


def test_a_passcode_changed_in_qlab_is_reported_once_not_retried():
    async def go():
        drv, sim, box = await _pair({"passcode": "5775"}, {"passcode": "5775"})
        sim._passcode = "9999"               # changed in QLab, then restarted
        sim.inject_error("qlab_restarted")
        # Rootless: the heartbeat is QLab's own, so the poll's workspace
        # queries are what come back denied.
        await drv.poll()
        await _settle(drv)
        assert drv.stashed_fault[0] == "auth_failed"
        assert box["link"].addresses().count("/connect") == 2
    _run(go())


def test_a_denial_right_after_logging_in_again_is_a_permission_not_a_restart():
    """QLab accepted the login and still refuses: another login would not
    change what the passcode is allowed, so the card says so instead."""
    denied = [("s", '{"status":"denied","address":"/go"}')]

    async def go():
        drv, sim, box = await _pair()
        drv._handle_reply("/reply/go", denied)
        assert "no longer had this controller logged in" in drv.get_state("last_error")
        await _settle(drv)
        logins = box["link"].addresses().count("/connect")
        drv._handle_reply("/reply/go", denied)
        assert drv._relogin_task.done()          # no second login started
        assert box["link"].addresses().count("/connect") == logins
        assert "not allowed" in drv.get_state("last_error")
        await drv.disconnect()
    _run(go())


def test_the_configured_workspace_closing_takes_the_device_offline_with_the_reason():
    async def go():
        drv, sim, box = await _pair({"workspace_id": "SIMWS"})
        sim.inject_error("workspace_closed")
        await drv._liveness_probe()          # the error reply is an answer
        code, message = drv.stashed_fault
        assert code == "no_response"
        assert "SIMWS" in message and "Open the show" in message
    _run(go())


def test_a_login_without_control_access_is_said_on_the_card():
    async def go():
        drv, sim, box = await _pair()
        drv._note_permissions("ok:view")
        assert "view only" in drv.get_state("last_error")
        await drv.disconnect()
    _run(go())


# ── Commands and feedback ───────────────────────────────────────────────────

def test_commands_build_qlabs_addresses_and_typed_arguments():
    async def go():
        drv, sim, box = await _pair({"workspace_id": "SIMWS"})
        link = box["link"]
        await drv.send_command("start_cue", {"number": "4"})
        assert link.sent[-1] == ("/workspace/SIMWS/cue/4/start", [])
        await drv.send_command("start_cue_id", {"cue_id": "cue-3"})
        assert link.sent[-1] == ("/workspace/SIMWS/cue_id/cue-3/start", [])
        await drv.send_command("load_cue_at", {"number": "2", "seconds": 2.5})
        assert link.sent[-1] == ("/workspace/SIMWS/cue/2/loadAt", [("f", 2.5)])
        await drv.send_command("set_cue_level", {"number": "2", "level": -6})
        assert link.sent[-1] == ("/workspace/SIMWS/cue/2/sliderLevel/0", [("f", -6.0)])
        await drv.send_command("set_cue_armed", {"number": "2", "value": 1})
        assert link.sent[-1] == ("/workspace/SIMWS/cue/2/armed", [("i", 1)])
        await drv.send_command("set_cue_color", {"number": "2", "color": "red"})
        assert link.sent[-1] == ("/workspace/SIMWS/cue/2/colorName", [("s", "red")])
        with pytest.raises(ValueError):
            await drv.send_command("start_cue", {})
        with pytest.raises(ValueError):
            await drv.send_command("no_such_command")
        await drv.disconnect()
    _run(go())


def test_the_playback_push_refreshes_the_playhead():
    async def go():
        drv, sim, box = await _pair()
        refresh = drv._schedule_refresh
        drv._schedule_refresh = lambda: None   # the push alone, no re-query
        await drv.send_command("go")
        assert drv.get_state("current_cue_id") == "cue-2"
        drv._schedule_refresh = refresh
        await drv.send_command("playhead_next")
        await drv.send_command("playhead_previous")
        await _settle(drv)
        assert drv.get_state("current_cue_number") == "2"
        assert drv.get_state("current_cue_name") == "Houselights to Half"
        await drv.send_command("stop")
        await drv.poll()
        assert drv.get_state("is_running") is False
        await drv.disconnect()
    _run(go())


def test_a_command_qlab_cannot_carry_out_lands_in_last_error():
    async def go():
        drv, sim, box = await _pair()
        await drv.send_command("start_cue", {"number": "99"})
        assert "/cue/99/start" in drv.get_state("last_error")
        await drv.disconnect()
    _run(go())
