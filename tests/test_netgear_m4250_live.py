"""Replay byte-exact M4250 CLI captures through the driver's parsers.

These fixtures (``tests/fixtures/netgear_m4250/``) are verbatim output from a
real NETGEAR M4250-40G8XF-PoE+ (software 13.0.5.14), captured 2026-06-07 over
telnet at factory defaults and again with a live PoE device (a Dante endpoint)
on port ``0/1``. They complement the synthetic M4350 fixtures in
``netgear_m4250_m4350_outputs.py``: the synthetic set proves the parsers against
the CLI-manual layout, this set proves them against what the hardware actually
emits — memory reported in KBytes, a ``Bootcode Version`` label, an
``IGMP Snooping Querier Mode`` label, the trailing ``lag``/``vlan``
pseudo-interfaces, and real multicast group tables.

Loaded with the same ``openavc.*`` stubs as ``test_netgear_m4250_m4350.py`` so
the community repo's suite stays self-contained (no ``openavc`` install).
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "utility" / "netgear_m4250_m4350.py"
FIX = REPO_ROOT / "tests" / "fixtures" / "netgear_m4250"


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

    class _BaseDriver:  # minimal stand-in; we only exercise module-level parsers
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
drv = _load(DRIVER_PATH, "netgear_m4250_m4350_live_under_test")

# Trailing CLI prompt, e.g. "\n(M4250-40G8XF-PoE+)#".
_PROMPT = re.compile(r"\n\([^()\n]*\)\s*[#>]\s*$")


def _body(rel: str) -> str:
    """Output body of a single-command capture.

    Captures are written as ``<echoed command>\\n\\n<output>\\n\\n<prompt>``; the
    driver's framing hands its parsers only the ``<output>`` (command echo and
    the trailing prompt removed), so reproduce that here.
    """
    text = (FIX / rel).read_text(encoding="utf-8")
    lines = text.split("\n")
    if lines and lines[0].strip():        # drop the echoed command line
        lines = lines[1:]
    body = "\n".join(lines).rstrip()
    return _PROMPT.sub("", body).strip("\n")


def _section(rel: str, cmd: str) -> str:
    """Output body of one command inside a ``===== CMD: <cmd> =====`` transcript."""
    text = (FIX / rel).read_text(encoding="utf-8")
    marker = f"===== CMD: {cmd} ====="
    start = text.index(marker) + len(marker)
    rest = text[start:]
    nxt = rest.find("===== CMD:")
    chunk = (rest if nxt == -1 else rest[:nxt])
    lines = chunk.split("\n")
    while lines and not lines[0].strip():   # leading blank line(s)
        lines.pop(0)
    if lines and lines[0].strip() == cmd:   # echoed command line
        lines.pop(0)
    body = "\n".join(lines).rstrip()
    return _PROMPT.sub("", body).strip("\n")


# A physical interface key is "slot/port" (M4250) or "unit/slot/port" (M4350);
# the lag/vlan pseudo-interfaces the switch appends must never appear here.
_PHYS_RE = re.compile(r"^\d+(?:/\d+){1,2}$")


def _all_physical(keys) -> bool:
    return all(_PHYS_RE.match(k) for k in keys)


# ── factory baseline: identity / health ──

def test_version_real_m4250():
    out = drv.parse_version(_body("factory/show-version.txt"))
    assert out["model"] == "M4250-40G8XF-PoE+"
    assert re.search(r"M4250", out["model"]) and "M4350" not in out["model"]
    assert out["serial_number"] == "7FH85A5BA029A"
    assert out["firmware_version"] == "13.0.5.14"
    assert out["mac_address"] == "28:94:01:7F:D8:F4"
    # The M4250 labels this "Bootcode Version" (one word), not "Boot Code Version".
    assert out["boot_version"] == "1.0.0.13"


def test_environment_real():
    out = drv.parse_environment(_body("factory/show-environment.txt"))
    assert out["temperature_c"] == 41
    assert out["temperature_state"] == "Normal"
    assert out["fan_status"] == "OK"
    assert out["psu_status"] == "OK"


def test_process_cpu_reports_kbytes_not_bytes():
    # The M4250 memory table header is "status     KBytes" — values are already
    # KB, so the driver must NOT divide by 1024 a second time.
    out = drv.parse_process_cpu(_body("factory/show-process-cpu.txt"))
    assert out["mem_free_kb"] == 974592
    assert out["mem_alloc_kb"] == 1015244
    assert out["cpu_util_5s"] == 1.63
    assert out["cpu_util_60s"] == 2.26
    assert out["cpu_util_300s"] == 2.40
    assert 0 < out["mem_used_percent"] < 100


# ── factory baseline: PoE ──

def test_poe_global_off_factory():
    out = drv.parse_poe_global(_body("factory/show-poe.txt"))
    assert out["poe_status"] == "OFF"
    assert out["poe_total_power_w"] == 960.0
    assert out["poe_consumed_power_w"] == 0.0
    assert out["poe_threshold_power_w"] == 864.0
    assert out["poe_usage_threshold"] == 90
    assert out["poe_power_mgmt_mode"] == "Dynamic"


def test_poe_port_info_all_factory():
    out = drv.parse_poe_port_info(_body("factory/show-poe-port-info-all.txt"))
    assert len(out) == 40                      # 0/1..0/40 PoE copper
    assert "0/41" not in out                   # SFP+ ports absent from PoE table
    assert out["0/1"]["poe_capable"] is True
    assert out["0/1"]["poe_status"] == "Searching"
    assert out["0/1"]["poe_max_power_w"] == 32.0
    assert out["0/1"]["poe_power_w"] == 0.0


def test_poe_port_config_all_factory():
    out = drv.parse_poe_port_config(
        _body("factory/show-poe-port-configuration-all.txt"))
    assert len(out) == 40
    assert out["0/1"]["poe_admin"] == "enabled"
    assert out["0/1"]["poe_priority"] == "low"
    assert out["0/40"]["poe_admin"] == "enabled"


# ── factory baseline: port tables skip lag/vlan pseudo-interfaces (bug #2) ──

def test_port_table_skips_lag_and_vlan():
    out = drv.parse_port_table(_body("factory/show-port-all.txt"))
    assert _all_physical(out)                  # no "lag 1"/"vlan 1" phantom ports
    assert "lag 1" not in out and "vlan 1" not in out
    assert len(out) == 48                       # 0/1..0/48 (40 copper + 8 SFP+)
    assert out["0/37"]["link_status"] == "up"
    assert out["0/37"]["speed"] == "1000 Full"
    assert out["0/1"]["link_status"] == "down"
    assert out["0/1"]["admin_status"] == "enabled"
    assert out["0/41"]["admin_status"] == "enabled"   # SFP+ present


def test_interfaces_status_skips_lag_and_vlan():
    out = drv.parse_interfaces_status(
        _body("factory/show-interfaces-status-all.txt"))
    assert _all_physical(out)
    assert "lag 1" not in out and "vlan 1" not in out
    assert len(out) == 48
    assert out["0/37"]["media_type"] == "Copper"
    assert out["0/37"]["vlan"] == "1"
    assert out["0/1"]["description"] == ""      # blank Name at factory


# ── factory baseline: IGMP / VLAN ──

def test_igmp_snooping_enabled_real():
    out = drv.parse_igmp_snooping(_body("factory/show-igmpsnooping.txt"))
    assert out["igmp_snooping"] is True


def test_igmp_querier_mode_label_real():
    # The M4250 prints "IGMP Snooping Querier Mode", not the manual's
    # "Admin Mode"; the driver must still read the mode and the address.
    out = drv.parse_igmp_querier(_body("factory/show-igmpsnooping-querier.txt"))
    assert out["igmp_querier"] is True
    assert out["igmp_querier_address"] == "169.254.100.100"


def test_vlan_count_real():
    assert drv.parse_vlan_count(_body("factory/show-vlan.txt")) == 1


# ── with a live PoE device on 0/1 ──

def test_poe_global_on_with_device():
    out = drv.parse_poe_global(_body("with-poe/show-poe.txt"))
    assert out["poe_status"] == "ON"
    assert out["poe_consumed_power_w"] == 6.7
    assert out["poe_total_power_w"] == 960.0


def test_poe_port_info_live_draw():
    out = drv.parse_poe_port_info(_body("with-poe/show-poe-port-info-all.txt"))
    p1 = out["0/1"]
    assert p1["poe_status"] == "Delivering Power"
    assert p1["poe_power_w"] == 6.7
    assert p1["poe_class"] == "4"
    assert p1["poe_current_ma"] == 123.0
    assert p1["poe_voltage_v"] == 55.0
    assert out["0/2"]["poe_status"] == "Searching"


def test_igmp_groups_real_dante_multicast():
    rows = drv.parse_igmp_groups(_body("with-poe/show-igmpsnooping-group.txt"))
    assert len(rows) == 11
    groups = {r["group"] for r in rows}
    assert "225.1.0.0/01:00:5E:01:00:00" in groups
    assert len(groups) == 11                    # every subscription is distinct
    ifaces = {r["interface"] for r in rows}
    assert ifaces == {"0/1", "0/37"}
    # The Dante endpoint on 0/1 drives the bulk of the subscriptions.
    assert sum(1 for r in rows if r["interface"] == "0/1") == 10


def test_lldp_remote_all_empty_neighbors():
    # Every port is listed but no neighbor reported (LLDP empty in the lab);
    # the parser must drop the empty rows rather than invent neighbors.
    out = drv.parse_lldp_remote(_body("with-poe/show-lldp-remote-device-all.txt"))
    assert out == {}


# ── command transcripts ──

def test_cablestatus_live_and_no_cable():
    live = drv.parse_cablestatus(
        _section("commands/diag-tests.txt", "cablestatus 0/1"))
    assert live["cable_status"] == "Normal"
    assert live["cable_length"] == "0m - 18m"   # range form, captured (item #3)

    empty = drv.parse_cablestatus(
        _section("commands/diag-tests.txt", "cablestatus 0/2"))
    assert empty["cable_status"] == "No Cable"
    assert "cable_length" not in empty          # no length when no cable


def test_interfaces_status_description_readback_real():
    # Real output after setting a quoted multi-word description: the Name reads
    # back unquoted, and the appended lag/vlan rows are still filtered out.
    out = drv.parse_interfaces_status(
        _section("commands/description-quoting.txt", "show interfaces status all"))
    assert out["0/1"]["description"] == "OpenAVC Test Port"
    assert _all_physical(out)
    assert "lag 1" not in out and "vlan 1" not in out
