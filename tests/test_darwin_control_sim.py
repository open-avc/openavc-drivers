"""Unit tests for the darwin_control simulator.

Loads ``switchers/darwin_control_sim.py`` directly, stubbing the ``openavc.simulator.*``
base it imports (so the community repo's test suite stays self-contained —
mirrors test_chazy_control_pro_sim.py).

The simulator's banners are asserted byte-for-byte against the same
real-hardware captures the driver parses (fixtures/darwin_control_banners.py).
A final block feeds the simulator's output back through the *driver's* parsers
to prove the two are exact duals.
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
SIM_PATH = REPO_ROOT / "switchers" / "darwin_control_sim.py"
DRIVER_PATH = REPO_ROOT / "switchers" / "darwin_control.py"
FIXTURES_PATH = REPO_ROOT / "tests" / "fixtures" / "darwin_control_banners.py"


def _install_simulator_stub() -> None:
    """Minimal stand-in for openavc.simulator.tcp_simulator.TCPSimulator (state + error
    modes) — the parts the simulator relies on."""
    if "openavc.simulator.tcp_simulator" in sys.modules:
        return
    pkg = ModuleType("openavc.simulator")
    pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("openavc.simulator", pkg)
    mod = ModuleType("openavc.simulator.tcp_simulator")

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

        def has_error_behavior(self, behavior):
            return any(
                self._error_modes.get(m, {}).get("behavior") == behavior
                for m in self._active_errors
            )

    mod.TCPSimulator = _TCPSimulator
    sys.modules["openavc.simulator.tcp_simulator"] = mod


def _install_server_stubs() -> None:
    if "openavc.drivers.base" in sys.modules:
        return
    server = ModuleType("openavc")
    server.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("openavc", server)
    drivers = ModuleType("openavc.drivers")
    drivers.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("openavc.drivers", drivers)
    base = ModuleType("openavc.drivers.base")

    class _BaseDriver:
        DRIVER_INFO: dict = {}

    base.BaseDriver = _BaseDriver
    sys.modules["openavc.drivers.base"] = base

    transport = ModuleType("openavc.transport")
    transport.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("openavc.transport", transport)
    tcp = ModuleType("openavc.transport.tcp")

    class _TCPTransport:
        pass

    tcp.TCPTransport = _TCPTransport
    sys.modules["openavc.transport.tcp"] = tcp

    utils = ModuleType("openavc.utils")
    utils.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("openavc.utils", utils)
    logger = ModuleType("openavc.utils.logger")
    logger.get_logger = lambda name="darwin": logging.getLogger(name)
    sys.modules["openavc.utils.logger"] = logger


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_simulator_stub()
_install_server_stubs()
simmod = _load(SIM_PATH, "darwin_control_sim_under_test")
drv = _load(DRIVER_PATH, "darwin_control_driver_for_sim_test")
fx = _load(FIXTURES_PATH, "darwin_fixtures_for_sim_test")

Sim = simmod.DarwinControlSimulator


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
    return drv.DarwinControlDriver._strip_echo(unit, sent)


def _send(sim, wire: str) -> str:
    return _strip(sim.handle_command(wire.encode("latin-1")), wire)


# ── SIMULATOR_INFO validity ──

def test_driver_id_and_transport_match():
    info = Sim.SIMULATOR_INFO
    assert info["driver_id"] == drv.DarwinControlDriver.DRIVER_INFO["id"]
    assert info["transport"] == drv.DarwinControlDriver.DRIVER_INFO["transport"]
    assert info["default_port"] == drv.DarwinControlDriver.DRIVER_INFO["ports"][0]


def test_all_controller_state_vars_in_initial_state():
    initial = Sim.SIMULATOR_INFO["initial_state"]
    declared = drv.DarwinControlDriver.DRIVER_INFO["state_variables"]
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
    assert greeting[len(simmod._IAC_GREETING):].decode("latin-1") == fx.RAW_GREETING


def test_driver_filter_telnet_strips_greeting_to_capture(sim):
    greeting = asyncio.run(sim.on_client_connected("c1"))
    d = drv.DarwinControlDriver.__new__(drv.DarwinControlDriver)
    d._iac_state = "normal"
    assert d._filter_telnet(greeting).decode("latin-1") == fx.RAW_GREETING


def test_command_is_echoed_and_prompt_terminated(sim):
    framed = sim.handle_command(b"GET STATUS")
    assert framed.startswith(b"GET STATUS\r\n")
    assert framed.endswith(simmod.PROMPT)
    assert b"\r\n" in framed  # CRLF line endings on the wire


# ── Byte-exact banners: seeded (1 enc + 1 dec, online) ──

def test_status_populated_byte_exact(sim):
    assert _send(sim, "GET STATUS") == fx.BANNER_STATUS_POPULATED


def test_enc_detail_byte_exact(sim):
    assert _send(sim, "GET ENC 1 STATUS") == fx.BANNER_ENC


def test_dec_detail_byte_exact(sim):
    assert _send(sim, "GET DEC 1 STATUS") == fx.BANNER_DEC


def test_gpio_byte_exact(sim):
    assert _send(sim, "GET GPIO 0 STATUS") == fx.BANNER_GPIO


def test_wall_empty_byte_exact(sim):
    assert _send(sim, "GET WALL STATUS") == fx.BANNER_WALL_EMPTY


# ── Byte-exact banners: empty controller + search lifecycle ──

def test_status_empty_after_reset(sim):
    assert "[SUCCESS]" in _send(sim, "ADD DEV RESET")
    assert _send(sim, "GET STATUS") == fx.BANNER_STATUS_EMPTY


def test_enc_empty_after_delete(sim):
    _send(sim, "SET ENC 1 DELETE")
    assert _send(sim, "GET ENC STATUS") == fx.BANNER_ENC_EMPTY


def test_enc_not_found_byte_exact(sim):
    _send(sim, "SET ENC 1 DELETE")
    assert _send(sim, "GET ENC 1 STATUS") == fx.BANNER_ENC_ABSENT


def test_dec_not_found(sim):
    assert _send(sim, "GET DEC 9 STATUS") == "[ERROR]Decoder 009 not exist."


def test_search_found_after_reset(sim):
    _send(sim, "ADD DEV RESET")
    assert _send(sim, "SEARCH") == fx.BANNER_SEARCH


def test_search_empty_when_all_assigned(sim):
    out = _send(sim, "SEARCH")
    # Default seed has both endpoints enrolled, so nothing new is found.
    assert "None" in out
    assert "169.254.236.013" not in out


def test_add_auto_all_reenrols_to_populated(sim):
    _send(sim, "ADD DEV RESET")
    out = _send(sim, "ADD AUTO ALL")
    assert "[SUCCESS]Add dev search IP:169.254.236.013  device to encoder." in out
    assert "[SUCCESS]Add dev search IP:169.254.030.013  device to decoder." in out
    assert _send(sim, "GET STATUS") == fx.BANNER_STATUS_POPULATED


def test_add_auto_all_ack_omits_id(sim):
    # HW behaviour: the ADD AUTO ALL ack names the search IP, not the assigned id.
    _send(sim, "ADD DEV RESET")
    out = _send(sim, "ADD AUTO ALL")
    assert "device to encoder." in out and "encoder 001" not in out.lower()


# ── Errors + confirm flow ──

def test_unknown_command(sim):
    assert _send(sim, "FLARGLE") == "[ERROR]Command not found."


def test_set_on_missing_endpoint_errors(sim):
    assert _send(sim, "SET ENC 5 NAME Foo") == "[ERROR]Encoder 005 not exist."


def test_reset_confirm_yes(sim):
    q = _send(sim, "SET RESET")
    assert '"Yes"' in q
    assert "[SUCCESS]" in _send(sim, "Yes")


def test_reset_confirm_no_cancels(sim):
    _send(sim, "SET RESET")
    assert "[SUCCESS]" in _send(sim, "No")


# ── Stateful mutations reflect in subsequent reads ──

def test_set_name_reflects_in_detail(sim):
    _send(sim, "SET ENC 1 NAME Lectern PC")
    banner = _send(sim, "GET ENC 1 STATUS")
    assert "Lectern PC" in banner
    assert drv._parse_encoder_detail(banner)["name"] == "Lectern PC"


def test_dec_switch_reflects_in_status_and_detail(sim):
    _send(sim, "SET DEC 1 SWITCH 2 VIDEO")
    status = _send(sim, "GET STATUS")
    assert drv._parse_status(status)["decoders"][1]["source"] == 2
    detail = _send(sim, "GET DEC 1 STATUS")
    assert drv._parse_decoder_detail(detail)["source"] == 2


def test_dec_output_toggle_reflects(sim):
    _send(sim, "SET DEC 1 OUTPUT OFF")
    assert drv._parse_decoder_detail(_send(sim, "GET DEC 1 STATUS"))["video_output"] is False
    _send(sim, "SET DEC 1 OUTPUT ON")
    assert drv._parse_decoder_detail(_send(sim, "GET DEC 1 STATUS"))["video_output"] is True


def test_dec_pause_and_hdcp_reflect(sim):
    _send(sim, "SET DEC 1 OUTPUT PAUSE ON")
    _send(sim, "SET DEC 1 OSP H22")
    d = drv._parse_decoder_detail(_send(sim, "GET DEC 1 STATUS"))
    assert d["pause"] is True
    assert d["hdcp"] == "H22"


def test_dec_lost_button_ir_fpled_reflect(sim):
    # Device-reported decoder settings the sim previously didn't model (review D2).
    _send(sim, "SET DEC 1 OUTPUT LOST 5")
    _send(sim, "SET DEC 1 BUTTON OFF")
    _send(sim, "SET DEC 1 IR OFF")
    _send(sim, "SET DEC 1 FPLED 0")
    d = drv._parse_decoder_detail(_send(sim, "GET DEC 1 STATUS"))
    assert d["video_lost_timeout"] == 5
    assert d["button"] is False
    assert d["ir_enabled"] is False
    assert d["fpled"] == "0"


def test_dec_resolution_and_rotate_reflect(sim):
    # These SET values live at the third token; the sim used to store the
    # keyword. Lock the corrected round-trip.
    _send(sim, "SET DEC 1 OUTPUT RESOLUTION 08")
    _send(sim, "SET DEC 1 OUTPUT ROTATE 2")
    d = drv._parse_decoder_detail(_send(sim, "GET DEC 1 STATUS"))
    assert d["resolution"] == "576p@50"  # RES_LABELS["08"]
    assert d["rotate"] == "180"  # ROTATE_DEG["2"]


def test_wall_create_delete_reflected(sim):
    _send(sim, "CREATE WALL HANDLE 1")
    w = drv._parse_wall_status(_send(sim, "GET WALL STATUS"))
    assert w == {1: {"name": "", "columns": 2, "rows": 2}}
    _send(sim, "DELETE WALL HANDLE 1")
    assert drv._parse_wall_status(_send(sim, "GET WALL STATUS")) == {}


# ── Round-trip through the driver's own parsers (true duals) ──

def test_roundtrip_status_parses(sim):
    p = drv._parse_status(_send(sim, "GET STATUS"))
    assert set(p["encoders"]) == {1}
    assert set(p["decoders"]) == {1}
    assert p["encoders"][1]["net"] is True
    assert p["encoders"][1]["edid"] == "DF000"
    assert p["encoders"][1]["ip"] == "169.254.10.1"
    assert p["decoders"][1]["net"] is True
    assert p["decoders"][1]["source"] == 1
    assert p["decoders"][1]["resolution"] == "1080p@60"
    assert p["decoders"][1]["mode"] == "MX"
    assert p["decoders"][1]["hdcp"] == "SNK"
    assert p["system"]["firmware"] == "1.50.02"
    assert p["system"]["lan2_ip"] == "192.168.4.33"
    assert p["system"]["web"] is True


def test_roundtrip_enc_detail_parses(sim):
    p = drv._parse_encoder_detail(_send(sim, "GET ENC 1 STATUS"))
    assert p["net"] is True and p["signal_present"] is False
    assert p["firmware"] == "3.12.02"
    assert p["edid"] == "DF000" and p["audio_input"] == "HDMI"
    assert p["multicast"] is True and p["name"] == "Encoder 001"
    assert p["fpled"] == "9"
    assert p["guest_enabled"] is False and p["guest_baud"] == "9" and p["guest_framing"] == "8n1"
    assert p["mac"] == "18:66:96:11:0D:EB"
    assert p["ip"] == "169.254.10.1" and p["gateway"] == "169.254.8.1"


def test_roundtrip_dec_detail_parses(sim):
    p = drv._parse_decoder_detail(_send(sim, "GET DEC 1 STATUS"))
    assert p["net"] is True and p["hpd"] is False and p["firmware"] == "3.12.01"
    assert p["mode"] == "MX" and p["resolution"] == "1080p@60" and p["rotate"] == "0"
    assert p["name"] == "Decoder 001" and p["source"] == 1
    assert p["lock_video"] == 0 and p["lock_usb"] == 0
    assert p["multicast"] is True
    assert p["aspect"] == "Maintain" and p["hdcp"] == "SNK"
    assert p["ir_enabled"] is True and p["button"] is True and p["fpled"] == "9"
    assert p["video_output"] is True and p["video_mute"] is False and p["pause"] is False
    assert p["auto"] is True and p["video_lost_timeout"] == 0
    assert p["mac"] == "18:66:96:11:0D:1D" and p["ip"] == "169.254.20.1"


def test_roundtrip_gpio_parses(sim):
    p = drv._parse_gpio(_send(sim, "GET GPIO 0 STATUS"))
    assert p["gpio1_dir"] == "In" and p["gpio1_level"] == 1
    assert p["gpio4_dir"] == "In" and p["gpio4_level"] == 1


def test_roundtrip_wall_parses(sim):
    _send(sim, "CREATE WALL HANDLE 1")
    _send(sim, "SET WALL 1 C 3 R 1")
    w = drv._parse_wall_status(_send(sim, "GET WALL STATUS"))
    assert w == {1: {"name": "", "columns": 3, "rows": 1}}
