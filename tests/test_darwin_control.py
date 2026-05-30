"""Unit tests for the darwin_control driver.

Loads ``switchers/darwin_control.py`` directly, stubbing the ``server.*``
imports it needs (BaseDriver, TCPTransport, get_logger) so the community repo's
test suite stays self-contained — mirrors test_chazy_control_pro.py.

The banner parsers are exercised against byte-exact captures from real
hardware (Controller(h) FW 1.50.02, one TX + one RX enrolled) in
fixtures/darwin_control_banners.py.
"""

from __future__ import annotations

import importlib.util
import logging
import string
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "switchers" / "darwin_control.py"
FIXTURES_PATH = REPO_ROOT / "tests" / "fixtures" / "darwin_control_banners.py"


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

    class _BaseDriver:  # minimal stand-in; we only test module-level code
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
    logger.get_logger = lambda name="darwin": logging.getLogger(name)
    sys.modules["server.utils.logger"] = logger


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_server_stubs()
drv = _load(DRIVER_PATH, "darwin_control_under_test")
fx = _load(FIXTURES_PATH, "darwin_fixtures_under_test")
INFO = drv.DarwinControlDriver.DRIVER_INFO

_SPECIAL = {"search", "add_auto_all", "exit_guest"} | set(drv._RESET_CONFIRM)


# ── Command surface consistency ──

def test_every_command_has_a_handler():
    for name in INFO["commands"]:
        assert (name in drv._COMMAND_TEMPLATES or name in drv._LIFECYCLE_COMMANDS
                or name in _SPECIAL), f"{name} has no handler"


def test_no_orphan_templates_or_lifecycle():
    for name in drv._COMMAND_TEMPLATES:
        assert name in INFO["commands"], f"template {name} not declared"
    for name in drv._LIFECYCLE_COMMANDS:
        assert name in INFO["commands"], f"lifecycle {name} not declared"


def test_template_placeholders_have_params():
    fmt = string.Formatter()
    sources = list(drv._COMMAND_TEMPLATES.items())
    sources += [(k, v["template"]) for k, v in drv._LIFECYCLE_COMMANDS.items()]
    for name, tpl in sources:
        placeholders = {f for _, f, _, _ in fmt.parse(tpl) if f}
        params = set(INFO["commands"][name].get("params", {}))
        missing = placeholders - params
        assert not missing, f"{name}: placeholders without params: {missing}"


def test_child_id_params_reference_declared_types():
    types = set(INFO["child_entity_types"])
    for name, cdef in INFO["commands"].items():
        for pname, pdef in cdef.get("params", {}).items():
            if pdef.get("type") == "child_id":
                assert pdef.get("child_type") in types, f"{name}.{pname}"


def test_enum_params_have_values():
    for name, cdef in INFO["commands"].items():
        for pname, pdef in cdef.get("params", {}).items():
            if pdef.get("type") == "enum":
                assert pdef.get("values"), f"{name}.{pname} enum has no values"


def test_writethrough_targets_are_real_commands_and_state():
    enc_state = INFO["child_entity_types"]["encoder"]["state_variables"]
    for cmd, (ctype, id_param, fn) in drv._WRITETHROUGH.items():
        assert cmd in INFO["commands"], f"write-through {cmd} not a command"
        assert ctype == "encoder"
        # the produced keys must all be declared encoder state vars
        sample = {id_param: 1, "rate": "2", "type": "1", "audio": "ON",
                  "sh": "640", "sv": "540", "format": "AAC"}
        for key in fn(sample):
            assert key in enc_state, f"{cmd} writes undeclared state {key}"


# ── GET STATUS parser ──

def test_parse_status_empty_roster():
    ps = drv._parse_status(fx.BANNER_STATUS_EMPTY)
    assert ps["encoders"] == {}
    assert ps["decoders"] == {}
    s = ps["system"]
    assert s["firmware"] == "1.50.02"
    assert s["power"] is True and s["ir"] is True
    assert s["web"] is True and s["https"] is False and s["ssh"] is False
    assert s["telnet_port"] == "23"
    assert s["lan2_ip"] == "192.168.4.33"
    assert s["lan2_subnet_mask"] == "255.255.252.0"
    assert s["lan1_mac"] == "18:66:96:11:11:48"
    assert s["hostname"] == "Controller.local"


def test_parse_status_populated_roster():
    ps = drv._parse_status(fx.BANNER_STATUS_POPULATED)
    assert sorted(ps["encoders"]) == [1]
    e = ps["encoders"][1]
    assert e["ip"] == "169.254.10.1" and e["net"] is True and e["signal_present"] is False
    assert e["edid"] == "DF000"
    assert e["audio_format"] == "PCM"  # AudioFormat column is device-reported
    assert sorted(ps["decoders"]) == [1]
    de = ps["decoders"][1]
    assert de["source"] == 1 and de["net"] is True and de["hpd"] is False
    assert de["resolution"] == "1080p@60" and de["mode"] == "MX" and de["hdcp"] == "SNK"


# ── GET ENC/DEC detail parsers ──

def test_parse_encoder_detail():
    e = drv._parse_encoder_detail(fx.BANNER_ENC)
    assert e["net"] is True and e["signal_present"] is False
    assert e["firmware"] == "3.12.02"
    assert e["edid"] == "DF000" and e["audio_input"] == "HDMI"
    assert e["multicast"] is True and e["name"] == "Encoder 001"
    assert e["fpled"] == "9"
    assert e["guest_enabled"] is False and e["guest_baud"] == "9" and e["guest_framing"] == "8n1"
    assert e["mac"] == "18:66:96:11:0D:EB"
    assert e["ip"] == "169.254.10.1" and e["gateway"] == "169.254.8.1"
    assert e["subnet_mask"] == "255.255.0.0"


def test_parse_decoder_detail():
    x = drv._parse_decoder_detail(fx.BANNER_DEC)
    assert x["net"] is True and x["hpd"] is False and x["firmware"] == "3.12.01"
    assert x["mode"] == "MX" and x["resolution"] == "1080p@60" and x["rotate"] == "0"
    assert x["name"] == "Decoder 001"
    assert x["source"] == 1
    assert x["lock_video"] == 0 and x["lock_ir"] == 0 and x["lock_rs232"] == 0 and x["lock_usb"] == 0
    assert x["multicast"] is True
    assert x["aspect"] == "Maintain" and x["hdcp"] == "SNK"
    assert x["ir_enabled"] is True and x["button"] is True and x["fpled"] == "9"
    assert x["guest_baud"] == "9" and x["guest_framing"] == "8n1"
    assert x["video_output"] is True and x["video_mute"] is False and x["pause"] is False
    assert x["auto"] is True and x["video_lost_timeout"] == 0
    assert x["mac"] == "18:66:96:11:0D:1D" and x["ip"] == "169.254.20.1"


def test_parse_gpio():
    g = drv._parse_gpio(fx.BANNER_GPIO)
    assert g["gpio1_dir"] == "In" and g["gpio1_level"] == 1
    assert g["gpio4_dir"] == "In"


def test_parse_wall_empty():
    assert drv._parse_wall_status(fx.BANNER_WALL_EMPTY) == {}


def test_routing_signals_have_no_audio_or_cec():
    # Darwin routes VIDEO/IR/RS232/USB only (audio embedded, no CEC).
    assert "AUDIO" not in drv.SIGNAL_TYPES
    assert "CEC" not in drv.SIGNAL_TYPES
    assert set(drv.SIGNAL_TYPES) == {"ALL", "VIDEO", "IR", "RS232", "USB"}


def test_hotkey_k0_is_unpadded():
    """The KVM hotkey modifier (k0) must be an UNPADDED 1-9 enum.

    Live hardware (FW 1.50.02) rejects a zero-padded modifier with
    ``[ERROR]OUT parameter out of range`` (9/9 rejected) and accepts the
    unpadded form (9/9 accepted). The IDE enum once shipped padded
    ("01".."09"), which made the hotkey command always fail when driven from
    the picker. Guard against regressing to the padded form, and confirm the
    rendered wire carries a single-digit modifier.
    """
    k0 = INFO["commands"]["dec_hotkey_set"]["params"]["k0"]
    assert k0["values"] == [str(x) for x in range(1, 10)]
    assert all(not v.startswith("0") for v in k0["values"])
    wire = drv._COMMAND_TEMPLATES["dec_hotkey_set"].format(
        decoder_id=1, nn=1, k0="1", k1=65, action="PULL", encoder_id=2)
    assert wire == "SET DEC 1 HOTKEY 1 KEY 1 65 ACTION PULL SRC 2"
