"""Unit tests for the netgear_m4250_m4350 driver.

Loads ``utility/netgear_m4250_m4350.py`` directly, stubbing the ``openavc.*``
imports it needs so the community repo's test suite stays self-contained (no
``openavc`` install). Exercises the CLI output parsers against the sample
outputs in fixtures/, plus command-surface consistency and the interface-id
helpers. Full poll/command/connect behavior is covered by the simulator
integration test.
"""

from __future__ import annotations

import importlib.util
import logging
import string
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "utility" / "netgear_m4250_m4350.py"
FIXTURES_PATH = REPO_ROOT / "tests" / "fixtures" / "netgear_m4250_m4350_outputs.py"


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

    class _BaseDriver:  # minimal stand-in; we only test module-level code
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
    logger.get_logger = lambda name="netgear": logging.getLogger(name)
    sys.modules["openavc.utils.logger"] = logger


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_server_stubs()
drv = _load(DRIVER_PATH, "netgear_m4250_m4350_under_test")
fx = _load(FIXTURES_PATH, "netgear_fixtures_under_test")
INFO = drv.NetgearM4250M4350Driver.DRIVER_INFO


# ── interface id helpers ──

def test_iface_to_id_three_part():
    assert drv._iface_to_id("1/0/2") == 10002
    assert drv._iface_to_id("2/0/48") == 20048
    assert drv._iface_to_id("1/0/1") == 10001


def test_iface_to_id_two_part():
    assert drv._iface_to_id("0/1") == 1
    assert drv._iface_to_id("0/24") == 24


def test_iface_to_id_rejects_non_physical():
    assert drv._iface_to_id("lag 3") is None
    assert drv._iface_to_id("vlan 10") is None
    assert drv._iface_to_id("") is None


def test_iface_ids_are_unique_and_stable():
    ifaces = ["1/0/1", "1/0/2", "2/0/1", "1/0/48"]
    ids = [drv._iface_to_id(i) for i in ifaces]
    assert len(set(ids)) == len(ids)


# ── system / health parsers ──

def test_parse_version():
    out = drv.parse_version(fx.SHOW_VERSION)
    assert out["model"] == "M4350-24X4F"
    assert out["serial_number"] == "7AB12C3D4E5F6"
    assert out["firmware_version"] == "14.0.6.17"
    assert out["mac_address"] == "BC:A5:11:22:33:44"
    assert out["system_name"] == "AV-CORE-SW1"
    assert "12 days" in out["uptime"]


def test_parse_environment():
    out = drv.parse_environment(fx.SHOW_ENVIRONMENT)
    assert out["temperature_c"] == 41
    assert out["temperature_state"] == "Normal"
    assert out["fan_status"] == "OK"
    assert out["psu_status"] == "OK"


def test_parse_environment_flags_failure():
    text = fx.SHOW_ENVIRONMENT.replace("FAN-2          Fixed 3100  28%        Operational",
                                       "FAN-2          Fixed 0     0%         Failed")
    out = drv.parse_environment(text)
    assert "not OK" in out["fan_status"]


def test_parse_process_cpu():
    out = drv.parse_process_cpu(fx.SHOW_PROCESS_CPU)
    assert out["cpu_util_5s"] == 3.20
    assert out["cpu_util_60s"] == 2.80
    assert out["cpu_util_300s"] == 2.50
    assert out["mem_free_kb"] == 268435456 // 1024
    assert 0 < out["mem_used_percent"] < 100


# ── PoE parsers ──

def test_parse_poe_global():
    out = drv.parse_poe_global(fx.SHOW_POE)
    assert out["poe_status"] == "ON"
    assert out["poe_total_power_w"] == 720.0
    assert out["poe_consumed_power_w"] == 62.5
    assert out["poe_threshold_power_w"] == 648.0
    assert out["poe_usage_threshold"] == 90
    assert out["poe_power_mgmt_mode"] == "Dynamic"


def test_parse_poe_port_info():
    out = drv.parse_poe_port_info(fx.SHOW_POE_PORT_INFO)
    p2 = out["1/0/2"]
    assert p2["poe_capable"] is True
    assert p2["poe_status"] == "Delivering Power"
    assert p2["poe_power_w"] == 4.0
    assert p2["poe_class"] == "4"
    assert p2["poe_voltage_v"] == 54.0
    assert p2["poe_current_ma"] == 74.0
    assert p2["poe_max_power_w"] == 32.0
    assert out["1/0/1"]["poe_status"] == "Searching"
    assert out["1/0/48"]["poe_status"] == "Disabled"


def test_parse_poe_port_config():
    out = drv.parse_poe_port_config(fx.SHOW_POE_PORT_CONFIG)
    assert out["1/0/2"]["poe_admin"] == "enabled"
    assert out["1/0/2"]["poe_priority"] == "high"
    assert out["1/0/48"]["poe_admin"] == "disabled"


# ── port parsers ──

def test_parse_port_table():
    out = drv.parse_port_table(fx.SHOW_PORT_ALL)
    assert out["1/0/1"]["link_status"] == "up"
    assert out["1/0/1"]["speed"] == "1000 Full"
    assert out["1/0/1"]["admin_status"] == "enabled"
    assert out["1/0/3"]["link_status"] == "down"
    assert out["1/0/3"]["speed"] == ""  # blank physical status when down
    assert out["1/0/48"]["admin_status"] == "disabled"
    assert out["1/0/49"]["speed"] == "10G Full"


def test_parse_interfaces_status():
    out = drv.parse_interfaces_status(fx.SHOW_INTERFACES_STATUS)
    assert out["1/0/1"]["description"] == "Camera 1"
    assert out["1/0/1"]["media_type"] == "RJ45"
    assert out["1/0/1"]["vlan"] == "10"
    assert out["1/0/2"]["description"] == "Display Left"


# ── multicast / IGMP parsers ──

def test_parse_igmp_snooping():
    assert drv.parse_igmp_snooping(fx.SHOW_IGMPSNOOPING)["igmp_snooping"] is True


def test_parse_igmp_querier():
    out = drv.parse_igmp_querier(fx.SHOW_IGMPSNOOPING_QUERIER)
    assert out["igmp_querier"] is True
    assert out["igmp_querier_address"] == "10.20.0.1"


def test_parse_igmp_groups():
    rows = drv.parse_igmp_groups(fx.SHOW_IGMPSNOOPING_GROUP)
    assert len(rows) == 2
    groups = {r["group"] for r in rows}
    assert "224.1.1.6/01:00:5E:01:01:06" in groups
    ifaces = {r["interface"] for r in rows}
    assert "1/0/2" in ifaces and "1/0/16" in ifaces


# ── neighbors / vlan / power / cable / rgb ──

def test_parse_lldp_remote():
    out = drv.parse_lldp_remote(fx.SHOW_LLDP_REMOTE)
    assert out["1/0/2"]["lldp_system_name"] == "Display-Left"
    assert out["1/0/2"]["lldp_chassis_id"] == "B8:27:EB:11:22:33"


def test_parse_vlan_count():
    assert drv.parse_vlan_count(fx.SHOW_VLAN) == 4


def test_parse_power_redundancy():
    assert drv.parse_power_redundancy(fx.SHOW_POWER_REDUNDANCY)["psu_redundancy"] == "Active"


def test_parse_cablestatus():
    out = drv.parse_cablestatus(fx.CABLESTATUS)
    assert out["cable_status"] == "Normal"
    assert "20m" in out["cable_length"]


def test_parse_rgb_led():
    out = drv.parse_rgb_led(fx.SHOW_RGB_LED)
    assert out["1/0/2"]["av_profile"] == "NDI"


# ── command surface consistency ──

_SPECIAL = {"reboot", "save_config", "poe_reset_all", "cable_test",
            "clear_port_counters"}


def test_every_command_has_a_handler():
    for name in INFO["commands"]:
        assert name in _SPECIAL or name in drv._PORT_COMMANDS, \
            f"{name} has no handler"


def test_no_orphan_port_commands():
    for name in drv._PORT_COMMANDS:
        assert name in INFO["commands"], f"port command {name} not declared"


def test_port_command_templates_have_params():
    fmt = string.Formatter()
    for name, spec in drv._PORT_COMMANDS.items():
        params = set(INFO["commands"][name].get("params", {}))
        for line in spec["lines"]:
            placeholders = {f for _, f, _, _ in fmt.parse(line) if f}
            assert placeholders <= params, f"{name}: {placeholders - params}"


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


def test_driver_info_essentials():
    assert INFO["id"] == "netgear_m4250_m4350"
    assert INFO["transport"] == "ssh"
    assert INFO["category"] == "utility"
    assert "port" in INFO["child_entity_types"]
    assert INFO["child_entity_types"]["port"]["label_field"] == "interface"


def test_discovery_hints_declared():
    disc = INFO["discovery"]
    # The validated base-MAC OUI block from the real unit.
    assert "28:94:01" in disc["oui"]
    # SSDP/UPnP rootDesc reports manufacturer NETGEAR; the platform mines that
    # into this alias, so the host surfaces as a possible match.
    assert "netgear" in disc["manufacturer_alias"]
    # The generic UPnP device type must NOT be declared as an ssdp fingerprint.
    assert "ssdp" not in disc
