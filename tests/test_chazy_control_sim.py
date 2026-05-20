"""Unit tests for the chazy_control (standard, non-Pro) simulator.

Loads ``switchers/chazy_control_sim.py`` directly, stubbing the ``simulator.*``
base it imports, so the community repo's test suite stays self-contained
(mirrors test_chazy_control_pro_sim.py).

Unlike the Control Pro simulator, there is no standalone Control hardware to
capture from, so the banners are not asserted byte-exact against a fixture.
Instead these tests prove (a) the simulator carries the standard Control's
identity, (b) Pro-only commands are rejected, and (c) every rendered banner
round-trips cleanly through the *driver's* own parsers — i.e. the simulator and
driver are exact duals over the standard Control's command surface.
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
SIM_PATH = REPO_ROOT / "switchers" / "chazy_control_sim.py"
DRIVER_PATH = REPO_ROOT / "switchers" / "chazy_control.py"


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
simmod = _load(SIM_PATH, "chazy_control_sim_under_test")
drv = _load(DRIVER_PATH, "chazy_control_driver_for_sim_test")

Sim = simmod.ChazyControlSimulator


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
    return drv.ChazyControlDriver._strip_echo(unit, sent)


# ── SIMULATOR_INFO validity ──

def test_driver_id_and_transport_match():
    info = Sim.SIMULATOR_INFO
    assert info["driver_id"] == drv.ChazyControlDriver.DRIVER_INFO["id"]
    assert info["transport"] == drv.ChazyControlDriver.DRIVER_INFO["transport"]


def test_all_controller_state_vars_in_initial_state():
    initial = Sim.SIMULATOR_INFO["initial_state"]
    declared = drv.ChazyControlDriver.DRIVER_INFO["state_variables"]
    for name, vardef in declared.items():
        assert name in initial, f"{name} missing from simulator initial_state"
        if vardef["type"] == "boolean":
            assert isinstance(initial[name], bool), f"{name} should be bool"
        if vardef["type"] == "integer":
            assert isinstance(initial[name], int), f"{name} should be int"


def test_initial_state_has_no_pro_only_keys():
    # The standard Control has no Date/Time module.
    initial = Sim.SIMULATOR_INFO["initial_state"]
    assert "date" not in initial and "ntp_server" not in initial


# ── Connect framing + identity ──

def test_greeting_has_iac_and_prompt(sim):
    greeting = asyncio.run(sim.on_client_connected("c1"))
    assert greeting.startswith(b"\xff"), "greeting must open with Telnet IAC"
    assert greeting.endswith(simmod.PROMPT)
    body = greeting[len(simmod._IAC_GREETING):].decode("latin-1")
    assert "Welcome To CHAZY CONTROL Terminal Control System" in body
    assert "FW Version: 1.00.17" in body


def test_driver_filter_telnet_strips_greeting(sim):
    greeting = asyncio.run(sim.on_client_connected("c1"))
    d = drv.ChazyControlDriver.__new__(drv.ChazyControlDriver)
    d._iac_state = "normal"
    cleaned = d._filter_telnet(greeting).decode("latin-1")
    assert "CHAZY CONTROL" in cleaned
    assert b"\xff" not in cleaned.encode("latin-1")  # all IAC stripped


def test_command_is_echoed_and_prompt_terminated(sim):
    framed = sim.handle_command(b"GET STATUS")
    assert framed.startswith(b"GET STATUS\r\n")
    assert framed.endswith(simmod.PROMPT)


def test_status_carries_standard_control_identity(sim):
    banner = _strip(sim.handle_command(b"GET STATUS"), "GET STATUS")
    assert "CHAZY CONTROL Status Info" in banner
    assert "TAV-CHAZY-CLTPRO" not in banner   # not the Pro


# ── Round-trip through the driver's parsers (true duals) ──

def test_roundtrip_status_online_parses(sim):
    banner = _strip(sim.handle_command(b"GET STATUS"), "GET STATUS")
    p = drv._parse_status(banner)
    assert set(p["encoders"]) == {1}
    assert set(p["decoders"]) == {1}
    assert p["encoders"][1]["net"] is True
    assert p["encoders"][1]["gen"] == "TAV-CHAZY4K-TX"
    assert p["decoders"][1]["net"] is True
    assert p["decoders"][1]["source_video"] == 1
    assert p["system"]["firmware"] == "1.00.17"
    assert p["system"]["lan2_ip"] == "192.168.6.100"
    assert p["system"]["dns_preferred"] == "192.168.6.1"
    assert p["system"]["hostname"] == "controller.local"


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


# ── Search lifecycle ──

def test_search_empty_when_all_assigned(sim):
    banner = _strip(sim.handle_command(b"SEARCH"), "SEARCH")
    assert "==New Encoder" in banner and "None" in banner


def test_search_found_after_delete(sim):
    sim.handle_command(b"SET ENC 1 DELETE")
    sim.handle_command(b"SET DEC 1 DELETE")
    banner = _strip(sim.handle_command(b"SEARCH"), "SEARCH")
    assert "18:66:96:11:0A:27" in banner  # the freed TX shows up in search


def test_status_empty_after_delete(sim):
    sim.handle_command(b"SET ENC 1 DELETE")
    sim.handle_command(b"SET DEC 1 DELETE")
    banner = _strip(sim.handle_command(b"GET STATUS"), "GET STATUS")
    p = drv._parse_status(banner)
    assert p["encoders"] == {} and p["decoders"] == {}


def test_add_auto_all_re_adds_offline(sim):
    sim.handle_command(b"SET ENC 1 DELETE")
    sim.handle_command(b"SET DEC 1 DELETE")
    out = _strip(sim.handle_command(b"ADD AUTO ALL"), "ADD AUTO ALL")
    assert "[SUCCESS]" in out
    sim.inject_error("endpoints_offline")
    banner = _strip(sim.handle_command(b"GET STATUS"), "GET STATUS")
    p = drv._parse_status(banner)
    assert p["encoders"][1]["net"] is False


# ── Errors + confirm flow ──

def test_enc_not_found(sim):
    out = _strip(sim.handle_command(b"GET ENC 2 STATUS"), "GET ENC 2 STATUS")
    assert out == "[ERROR]Encoder 002 does not exist."


def test_unknown_command(sim):
    assert _strip(sim.handle_command(b"FLARGLE"), "FLARGLE") == \
        "[ERROR]Command not found."


def test_set_on_missing_endpoint_errors(sim):
    out = _strip(sim.handle_command(b"SET ENC 5 NAME Foo"), "SET ENC 5 NAME Foo")
    assert out == "[ERROR]Encoder 005 does not exist."


def test_reset_confirm_yes(sim):
    q = _strip(sim.handle_command(b"SET RESET"), "SET RESET")
    assert 'Type "Yes"' in q
    assert "[SUCCESS]" in _strip(sim.handle_command(b"Yes"), "Yes")


def test_reset_confirm_no_cancels(sim):
    _strip(sim.handle_command(b"SET RESET"), "SET RESET")
    assert _strip(sim.handle_command(b"No"), "No") == "[SUCCESS]RESET process ignored."


# ── Pro-only modules are rejected (the defining behavioural delta) ──

@pytest.mark.parametrize("cmd", [
    b"GET DATE",
    b"GET NTP",
    b"SET DATE 2026-05-20 12:00:00",
    b"SET NTP SERVER time.nist.gov",
    b"ADD MEDIA HANDLE 1",
    b"SET MEDIA 1 NAME Foo",
    b"CREATE GROUP HANDLE 1",
    b"SET GROUP 1 NAME Foo",
    b"CREATE EVENT HANDLE 1",
    b"CREATE SCHEDULE HANDLE 1",
    b"SAVE CONFIG PRESET 1 NAME Foo",
    b"APPLY CONFIG PRESET 1",
    b"CREATE DANTE PRESET HANDLE 1",
    b"SET DANTE PRESET 1 NAME Foo",
    b"APPLY DANTE PRESET 1",
])
def test_pro_only_commands_rejected(sim, cmd):
    out = _strip(sim.handle_command(cmd), cmd.decode())
    assert out == "[ERROR]Command not found.", f"{cmd!r} should be rejected"


def test_shared_commands_still_accepted(sim):
    # Video wall (shared lifecycle), Dante routing, and decoder routing all work.
    assert "[SUCCESS]" in _strip(sim.handle_command(b"CREATE WALL HANDLE 1"),
                                 "CREATE WALL HANDLE 1")
    assert "[SUCCESS]" in _strip(sim.handle_command(b"SET DANTE DEV foo NAME bar"),
                                 "SET DANTE DEV foo NAME bar")
    assert "[SUCCESS]" in _strip(sim.handle_command(b"SET DEC 1 SWITCH 1 VIDEO"),
                                 "SET DEC 1 SWITCH 1 VIDEO")


# ── Stateful mutations reflect in subsequent reads ──

def test_set_name_reflects_in_detail(sim):
    sim.handle_command(b"SET ENC 1 NAME Lectern PC")
    banner = _strip(sim.handle_command(b"GET ENC 1 STATUS"), "GET ENC 1 STATUS")
    assert "Lectern PC" in banner
    assert drv._parse_encoder_detail(banner)["name"] == "Lectern PC"


def test_dec_switch_reflects_in_detail(sim):
    sim._encoders[2] = sim._make_encoder(2)
    sim.handle_command(b"SET DEC 1 SWITCH 2 VIDEO")
    detail = _strip(sim.handle_command(b"GET DEC 1 STATUS"), "GET DEC 1 STATUS")
    assert drv._parse_decoder_detail(detail)["source_video"] == 2
