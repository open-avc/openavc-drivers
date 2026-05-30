"""Unit tests for the chazy_control (standard, non-Pro) driver.

Loads ``switchers/chazy_control.py`` directly, stubbing the ``server.*``
imports it needs (BaseDriver, TCPTransport, get_logger) so the community repo's
test suite stays self-contained — mirrors test_chazy_control_pro.py.

The standard Control's command set is a documented strict subset of the Control
Pro. These tests lock that subset in: the shared encoder/decoder/video-wall/
Dante-routing/network/GPIO surface is present, and the Pro-only modules (media,
group, event, schedule, config-preset, Dante-preset, date/NTP) are absent. The
banner parsers are byte-identical to the Pro's (validated against hardware in
test_chazy_control_pro.py); their behaviour on non-Pro-identity banners is
proven by the sim round-trip in test_chazy_control_sim.py.
"""

from __future__ import annotations

import importlib.util
import logging
import string
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "switchers" / "chazy_control.py"

# Commands that exist only on the Control Pro — must NOT appear here.
PRO_ONLY_COMMANDS = {
    # Media Player
    "media_add", "media_delete", "media_addr_list", "media_addr_ping",
    "media_set_id", "media_set_name", "media_set_type", "media_set_addr_file",
    "media_set_user", "media_transparency_on", "media_transparency_off",
    "media_reload",
    # Groups
    "group_create", "group_delete", "group_set_name", "group_add_dec",
    "group_del_dec", "group_switch",
    # Events
    "event_create", "event_delete", "event_set_name", "event_set_type",
    "event_set_addr", "event_set_addr_port", "event_set_data",
    "event_set_data_hex", "event_set_params", "event_set_request",
    "event_set_resend_delay", "event_start", "event_stop",
    # Scheduler
    "schedule_create", "schedule_delete", "schedule_set_name",
    "schedule_set_color", "schedule_set_time_type", "schedule_set_week_type",
    "schedule_set_date", "schedule_set_time", "schedule_action_dec_enc",
    "schedule_action_dec_media", "schedule_action_group_enc",
    "schedule_action_group_media", "schedule_action_dante_preset",
    "schedule_action_event", "schedule_delete_action", "schedule_start",
    "schedule_stop",
    # Configuration + Dante presets
    "config_preset_save", "config_preset_delete", "config_preset_apply",
    "dante_preset_create", "dante_preset_delete", "dante_preset_set_name",
    "dante_preset_apply",
    # Date / time
    "set_date", "set_ntp_server",
}


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
drv = _load(DRIVER_PATH, "chazy_control_under_test")
INFO = drv.ChazyControlDriver.DRIVER_INFO


# ── Identity ──

def test_driver_identity():
    assert INFO["id"] == "chazy_control"
    assert INFO["manufacturer"] == "TurtleAV"
    assert INFO["transport"] == "tcp"
    assert INFO["min_platform_version"] == "0.13.0"
    assert INFO["simulated"] is True


# ── Command surface consistency (mirrors the Pro suite) ──

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


# ── Subset correctness: shared surface present, Pro-only absent ──

def test_shared_command_surface_present():
    # The encoder/decoder/video-wall/Dante-routing/network/GPIO/search surface
    # the standard Control shares with the Pro must all be present.
    for name in (
        "reboot_controller", "set_rs232_baud",
        "enc_set_name", "enc_static_ip", "enc_preset_apply", "enc_lan2_ipmode",
        "enc_guest_config", "enc_switch_arc", "enc_reset",
        "dec_route", "dec_static_ip", "dec_preset_apply", "dec_hotkey",
        "dec_reset",
        "wall_create", "wall_delete", "wall_apply_preset", "wall_preset_class",
        "dante_set_name", "dante_rxchn_subscribe", "dante_interface_static",
        "dante_search",
        "search", "add_auto_all", "add_dev_enc", "add_dev_reset",
        "gpio_dir", "gpio_level",
        "net_dhcp", "net_telnet_port", "net_hostname",
    ):
        assert name in INFO["commands"], f"missing shared command {name}"


def test_pro_only_commands_absent():
    present = set(INFO["commands"])
    leaked = present & PRO_ONLY_COMMANDS
    assert not leaked, f"Pro-only commands leaked into chazy_control: {sorted(leaked)}"
    # And none survive in the wire/lifecycle tables either.
    for name in PRO_ONLY_COMMANDS:
        assert name not in drv._COMMAND_TEMPLATES, f"{name} in templates"
        assert name not in drv._LIFECYCLE_COMMANDS, f"{name} in lifecycle"


# ── child_entity_types schema (subset of the Pro's nine) ──

def test_child_entity_types_are_the_subset():
    types = INFO["child_entity_types"]
    assert set(types) == {"encoder", "decoder", "video_wall"}
    assert types["encoder"]["id_format"] == {
        "type": "integer", "min": 1, "max": 762, "pad_width": 3}
    assert types["decoder"]["id_format"]["max"] == 762
    assert types["video_wall"]["id_format"]["max"] == 9  # FW 1.00.17 §7 hdl [01..09]


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


def test_lifecycle_only_enc_dec_wall():
    # No group/event/schedule/media/dante_preset/config_preset lifecycle.
    assert set(drv._LIFECYCLE_COMMANDS) == {
        "enc_delete", "enc_set_id", "dec_delete", "dec_set_id",
        "wall_create", "wall_delete",
    }
    for spec in drv._LIFECYCLE_COMMANDS.values():
        assert spec["child_type"] in {"encoder", "decoder", "video_wall"}


# ── System state-variables: Date/NTP module dropped, Network kept ──

def test_state_vars_drop_date_ntp_keep_network():
    sv = INFO["state_variables"]
    assert "date" not in sv and "ntp_server" not in sv
    # DNS is part of the Network module, which the standard Control keeps.
    for name in ("dns_mode", "dns_preferred", "dns_alternate",
                 "lan1_ip", "lan2_ip", "hostname", "gpio1_dir"):
        assert name in sv, f"network/system var {name} missing"


# ── Helpers (shared parser primitives) ──

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


# ── Video-wall enumeration (the standard Control's one queryable config type) ──
#
# The parser keys off the VW Col Row CfgSel Name row, so it's identity-agnostic
# (same on the standard Control and the Pro). No standalone Control hardware
# exists, so the banner here is the shared family layout with CHAZY CONTROL
# identity rather than a hardware fixture.

def test_parse_wall_status():
    banner = (
        "================================================================\n"
        "              CHAZY CONTROL Video Wall Info\n"
        "              FW Version: 1.00.17\n"
        "\n"
        "VW  Col    Row    CfgSel  Name\n"
        "01  02     02     01      NULL\n"
        "    OutID\n"
        "    --- --- --- ---\n"
        "    Cfg    Name\n"
        "    01     Preset 1\n"
        "           Class  From    Screen\n"
        "           A      001     H01V01 H02V01 H01V02 H02V02\n"
        "================================================================"
    )
    w = drv._parse_wall_status(banner)
    assert w == {1: {"name": "", "columns": 2, "rows": 2}}


def test_parse_wall_status_empty():
    banner = (
        "================================================================\n"
        "              CHAZY CONTROL Video Wall Info\n"
        "\n"
        "No Video Wall\n"
        "================================================================"
    )
    assert drv._parse_wall_status(banner) == {}
