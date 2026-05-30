"""Unit tests for the chazy_control_pro driver.

Loads ``switchers/chazy_control_pro.py`` directly, stubbing the ``server.*``
imports it needs (BaseDriver, TCPTransport, get_logger) so the community repo's
test suite stays self-contained — mirrors test_crestron_cip_discovery.py.

The banner parsers are exercised against byte-exact captures from real
hardware (FW 1.10.11) in fixtures/chazy_control_pro_banners.py, in both the
offline (just-added) and online (linked) device states.
"""

from __future__ import annotations

import importlib.util
import logging
import string
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "switchers" / "chazy_control_pro.py"
FIXTURES_PATH = REPO_ROOT / "tests" / "fixtures" / "chazy_control_pro_banners.py"
CHILD_FIXTURES_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "chazy_control_pro_child_banners.py"
)


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
    logger.get_logger = lambda name="chazy": logging.getLogger(name)
    sys.modules["server.utils.logger"] = logger


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_server_stubs()
drv = _load(DRIVER_PATH, "chazy_control_pro_under_test")
fx = _load(FIXTURES_PATH, "chazy_fixtures_under_test")
fxc = _load(CHILD_FIXTURES_PATH, "chazy_child_fixtures_under_test")
INFO = drv.ChazyControlProDriver.DRIVER_INFO


# ── Command surface consistency ──

def test_every_command_has_a_handler():
    special = {"search", "add_auto_all"} | set(drv._RESET_CONFIRM)
    for name in INFO["commands"]:
        assert (name in drv._COMMAND_TEMPLATES or name in drv._LIFECYCLE_COMMANDS
                or name in special), f"{name} has no handler"


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
                assert pdef.get("values"), f"{name}.{pname} enum missing values"


def test_reset_commands_present():
    for name in ("reset_system_confirm", "reset_network_confirm", "reset_all_confirm"):
        assert name in INFO["commands"]


def test_full_module_coverage_present():
    # Spot-check that the Pro-only modules and the per-endpoint network/preset
    # commands all made it into the surface (guards against scope regressions).
    for name in (
        "media_add", "group_create", "event_create", "schedule_create",
        "config_preset_save", "dante_preset_create", "set_date", "set_ntp_server",
        "enc_static_ip", "enc_preset_apply", "enc_lan2_ipmode", "enc_guest_config",
        "dec_static_ip", "dec_preset_apply", "dec_hotkey",
        "net_dns", "dante_rxchn_subscribe", "wall_create",
    ):
        assert name in INFO["commands"], f"missing {name}"


# ── child_entity_types schema ──

def test_child_entity_types_declared():
    types = INFO["child_entity_types"]
    assert set(types) == {
        "encoder", "decoder", "video_wall", "group", "event",
        "schedule", "media", "dante_preset", "config_preset",
    }
    assert types["encoder"]["id_format"] == {
        "type": "integer", "min": 1, "max": 762, "pad_width": 3}
    assert types["decoder"]["id_format"]["max"] == 762
    assert types["config_preset"]["id_format"]["max"] == 10


def test_child_types_do_not_declare_reserved_props():
    for ctype, cdef in INFO["child_entity_types"].items():
        for prop in cdef.get("state_variables", {}):
            assert prop not in ("online", "label"), f"{ctype} declares {prop}"


def test_cloud_priority_tags_present():
    enc = INFO["child_entity_types"]["encoder"]["state_variables"]
    assert enc["signal_present"]["cloud_priority"] == "high"
    dec = INFO["child_entity_types"]["decoder"]["state_variables"]
    assert dec["source_video"]["cloud_priority"] == "high"
    assert dec["source_ir"]["cloud_priority"] == "low"


# ── Helpers ──

def test_norm_ip():
    assert drv._norm_ip("169.254.010.001") == "169.254.10.1"
    assert drv._norm_ip("255.255.000.000") == "255.255.0.0"
    assert drv._norm_ip("not-an-ip") == "not-an-ip"


def test_split_columns_blank_middle():
    header = "ENC     Type                EDID    IP               NET/Sig"
    data = "001                         DF000   169.254.010.001  Off/Off"
    cols = drv._split_columns(header, data)
    assert cols["ENC"] == "001"
    assert cols["Type"] == ""           # blank middle column preserved
    assert cols["EDID"] == "DF000"
    assert cols["NET/Sig"] == "Off/Off"


# ── GET STATUS parsing ──

def test_parse_status_empty():
    p = drv._parse_status(fx.BANNER_STATUS_EMPTY)
    assert p["encoders"] == {} and p["decoders"] == {}
    assert p["system"]["firmware"] == "1.10.11"
    assert p["system"]["telnet_port"] == "23"


@pytest.mark.parametrize("banner", ["BANNER_STATUS_POPULATED", "BANNER_STATUS_ONLINE"])
def test_parse_status_roster(banner):
    p = drv._parse_status(getattr(fx, banner))
    assert set(p["encoders"]) == {1}
    assert set(p["decoders"]) == {1}
    assert p["encoders"][1]["edid"] == "DF000"
    assert p["encoders"][1]["ip"] == "169.254.10.1"
    assert p["decoders"][1]["source_video"] == 1
    assert p["decoders"][1]["mode"] == "MX"
    assert p["decoders"][1]["resolution"] == "02"
    assert p["system"]["lan2_ip"] == "192.168.4.188"
    assert p["system"]["lan1_mac"] == "18:66:96:11:11:E0"
    assert p["system"]["dns_preferred"] == "192.168.4.1"
    assert p["system"]["hostname"] == "controller.local"


def test_parse_status_online_flags():
    p = drv._parse_status(fx.BANNER_STATUS_ONLINE)
    assert p["encoders"][1]["net"] is True            # "On /Off"
    assert p["encoders"][1]["signal_present"] is False
    assert p["encoders"][1]["gen"] == "TAV-CHAZY4K-TX"
    assert p["decoders"][1]["net"] is True


# ── GET ENC/DEC STATUS detail parsing ──

def test_parse_encoder_detail_offline():
    e = drv._parse_encoder_detail(fx.BANNER_ENC_DETAIL)
    assert e["edid"] == "DF000" and e["audio_input"] == "HDMI"
    assert e["multicast"] is True and e["sac"] == "ARC"
    assert e["guest_enabled"] is False and e["guest_baud"] == "9"
    assert e["ip_mode"] == "Static" and e["mac"] == "18:66:96:11:0A:27"
    assert e["ip"] == "169.254.10.1" and e["subnet_mask"] == "255.255.0.0"
    assert "arc_fix" not in e and "arc_source" not in e  # NA skipped


def test_parse_encoder_detail_online():
    e = drv._parse_encoder_detail(fx.BANNER_ENC_DETAIL_ONLINE)
    assert e["gen"] == "TAV-CHAZY4K-TX"
    assert e["net"] is True and e["firmware"] == "1.10.03"
    assert e["name"] == "Encoder 001"
    assert e["arc_fix"] == 0 and e["arc_source"] == 0
    # Pin rows: port 1 full, port 2 omits IRVOL/PHY (relay still found)
    assert e["io1_dir"] == "Out" and e["io1_relay"] == "Open" and e["io1_phy"] == "Copper"
    assert e["io2_dir"] == "Out" and e["io2_relay"] == "Open"


def test_parse_decoder_detail_online():
    d = drv._parse_decoder_detail(fx.BANNER_DEC_DETAIL_ONLINE)
    assert d["gen"] == "TAV-CHAZY4K-RX" and d["net"] is True
    assert d["mode"] == "MX" and d["resolution"] == "02" and d["rotate"] == "0"
    assert d["name"] == "Decoder 001"
    assert [d["source_video"], d["source_cec"]] == [1, 1]
    assert d["fix_video"] == 0
    assert d["multicast"] is True and d["video_output"] is True and d["video_mute"] is False
    assert d["sac"] == "ARC" and d["osp"] == 4
    assert d["mac"] == "18:66:96:11:08:C0" and d["ip"] == "169.254.20.1"


def test_parse_gpio():
    g = drv._parse_gpio(fx.BANNER_GPIO)
    assert g["gpio1_dir"] == "In" and g["gpio1_level"] == 1
    assert g["gpio4_dir"] == "In" and g["gpio4_level"] == 1


# ── Pre-existing-child enumeration parsers (group/event/wall/dante_preset) ──
#
# Validated against the byte-exact populated banners in
# fixtures/chazy_control_pro_child_banners.py. Both the ALL (list) and DETAIL
# (per-handle) captures parse to the same single-instance view.

@pytest.mark.parametrize("banner", ["BANNER_GROUP_ALL", "BANNER_GROUP_DETAIL"])
def test_parse_group_status(banner):
    g = drv._parse_group_status(getattr(fxc, banner))
    assert set(g) == {1}
    assert g[1]["name"] == "TestGroup"
    assert g[1]["member_count"] == 1


@pytest.mark.parametrize("banner", ["BANNER_EVENT_ALL", "BANNER_EVENT_DETAIL"])
def test_parse_event_status(banner):
    e = drv._parse_event_status(getattr(fxc, banner))
    assert set(e) == {1}
    assert e[1]["name"] == "TestEvent"
    assert e[1]["event_type"] == "TCP"
    assert e[1]["address"] == ""
    assert "running" not in e[1]  # no banner field for it; defaults on register


@pytest.mark.parametrize("banner", ["BANNER_WALL_ALL", "BANNER_WALL_DETAIL"])
def test_parse_wall_status(banner):
    w = drv._parse_wall_status(getattr(fxc, banner))
    assert set(w) == {1}
    assert w[1]["columns"] == 2
    assert w[1]["rows"] == 2
    assert w[1]["name"] == ""  # "NULL" sentinel normalised to empty


@pytest.mark.parametrize(
    "banner", ["BANNER_DANTE_PRESET_ALL", "BANNER_DANTE_PRESET_DETAIL"]
)
def test_parse_dante_preset_status(banner):
    p = drv._parse_dante_preset_status(getattr(fxc, banner))
    assert set(p) == {1}
    assert p[1]["name"] == "TestDP"


def test_config_child_parsers_handle_empty():
    # The "No <Type>" body an empty controller returns must parse to {}.
    for body, parser in (
        ("No Group", drv._parse_group_status),
        ("No Event", drv._parse_event_status),
        ("No Video Wall", drv._parse_wall_status),
        ("No Dante Preset", drv._parse_dante_preset_status),
    ):
        banner = f"{'=' * 64}\n              TAV-CHAZY-CLTPRO Info\n\n{body}\n{'=' * 64}"
        assert parser(banner) == {}
