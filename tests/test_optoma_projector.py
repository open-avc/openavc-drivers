"""Driver + simulator tests for optoma_projector (Optoma RS232 / LAN
"~XX" protocol).

No Optoma hardware on hand, so correctness is proven two ways: metadata
/ shape assertions on the driver, and **dual-proof round trips** that
wire the real driver to the real simulator over an in-memory transport
— the simulator renders the ~XX grammar from Optoma's RS232 Protocol
Function List, the driver parses it, and results are asserted on both
sides.

Covers the protocol essentials:
  - command-line formatting (two-digit projector ID field, CR
    terminator) against the protocol document's worked example;
  - the serialized request/response design that the echo-less bare
    P / F / Ok replies force, including reply timeouts and the older
    firmware's OK casing;
  - unsolicited INFO push handling: power transitions (compressed
    warm-up burst), fault codes (known + unknown), interleaving with
    an in-flight request, and the ready notice clearing the fault;
  - Telnet IAC negotiation / NUL padding / command-echo stripping;
  - the asymmetric input code spaces (write 15 = HDMI2 reads back 8);
  - every polled read landing in the right state variable, the
    standby-restricted poll, and light-hours summing across the
    single-total and normal/eco reply shapes;
  - per-model feature flags answering F (no lens, no audio, volume
    range) without contaminating state;
  - the projector-ID gate (mismatched ID gets silence, not F);
  - raw_command read routing and set-only commands fabricating no
    state;
  - device-setting writes with immediate read-back;
  - the discovery probe's expect pattern matching the simulator's
    reply to the probe's own send bytes.

Loads the driver + simulator with the ``server.*`` / ``simulator.*``
imports stubbed so the community CI stays self-contained (conftest.py
rolls the stubs back after this module is collected).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "projectors" / "optoma_projector.py"
SIM_PATH = REPO_ROOT / "projectors" / "optoma_projector_sim.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeState:
    def __init__(self) -> None:
        self.data: dict = {}

    def set(self, key, value, **_):
        self.data[key] = value


class _FakeEvents:
    async def emit(self, name, *args, **kwargs):
        pass


class _FakeBaseDriver:
    """Stand-in for the platform BaseDriver surface this driver uses."""

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False

    def set_state(self, key, value) -> None:
        self.state.set(f"device.{self.device_id}.{key}", value)

    def get_state(self, key, default=None):
        return self.state.data.get(f"device.{self.device_id}.{key}", default)


_CURRENT_SIM: object | None = None


class _FakeTransport:
    """In-memory pipe: driver bytes -> sim -> reply frames -> driver,
    split on CR exactly like the platform's delimiter framing."""

    def __init__(self, on_data) -> None:
        self.on_data = on_data
        self.connected = True
        self.sent: list[bytes] = []
        self.silent = False

    async def send(self, data) -> None:
        if not self.connected:
            raise ConnectionError("transport closed")
        self.sent.append(bytes(data))
        if self.silent:
            return
        sim = _CURRENT_SIM
        reply = sim.handle_command(bytes(data)) if sim else None
        if reply:
            for frame in bytes(reply).split(b"\r"):
                if frame:
                    await self.on_data(frame)

    async def close(self) -> None:
        self.connected = False


class _FakeTCPSimulator:
    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self._state = dict(self.SIMULATOR_INFO.get("initial_state", {}))

    @property
    def state(self) -> dict:
        # Mirrors BaseSimulator: a READ-ONLY COPY.
        return dict(self._state)

    def set_state(self, key, value) -> None:
        self._state[key] = value

    def get_state(self, key, default=None):
        return self._state.get(key, default)


def _load(name: str, path: Path) -> ModuleType:
    server = ModuleType("server")
    server.__path__ = []  # type: ignore[attr-defined]
    sys.modules["server"] = server
    for sub in ("drivers", "utils"):
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


DRV = _load("optoma_projector_under_test", DRIVER_PATH)
SIM = _load("optoma_projector_sim_under_test", SIM_PATH)


# ── Pairing harness ─────────────────────────────────────────────────────────

def _make_pair(sim_config=None, driver_overrides=None):
    global _CURRENT_SIM
    sim = SIM.OptomaProjectorSimulator("sim1", sim_config or {})
    _CURRENT_SIM = sim

    cfg = {
        "host": "10.0.0.71",
        "port": 23,
        "projector_id": 0,
        "poll_interval": 0,
        "inter_command_delay": 0.0,
    }
    cfg.update(driver_overrides or {})
    driver = DRV.OptomaProjectorDriver("proj1", cfg, _FakeState(), _FakeEvents())
    driver.transport = _FakeTransport(driver.on_data_received)
    return driver, sim


def _run(coro):
    loop = asyncio.get_event_loop_policy().new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def pair():
    return _make_pair()


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_metadata_shape():
    info = DRV.OptomaProjectorDriver.DRIVER_INFO
    assert info["version"] == "1.0.0"
    assert info["min_platform_version"] == "0.23.0"
    assert info["ports"] == [23]
    assert info["transports"] == ["tcp", "serial"]
    assert info["source_url"].startswith("https://")
    for key, setting in info["device_settings"].items():
        assert setting["state_key"] in info["state_variables"], key
    for cid in info["quick_actions"]:
        assert cid in info["commands"], cid


def test_every_read_lands_in_a_declared_state_variable():
    info = DRV.OptomaProjectorDriver.DRIVER_INFO
    declared = set(info["state_variables"])
    for state_key, _kind in DRV._READS.values():
        assert state_key in declared, state_key
    assert "fault" in declared
    assert "power_state" in declared


def test_command_line_formatting(pair):
    driver, _sim = pair
    _run(driver.send_command("power_on"))
    # Protocol document worked example shape: ~XXnnn v<CR>.
    assert driver.transport.sent[0] == b"~0000 1\r"

    driver5, _ = _make_pair(driver_overrides={"projector_id": 5})
    _run(driver5.send_command("power_off"))
    assert driver5.transport.sent[0] == b"~0500 0\r"


# ── Power / INFO push ───────────────────────────────────────────────────────

def test_power_on_burst_reaches_on(pair):
    driver, sim = pair
    sim.set_state("power", 0)
    ok = _run(driver.send_command("power_on"))
    assert ok is True
    assert sim.state["power"] == 1
    # Compressed warm-up burst: INFO1 then INFO24.
    assert driver.get_state("power_state") == "on"
    assert driver.get_state("fault") == "none"


def test_power_off_burst_reaches_standby(pair):
    driver, sim = pair
    ok = _run(driver.send_command("power_off"))
    assert ok is True
    assert sim.state["power"] == 0
    assert driver.get_state("power_state") == "standby"


def test_info_fault_codes():
    driver, _sim = _make_pair()

    async def feed():
        await driver.on_data_received(b"INFO1")
        assert driver.get_state("power_state") == "warming_up"
        await driver.on_data_received(b"INFO2")
        assert driver.get_state("power_state") == "cooling_down"
        await driver.on_data_received(b"INFO7")
        assert driver.get_state("fault") == "over_temperature"
        await driver.on_data_received(b"INFO4")
        assert driver.get_state("fault") == "light_source_fail"
        # Unknown code surfaces without crashing.
        await driver.on_data_received(b"INFO99")
        assert driver.get_state("fault") == "fault_99"
        # Ready clears the fault.
        await driver.on_data_received(b"INFO24")
        assert driver.get_state("power_state") == "on"
        assert driver.get_state("fault") == "none"

    _run(feed())


def test_info_mid_request_does_not_steal_reply(pair):
    driver, _sim = pair
    driver.transport.silent = True

    async def scenario():
        task = asyncio.ensure_future(driver._request("124", "1", timeout=1.0))
        await asyncio.sleep(0.01)
        # A fault push lands while the read is in flight...
        await driver.on_data_received(b"INFO6")
        # ...then the actual reply.
        await driver.on_data_received(b"Ok1")
        return await task

    reply = _run(scenario())
    assert reply == "Ok1"
    assert driver.get_state("fault") == "fan_lock"


# ── Receive-path hygiene ────────────────────────────────────────────────────

def test_iac_nul_and_echo_stripping(pair):
    driver, _sim = pair

    async def feed():
        # Telnet negotiation + NUL padding only — no reply content.
        await driver.on_data_received(b"\xff\xfd\x01\xff\xfb\x03\x00")
        assert not driver._reply_queue
        # A command echo is not a reply.
        await driver.on_data_received(b"~00124 1")
        assert not driver._reply_queue
        # IAC bytes glued onto a real reply are stripped.
        await driver.on_data_received(b"\xff\xfd\x18Ok1")
        assert list(driver._reply_queue) == ["Ok1"]

    _run(feed())


def test_older_firmware_ok_casing():
    driver, _sim = _make_pair(sim_config={"ok_casing": "OK"})
    _run(driver.poll())
    assert driver.get_state("power_state") == "on"
    assert driver.get_state("brightness") == 50


# ── Input code asymmetry ────────────────────────────────────────────────────

def test_input_write_read_code_asymmetry(pair):
    driver, sim = pair
    ok = _run(driver.send_command("set_input", {"input": "hdmi2"}))
    assert ok is True
    # Write code 15 stores read code 8.
    assert driver.transport.sent[0] == b"~0012 15\r"
    assert sim.state["input_code"] == 8
    _run(driver.poll())
    assert driver.get_state("input") == "hdmi2"


def test_unknown_input_read_code(pair):
    driver, sim = pair
    sim.set_state("input_code", 19)  # not in the read map
    _run(driver.poll())
    assert driver.get_state("input") == "input_19"


# ── Polling ─────────────────────────────────────────────────────────────────

def test_full_poll_populates_state(pair):
    driver, _sim = pair
    _run(driver.poll())
    assert driver.get_state("power_state") == "on"
    assert driver.get_state("input") == "hdmi1"
    assert driver.get_state("av_mute") is False
    assert driver.get_state("audio_mute") is False
    assert driver.get_state("display_mode") == "presentation"
    assert driver.get_state("aspect_ratio") == "auto"
    assert driver.get_state("brightness") == 50
    assert driver.get_state("contrast") == 50
    # Normal/eco counters are summed.
    assert driver.get_state("light_source_hours") == 1234 + 210
    assert driver.get_state("source_resolution") == "1920x1200"


def test_standby_poll_skips_on_queries(pair):
    driver, sim = pair
    sim.set_state("power", 0)
    _run(driver.poll())
    # Only the always-reads went out: power + hours.
    assert len(driver.transport.sent) == 2
    assert driver.get_state("power_state") == "standby"
    assert driver.get_state("input") is None


def test_single_total_hours_shape():
    driver, sim = _make_pair(sim_config={"dual_hours": False})
    sim.set_state("light_hours_normal", 4321)
    sim.set_state("light_hours_eco", 0)
    _run(driver.poll())
    assert driver.get_state("light_source_hours") == 4321


def test_identity_reads():
    driver, _sim = _make_pair()
    _run(driver._post_connect())
    assert driver.get_state("model_name") == "Optoma WUXGA"
    assert driver.get_state("firmware_version") == "C01.23"
    assert driver.get_state("serial_number") == "Q8EJ8850001"


def test_model_name_string_passthrough():
    driver, sim = _make_pair()
    sim.set_state("model_class", "ZU725TST")
    _run(driver._post_connect())
    assert driver.get_state("model_name") == "ZU725TST"


# ── Writes / per-model rejects ──────────────────────────────────────────────

def test_dicom_mode_write_reads_back(pair):
    driver, sim = pair
    ok = _run(driver.send_command("set_display_mode", {"mode": "dicom_sim"}))
    assert ok is True
    assert driver.transport.sent[0] == b"~0020 13\r"
    assert sim.state["display_mode_code"] == 10
    _run(driver.poll())
    assert driver.get_state("display_mode") == "dicom_sim"


def test_volume_range_reject():
    driver, sim = _make_pair(sim_config={"volume_max": 10})
    assert _run(driver.send_command("set_volume", {"level": 12})) is False
    assert sim.state["volume"] == 5
    assert _run(driver.send_command("set_volume", {"level": 7})) is True
    assert sim.state["volume"] == 7


def test_missing_lens_answers_f():
    driver, _sim = _make_pair(sim_config={"has_lens": False})
    assert _run(driver.send_command("lens_shift", {"direction": "up"})) is False
    assert _run(driver.send_command("lens_zoom", {"direction": "in"})) is False


def test_missing_audio_answers_f():
    driver, _sim = _make_pair(sim_config={"has_audio": False})
    assert _run(driver.send_command("audio_mute_on")) is False
    _run(driver.poll())
    # The audio-mute read answered F: state never fabricated.
    assert driver.get_state("audio_mute") is None


def test_shutter(pair):
    driver, sim = pair
    assert _run(driver.send_command("shutter_close")) is True
    assert sim.state["shutter_closed"] == 1
    no_shutter_driver, _ = _make_pair(sim_config={"has_shutter": False})
    assert _run(no_shutter_driver.send_command("shutter_close")) is False


def test_lens_memory_and_keystone(pair):
    driver, sim = pair
    assert _run(driver.send_command("lens_memory_save", {"slot": 3})) is True
    assert sim.state["lens_memory_saved"] == 3
    assert _run(driver.send_command("lens_memory_apply", {"slot": 3})) is True
    assert _run(driver.send_command("set_v_keystone", {"value": -12})) is True
    assert sim.state["v_keystone"] == -12
    assert driver.transport.sent[-1] == b"~0066 -12\r"


def test_set_only_commands_fabricate_no_state(pair):
    driver, sim = pair
    assert _run(driver.send_command("set_volume", {"level": 4})) is True
    assert _run(driver.send_command("freeze_on")) is True
    assert sim.state["freeze"] == 1
    assert driver.get_state("volume") is None
    assert driver.get_state("freeze") is None


# ── Projector-ID gate ───────────────────────────────────────────────────────

def test_projector_id_match_and_silence():
    driver, sim = _make_pair(
        sim_config={"projector_id": 3}, driver_overrides={"projector_id": 3}
    )
    _run(driver.poll())
    assert driver.get_state("power_state") == "on"
    assert driver.transport.sent[0] == b"~03124 1\r"

    wrong, _sim2 = _make_pair(
        sim_config={"projector_id": 3}, driver_overrides={"projector_id": 4}
    )
    reply = _run(wrong._request("124", "1", timeout=0.05))
    assert reply is None
    assert wrong.get_state("power_state") is None


# ── Timeouts / transport faults ─────────────────────────────────────────────

def test_reply_timeout_returns_none(pair):
    driver, _sim = pair
    driver.transport.silent = True
    reply = _run(driver._request("124", "1", timeout=0.05))
    assert reply is None
    assert driver.get_state("power_state") is None


def test_dead_transport_raises(pair):
    driver, _sim = pair
    driver.transport.connected = False
    with pytest.raises(ConnectionError):
        _run(driver.send_command("power_on"))


# ── raw_command ─────────────────────────────────────────────────────────────

def test_raw_command_read_routing(pair):
    driver, sim = pair
    sim.set_state("brightness", 77)
    reply = _run(driver.send_command("raw_command", {"command": "~00125 1"}))
    assert reply == "Ok77"
    assert driver.get_state("brightness") == 77


def test_raw_command_write_no_state(pair):
    driver, sim = pair
    reply = _run(driver.send_command("raw_command", {"command": "~0021 70"}))
    assert reply == "P"
    assert sim.state["brightness"] == 70
    assert driver.get_state("brightness") is None


def test_raw_command_requires_tilde(pair):
    driver, _sim = pair
    with pytest.raises(ValueError):
        _run(driver.send_command("raw_command", {"command": "00125 1"}))


# ── Device settings ─────────────────────────────────────────────────────────

def test_device_setting_roundtrip(pair):
    driver, sim = pair
    _run(driver.set_device_setting("brightness", 33))
    assert sim.state["brightness"] == 33
    assert driver.get_state("brightness") == 33
    _run(driver.set_device_setting("display_mode", "movie"))
    assert sim.state["display_mode_code"] == 3
    assert driver.get_state("display_mode") == "movie"
    with pytest.raises(ValueError):
        _run(driver.set_device_setting("nonexistent", 1))


# ── Discovery probe coherence ───────────────────────────────────────────────

def test_probe_expect_matches_sim_reply(pair):
    _driver, sim = pair
    probe = DRV.OptomaProjectorDriver.DRIVER_INFO["discovery"]["tcp_probe"]
    assert probe["port"] == 23
    reply = sim.handle_command(probe["send_ascii"].encode("ascii"))
    assert re.search(probe["expect_regex"], reply.decode("ascii"))
    # Older firmware casing matches too.
    sim_old = SIM.OptomaProjectorSimulator("sim2", {"ok_casing": "OK"})
    reply_old = sim_old.handle_command(probe["send_ascii"].encode("ascii"))
    assert re.search(probe["expect_regex"], reply_old.decode("ascii"))
