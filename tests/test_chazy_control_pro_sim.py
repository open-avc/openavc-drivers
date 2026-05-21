"""Unit tests for the chazy_control_pro simulator.

Loads ``switchers/chazy_control_pro_sim.py`` directly, stubbing the
``simulator.*`` base it imports (so the community repo's test suite stays
self-contained — mirrors how test_chazy_control_pro.py stubs ``server.*``).

The simulator's banners are asserted byte-for-byte against the same
real-hardware captures the driver parses (fixtures/chazy_control_pro_banners.py),
in both the offline (just-added) and online (linked) states. A final block
feeds the simulator's output back through the *driver's* parsers to prove the
two are exact duals.
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
SIM_PATH = REPO_ROOT / "switchers" / "chazy_control_pro_sim.py"
DRIVER_PATH = REPO_ROOT / "switchers" / "chazy_control_pro.py"
FIXTURES_PATH = REPO_ROOT / "tests" / "fixtures" / "chazy_control_pro_banners.py"
CHILD_FIXTURES_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "chazy_control_pro_child_banners.py"
)


def _install_simulator_stub() -> None:
    """Minimal stand-in for simulator.tcp_simulator.TCPSimulator that mirrors
    the parts of BaseSimulator the simulator relies on (state + error modes).
    """
    if "simulator.tcp_simulator" in sys.modules:
        return
    pkg = ModuleType("simulator")
    pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("simulator", pkg)
    mod = ModuleType("simulator.tcp_simulator")

    class _TCPSimulator:
        SIMULATOR_INFO: dict = {}

        def __init__(self, device_id, config=None):
            self.device_id = device_id
            self.config = config or {}
            self._state = dict(self.SIMULATOR_INFO.get("initial_state", {}))
            self._error_modes = dict(self.SIMULATOR_INFO.get("error_modes", {}))
            self._active_errors: set[str] = set()
            self._delays = dict(self.SIMULATOR_INFO.get("delays", {}))

        @property
        def state(self):
            return dict(self._state)

        def set_state(self, key, value):
            self._state[key] = value

        def get_state(self, key, default=None):
            return self._state.get(key, default)

        @property
        def active_errors(self):
            return set(self._active_errors)

        def inject_error(self, mode):
            self._active_errors.add(mode)

        def clear_error(self, mode):
            self._active_errors.discard(mode)

        def has_error_behavior(self, behavior):
            return any(
                self._error_modes.get(m, {}).get("behavior") == behavior
                for m in self._active_errors
            )

    mod.TCPSimulator = _TCPSimulator
    sys.modules["simulator.tcp_simulator"] = mod


def _install_server_stubs() -> None:
    if "server.drivers.base" in sys.modules:
        return
    server = ModuleType("server")
    server.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("server", server)
    drivers = ModuleType("server.drivers")
    drivers.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("server.drivers", drivers)
    base = ModuleType("server.drivers.base")

    class _BaseDriver:
        DRIVER_INFO: dict = {}

    base.BaseDriver = _BaseDriver
    sys.modules["server.drivers.base"] = base

    transport = ModuleType("server.transport")
    transport.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("server.transport", transport)
    tcp = ModuleType("server.transport.tcp")

    class _TCPTransport:
        pass

    tcp.TCPTransport = _TCPTransport
    sys.modules["server.transport.tcp"] = tcp

    utils = ModuleType("server.utils")
    utils.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("server.utils", utils)
    logger = ModuleType("server.utils.logger")
    logger.get_logger = lambda name="chazy": logging.getLogger(name)
    sys.modules["server.utils.logger"] = logger


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_simulator_stub()
_install_server_stubs()
simmod = _load(SIM_PATH, "chazy_control_pro_sim_under_test")
drv = _load(DRIVER_PATH, "chazy_control_pro_driver_for_sim_test")
fx = _load(FIXTURES_PATH, "chazy_fixtures_for_sim_test")
fxc = _load(CHILD_FIXTURES_PATH, "chazy_child_fixtures_for_sim_test")

Sim = simmod.ChazyControlProSimulator


@pytest.fixture
def sim():
    return Sim("sim1")


def _strip(framed: bytes, sent: str) -> str:
    """Reproduce the driver's view: take the unit before the prompt, strip the
    echoed command line (via the driver's own _strip_echo)."""
    prompt = simmod.PROMPT
    idx = framed.find(prompt)
    assert idx != -1, "response not terminated by CONTROLLER> prompt"
    unit = framed[:idx].decode("latin-1")
    return drv.ChazyControlProDriver._strip_echo(unit, sent)


# ── SIMULATOR_INFO validity ──

def test_driver_id_and_transport_match():
    info = Sim.SIMULATOR_INFO
    assert info["driver_id"] == drv.ChazyControlProDriver.DRIVER_INFO["id"]
    assert info["transport"] == drv.ChazyControlProDriver.DRIVER_INFO["transport"]


def test_all_controller_state_vars_in_initial_state():
    initial = Sim.SIMULATOR_INFO["initial_state"]
    declared = drv.ChazyControlProDriver.DRIVER_INFO["state_variables"]
    for name, vardef in declared.items():
        assert name in initial, f"{name} missing from simulator initial_state"
        if vardef["type"] == "boolean":
            assert isinstance(initial[name], bool), f"{name} should be bool"
        if vardef["type"] == "integer":
            assert isinstance(initial[name], int), f"{name} should be int"


# ── Connect framing ──

def test_greeting_has_iac_and_prompt(sim):
    greeting = asyncio.run(sim.on_client_connected("c1"))
    assert greeting.startswith(b"\xff"), "greeting must open with Telnet IAC"
    assert greeting.endswith(simmod.PROMPT)
    # After the IAC negotiation, the body matches the captured greeting.
    assert greeting[len(simmod._IAC_GREETING):].decode("latin-1") == fx.RAW_GREETING


def test_driver_filter_telnet_strips_greeting_to_capture(sim):
    greeting = asyncio.run(sim.on_client_connected("c1"))
    d = drv.ChazyControlProDriver.__new__(drv.ChazyControlProDriver)
    d._iac_state = "normal"
    assert d._filter_telnet(greeting).decode("latin-1") == fx.RAW_GREETING


def test_command_is_echoed_and_prompt_terminated(sim):
    framed = sim.handle_command(b"GET STATUS")
    assert framed.startswith(b"GET STATUS\r\n")
    assert framed.endswith(simmod.PROMPT)
    assert b"\r\n" in framed  # CRLF line endings on the wire


# ── Byte-exact banners: online (default seed) ──

def test_status_online_byte_exact(sim):
    assert _strip(sim.handle_command(b"GET STATUS"), "GET STATUS") == \
        fx.BANNER_STATUS_ONLINE


def test_enc_detail_online_byte_exact(sim):
    assert _strip(sim.handle_command(b"GET ENC 1 STATUS"), "GET ENC 1 STATUS") == \
        fx.BANNER_ENC_DETAIL_ONLINE


def test_dec_detail_online_byte_exact(sim):
    assert _strip(sim.handle_command(b"GET DEC 1 STATUS"), "GET DEC 1 STATUS") == \
        fx.BANNER_DEC_DETAIL_ONLINE


def test_gpio_byte_exact(sim):
    assert _strip(sim.handle_command(b"GET GPIO 0 STATUS"), "GET GPIO 0 STATUS") == \
        fx.BANNER_GPIO


# ── Byte-exact banners: offline (endpoints_offline) ──

def test_status_offline_byte_exact(sim):
    sim.inject_error("endpoints_offline")
    assert _strip(sim.handle_command(b"GET STATUS"), "GET STATUS") == \
        fx.BANNER_STATUS_POPULATED


def test_enc_detail_offline_byte_exact(sim):
    sim.inject_error("endpoints_offline")
    assert _strip(sim.handle_command(b"GET ENC 1 STATUS"), "GET ENC 1 STATUS") == \
        fx.BANNER_ENC_DETAIL


def test_dec_detail_offline_byte_exact(sim):
    sim.inject_error("endpoints_offline")
    assert _strip(sim.handle_command(b"GET DEC 1 STATUS"), "GET DEC 1 STATUS") == \
        fx.BANNER_DEC_DETAIL


# ── Byte-exact banners: empty controller + search lifecycle ──

def test_status_empty_after_delete(sim):
    sim.handle_command(b"SET ENC 1 DELETE")
    sim.handle_command(b"SET DEC 1 DELETE")
    assert _strip(sim.handle_command(b"GET STATUS"), "GET STATUS") == \
        fx.BANNER_STATUS_EMPTY


def test_search_empty_when_all_assigned(sim):
    assert _strip(sim.handle_command(b"SEARCH"), "SEARCH") == fx.BANNER_SEARCH_EMPTY
    assert _strip(sim.handle_command(b"GET SEARCH STATUS"), "GET SEARCH STATUS") == \
        fx.BANNER_SEARCH_EMPTY


def test_search_found_when_unassigned(sim):
    sim.handle_command(b"SET ENC 1 DELETE")
    sim.handle_command(b"SET DEC 1 DELETE")
    assert _strip(sim.handle_command(b"SEARCH"), "SEARCH") == fx.BANNER_SEARCH_FOUND


def test_add_auto_all_emits_captured_lines(sim):
    sim.handle_command(b"SET ENC 1 DELETE")
    sim.handle_command(b"SET DEC 1 DELETE")
    out = _strip(sim.handle_command(b"ADD AUTO ALL"), "ADD AUTO ALL")
    assert fx.LINE_ADD_AUTO_ENC in out
    assert fx.LINE_ADD_AUTO_DEC in out
    # The re-added endpoints come back offline (Net Off), as captured.
    sim.inject_error("endpoints_offline")
    assert _strip(sim.handle_command(b"GET STATUS"), "GET STATUS") == \
        fx.BANNER_STATUS_POPULATED


# ── Errors + confirm flow ──

def test_enc_not_found_byte_exact(sim):
    assert _strip(sim.handle_command(b"GET ENC 2 STATUS"), "GET ENC 2 STATUS") == \
        fx.BANNER_ENC_NOT_FOUND


def test_dec_not_found_byte_exact(sim):
    assert _strip(sim.handle_command(b"GET DEC 9 STATUS"), "GET DEC 9 STATUS") == \
        "[ERROR]Decoder 009 does not exist."


def test_unknown_command(sim):
    assert _strip(sim.handle_command(b"FLARGLE"), "FLARGLE") == fx.LINE_ERR_CMD


def test_set_on_missing_endpoint_errors(sim):
    out = _strip(sim.handle_command(b"SET ENC 5 NAME Foo"), "SET ENC 5 NAME Foo")
    assert out == "[ERROR]Encoder 005 does not exist."


def test_reset_confirm_yes(sim):
    q = _strip(sim.handle_command(b"SET RESET"), "SET RESET")
    assert q == fx.LINE_RESET_QUESTION
    assert "[SUCCESS]" in _strip(sim.handle_command(b"Yes"), "Yes")


def test_reset_confirm_no_cancels(sim):
    _strip(sim.handle_command(b"SET RESET"), "SET RESET")
    assert _strip(sim.handle_command(b"No"), "No") == fx.LINE_RESET_CANCELLED


def test_get_date_and_ntp(sim):
    assert _strip(sim.handle_command(b"GET DATE"), "GET DATE") == fx.LINE_GET_DATE
    assert _strip(sim.handle_command(b"GET NTP"), "GET NTP") == fx.LINE_GET_NTP


# ── Stateful mutations reflect in subsequent reads ──

def test_set_name_reflects_in_detail(sim):
    sim.handle_command(b"SET ENC 1 NAME Lectern PC")
    banner = _strip(sim.handle_command(b"GET ENC 1 STATUS"), "GET ENC 1 STATUS")
    assert "Lectern PC" in banner
    parsed = drv._parse_encoder_detail(banner)
    assert parsed["name"] == "Lectern PC"


def test_dec_switch_reflects_in_status_and_detail(sim):
    # Add a second encoder to route to, then switch decoder 1's video to it.
    sim.handle_command(b"SET DEC 1 SWITCH 1 VIDEO")  # noop baseline
    sim._encoders[2] = sim._make_encoder(2)
    sim.handle_command(b"SET DEC 1 SWITCH 2 VIDEO")
    status = _strip(sim.handle_command(b"GET STATUS"), "GET STATUS")
    dec_rows = [ln for ln in status.split("\n") if ln.startswith("001")
                and "169.254.020" in ln]
    assert dec_rows and dec_rows[0].split()[2] == "002"  # From column
    detail = _strip(sim.handle_command(b"GET DEC 1 STATUS"), "GET DEC 1 STATUS")
    assert drv._parse_decoder_detail(detail)["source_video"] == 2


# ── Round-trip through the driver's own parsers (true duals) ──

def test_roundtrip_status_online_parses(sim):
    banner = _strip(sim.handle_command(b"GET STATUS"), "GET STATUS")
    p = drv._parse_status(banner)
    assert set(p["encoders"]) == {1}
    assert set(p["decoders"]) == {1}
    assert p["encoders"][1]["net"] is True
    assert p["encoders"][1]["gen"] == "TAV-CHAZY4K-TX"
    assert p["decoders"][1]["net"] is True
    assert p["decoders"][1]["source_video"] == 1
    assert p["system"]["firmware"] == "1.10.11"
    assert p["system"]["lan2_ip"] == "192.168.4.188"


def test_roundtrip_status_offline_parses(sim):
    sim.inject_error("endpoints_offline")
    banner = _strip(sim.handle_command(b"GET STATUS"), "GET STATUS")
    p = drv._parse_status(banner)
    assert p["encoders"][1]["net"] is False
    assert p["encoders"][1]["gen"] == ""  # blank Type column when offline
    assert p["decoders"][1]["net"] is False


def test_roundtrip_enc_detail_parses(sim):
    banner = _strip(sim.handle_command(b"GET ENC 1 STATUS"), "GET ENC 1 STATUS")
    p = drv._parse_encoder_detail(banner)
    assert p["net"] is True
    assert p["firmware"] == "1.10.03"
    assert p["ip"] == "169.254.10.1"
    assert p["mac"] == "18:66:96:11:0A:27"
    assert p["multicast"] is True
    assert p["io1_dir"] == "Out" and p["io1_phy"] == "Copper"


def test_roundtrip_dec_detail_parses(sim):
    banner = _strip(sim.handle_command(b"GET DEC 1 STATUS"), "GET DEC 1 STATUS")
    p = drv._parse_decoder_detail(banner)
    assert p["net"] is True
    assert p["mode"] == "MX"
    assert p["source_video"] == 1
    assert p["fix_video"] == 0
    assert p["multicast"] is True and p["video_mute"] is False


def test_roundtrip_gpio_parses(sim):
    banner = _strip(sim.handle_command(b"GET GPIO 0 STATUS"), "GET GPIO 0 STATUS")
    p = drv._parse_gpio(banner)
    assert p["gpio1_dir"] == "In"
    assert p["gpio1_level"] == 1
    assert p["gpio4_level"] == 1


# ── Config-child banners: byte-exact vs the captured child fixtures ──
#
# A fresh controller has none of these; the byte-exact captures were taken
# after one of each was created. Each test drives the sim into the captured
# state, then asserts GET <TYPE> STATUS matches byte-for-byte.

def test_group_status_byte_exact(sim):
    sim.handle_command(b"CREATE GROUP HANDLE 1")
    sim.handle_command(b"SET GROUP 1 NAME TestGroup")
    sim.handle_command(b"ADD GROUP 1 DEC 1")
    assert _strip(sim.handle_command(b"GET GROUP STATUS"), "GET GROUP STATUS") == \
        fxc.BANNER_GROUP_ALL
    # Per-handle returns the same single-instance view.
    assert _strip(sim.handle_command(b"GET GROUP 1 STATUS"), "GET GROUP 1 STATUS") == \
        fxc.BANNER_GROUP_DETAIL


def test_event_status_byte_exact(sim):
    sim.handle_command(b"CREATE EVENT HANDLE 1")
    sim.handle_command(b"SET EVENT 1 NAME TestEvent")
    assert _strip(sim.handle_command(b"GET EVENT STATUS"), "GET EVENT STATUS") == \
        fxc.BANNER_EVENT_ALL
    assert _strip(sim.handle_command(b"GET EVENT 1 STATUS"), "GET EVENT 1 STATUS") == \
        fxc.BANNER_EVENT_DETAIL


def test_wall_status_byte_exact(sim):
    sim.handle_command(b"CREATE WALL HANDLE 1")
    assert _strip(sim.handle_command(b"GET WALL STATUS"), "GET WALL STATUS") == \
        fxc.BANNER_WALL_ALL
    assert _strip(sim.handle_command(b"GET WALL 1 STATUS"), "GET WALL 1 STATUS") == \
        fxc.BANNER_WALL_DETAIL


def test_dante_preset_status_byte_exact(sim):
    sim.handle_command(b"CREATE DANTE PRESET HANDLE 1")
    # Routing detail is decoration the schema doesn't track; construct the
    # captured TestDP routes directly (precedent: poking sim._encoders).
    sim._dante_presets[1] = {
        "id": 1, "name": "TestDP",
        "routes": [
            ("Decoder-001", "video", "01", "Encoder-001", "01"),
            ("Decoder-001", "audio", "01", "0", "00"),
            ("Decoder-001", "audio", "02", "0", "00"),
        ],
    }
    out = _strip(sim.handle_command(b"GET DANTE PRESET STATUS"), "GET DANTE PRESET STATUS")
    assert out == fxc.BANNER_DANTE_PRESET_ALL
    out1 = _strip(sim.handle_command(b"GET DANTE PRESET 1 STATUS"),
                  "GET DANTE PRESET 1 STATUS")
    assert out1 == fxc.BANNER_DANTE_PRESET_DETAIL


# ── Empty banners + lifecycle reflected in subsequent reads ──

def test_config_status_empty_by_default(sim):
    assert "No Group" in _strip(sim.handle_command(b"GET GROUP STATUS"), "GET GROUP STATUS")
    assert "No Event" in _strip(sim.handle_command(b"GET EVENT STATUS"), "GET EVENT STATUS")
    assert "No Video Wall" in _strip(sim.handle_command(b"GET WALL STATUS"), "GET WALL STATUS")
    assert "No Dante Preset" in _strip(
        sim.handle_command(b"GET DANTE PRESET STATUS"), "GET DANTE PRESET STATUS")


def test_create_delete_group_reflected(sim):
    sim.handle_command(b"CREATE GROUP HANDLE 5")
    g = drv._parse_group_status(
        _strip(sim.handle_command(b"GET GROUP STATUS"), "GET GROUP STATUS"))
    assert set(g) == {5}
    sim.handle_command(b"DELETE GROUP HANDLE 5")
    assert drv._parse_group_status(
        _strip(sim.handle_command(b"GET GROUP STATUS"), "GET GROUP STATUS")) == {}


# ── Round-trip through the driver's own config parsers (true duals) ──

def test_roundtrip_group_parses(sim):
    sim.handle_command(b"CREATE GROUP HANDLE 1")
    sim.handle_command(b"SET GROUP 1 NAME TestGroup")
    sim.handle_command(b"ADD GROUP 1 DEC 1")
    g = drv._parse_group_status(
        _strip(sim.handle_command(b"GET GROUP STATUS"), "GET GROUP STATUS"))
    assert g == {1: {"name": "TestGroup", "member_count": 1}}


def test_roundtrip_event_parses(sim):
    sim.handle_command(b"CREATE EVENT HANDLE 1")
    sim.handle_command(b"SET EVENT 1 NAME TestEvent")
    e = drv._parse_event_status(
        _strip(sim.handle_command(b"GET EVENT STATUS"), "GET EVENT STATUS"))
    assert e[1]["name"] == "TestEvent"
    assert e[1]["event_type"] == "TCP"
    assert e[1]["address"] == ""


def test_roundtrip_wall_parses(sim):
    sim.handle_command(b"CREATE WALL HANDLE 1")
    w = drv._parse_wall_status(
        _strip(sim.handle_command(b"GET WALL STATUS"), "GET WALL STATUS"))
    assert w == {1: {"name": "", "columns": 2, "rows": 2}}


def test_roundtrip_dante_preset_parses(sim):
    sim._dante_presets[1] = sim._make_dante_preset(1, name="TestDP")
    p = drv._parse_dante_preset_status(
        _strip(sim.handle_command(b"GET DANTE PRESET STATUS"), "GET DANTE PRESET STATUS"))
    assert p == {1: {"name": "TestDP"}}


def test_wall_status_multi_instance(sim):
    # Live two-wall hardware query showed each wall is a full VW... block
    # separated by one blank line; the parser must read both and the sim must
    # emit the separator.
    sim.handle_command(b"CREATE WALL HANDLE 1")
    sim._walls[2] = sim._make_wall(2, columns=3, rows=1)
    out = _strip(sim.handle_command(b"GET WALL STATUS"), "GET WALL STATUS")
    w = drv._parse_wall_status(out)
    assert set(w) == {1, 2}
    assert w[1] == {"name": "", "columns": 2, "rows": 2}
    assert w[2] == {"name": "", "columns": 3, "rows": 1}
    lines = out.split("\n")
    vw_idx = [i for i, ln in enumerate(lines) if ln.startswith("VW  Col")]
    assert len(vw_idx) == 2
    assert lines[vw_idx[1] - 1] == ""  # blank line separates the two wall blocks
