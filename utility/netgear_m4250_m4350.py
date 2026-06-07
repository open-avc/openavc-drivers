"""
OpenAVC NETGEAR M4250 / M4350 AV Line switch driver.

One driver for both the M4250 and M4350 "AV Line" managed switch families.
They share a single CLI (one manual covers both); the driver detects the family
from ``show version`` and adapts the two things that differ: interface naming
(M4250 ``slot/port`` vs M4350 ``unit/slot/port``) and a couple of PoE/power
commands. Each physical port is modelled as an OpenAVC *child entity* of the
switch (state keyed ``device.<id>.port.<padded_id>.<prop>``), so PoE draw, link
state, and the connected device for every port are visible and controllable
through one OpenAVC device.

Why this matters for AV: the headline features are PoE troubleshooting
(power-cycle a frozen camera/display/AP with ``poe reset``), per-port PoE draw /
class / status, multicast/IGMP health (the usual cause of "no video" on
AV-over-IP), link state, system health (temperature/fans/PSU/CPU), reboot, and
config save.

Transport / protocol:

* The CLI is reached over **SSH** in production (``transport: ssh``) using the
  platform SSH transport, which shells out to the OS OpenSSH client. For the
  device simulator (and locked-down lab networks) the same CLI is reached over
  raw **telnet/TCP** (``transport: tcp``); the framing below is identical either
  way because both transports are raw byte pipes.
* The session is strictly request/response. With a forced PTY the switch echoes
  each command, prints the response, then re-prints its prompt
  (``(Name) #`` privileged, ``(Name) >`` user, ``(Name) (Interface 1/0/2)#`` in
  interface config). The prompt is the end-of-response marker. Interactive
  sub-prompts (``Password:`` for enable, ``(y/n)`` for reload) are recognised so
  the driver can answer them.
* Output paging is disabled per session with ``terminal length 0`` (M4350); the
  ``--More-- or (q)uit`` pager is also auto-answered as a fallback (M4250).

License: MIT.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from server.drivers.base import BaseDriver
from server.transport.tcp import TCPTransport  # noqa: F401  (telnet/sim path; base builds it)
from server.utils.logger import get_logger

log = get_logger(__name__)

# Strip ANSI/VT escape sequences a PTY session can emit.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
# CLI prompt: "(name) #", "(name) >", "(name) (Config)#", "(name) (Interface 1/0/2)#".
_PROMPT_RE = re.compile(r"\([^()\r\n]*\)\s*(?:\([^()\r\n]*\)\s*)?[#>]\s*$")
# Interactive sub-prompts the device pauses on awaiting input.
_PASSWORD_RE = re.compile(r"[Pp]assword:\s*$")
_CONFIRM_RE = re.compile(r"\((?:y/n|Y/N|yes/no|Yes/No)\)[\s.?]*$|continue\?\s*$")
# Pager prompt (fallback when terminal length 0 is unavailable).
_PAGER_RE = re.compile(r"--More--[^\n]*$")
_PAGER_STRIP_RE = re.compile(r"--More-- or \(q\)uit[^\n]*")

PORT_MAX = 99999  # encoded id ceiling (unit*10000 + slot*1000 + port)


# ────────────────────────── interface id helpers ──────────────────────────

def _iface_parts(iface: str) -> tuple[int, int, int] | None:
    """Parse 'slot/port' or 'unit/slot/port' into (unit, slot, port) ints.

    M4250 uses two parts (unit assumed 0); M4350 uses three. Returns None for
    anything that isn't a physical interface (LAGs, vlan routing ifaces, blanks).
    """
    parts = iface.strip().split("/")
    if not all(p.strip().isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts]
    if len(nums) == 2:
        return (0, nums[0], nums[1])
    if len(nums) == 3:
        return (nums[0], nums[1], nums[2])
    return None


def _iface_to_id(iface: str) -> int | None:
    """Deterministic integer child id for a physical interface, so a port keeps
    the same id across polls (and across M4350 100G breakout reconfigurations).
    """
    parts = _iface_parts(iface)
    if parts is None:
        return None
    unit, slot, port = parts
    return unit * 10000 + slot * 1000 + port


def _iface_sort_key(iface: str) -> tuple[int, int, int]:
    return _iface_parts(iface) or (0, 0, 0)


# ────────────────────────── generic parse helpers ──────────────────────────

def _clean(text: str) -> str:
    """Drop ANSI escapes and pager tokens from a response body."""
    text = _ANSI_RE.sub("", text)
    text = _PAGER_STRIP_RE.sub("", text)
    return text


def _kv(text: str) -> dict[str, str]:
    """Parse 'Label........value' dotted key/value lines into {label: value}."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(.+?)\s*\.{2,}\s*(.*?)\s*$", line)
        if m:
            # Some outputs label with a trailing colon ("N+1 Active: ...");
            # normalise it away so lookups are consistent.
            out[m.group(1).strip().rstrip(":").strip()] = m.group(2).strip()
    return out


def _column_spans(separator: str) -> list[tuple[int, int | None]]:
    """Column (start, end) spans from a '----- ------ ---' separator line.

    Each maximal run of dashes is one column; the last column runs to EOL.
    Robust to empty cells (a down port's blank speed) because cells are sliced
    by fixed positions, not whitespace-split.
    """
    starts = [m.start() for m in re.finditer(r"-+", separator)]
    spans: list[tuple[int, int | None]] = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else None
        spans.append((s, e))
    return spans


def _table(text: str, fields: list[str]) -> list[dict[str, str]]:
    """Parse an aligned column table into a list of row dicts.

    Finds the dashed separator line, derives column spans from it, and slices
    each following non-empty data line into ``fields`` (positional). Header
    lines above the separator are ignored — the caller supplies field names.
    """
    lines = text.splitlines()
    sep_idx = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and set(stripped) <= {"-", " "} and "---" in stripped:
            sep_idx = i
            break
    if sep_idx == -1:
        return []
    spans = _column_spans(lines[sep_idx])
    rows: list[dict[str, str]] = []
    for line in lines[sep_idx + 1:]:
        if not line.strip():
            continue
        if _PROMPT_RE.search(line):
            break
        cells = [line[s:e].strip() if e is not None else line[s:].strip()
                 for s, e in spans]
        if not cells or not cells[0]:
            continue
        row = {fields[i]: (cells[i] if i < len(cells) else "")
               for i in range(len(fields))}
        rows.append(row)
    return rows


def _to_int(value: str, default: int = 0) -> int:
    m = re.search(r"-?\d+", value or "")
    return int(m.group()) if m else default


def _to_float(value: str, default: float = 0.0) -> float:
    m = re.search(r"-?\d+(?:\.\d+)?", value or "")
    return float(m.group()) if m else default


def _is_enabled(value: str) -> bool:
    return value.strip().lower() in ("enable", "enabled", "yes", "up", "on",
                                     "active", "true")


# ────────────────────────── command output parsers ─────────────────────────

def parse_version(text: str) -> dict[str, Any]:
    """show version / show hardware -> identity fields."""
    kv = _kv(text)
    out: dict[str, Any] = {}
    if "Machine Model" in kv:
        out["model"] = kv["Machine Model"]
    elif "System Description" in kv:
        out["model"] = kv["System Description"].split(",")[0].strip()
    if "Serial Number" in kv:
        out["serial_number"] = kv["Serial Number"]
    if "Software Version" in kv:
        out["firmware_version"] = kv["Software Version"]
    # Label differs by family/firmware: the CLI manual and M4350 print
    # "Boot Code Version"; the M4250 prints "Bootcode Version".
    for boot_key in ("Boot Code Version", "Bootcode Version"):
        if boot_key in kv:
            out["boot_version"] = kv[boot_key]
            break
    for mac_key in ("Burned In MAC Address", "Burned in MAC Address"):
        if mac_key in kv:
            out["mac_address"] = kv[mac_key]
    for up_key in ("System Up Time", "Up Time", "System Uptime"):
        if up_key in kv:
            out["uptime"] = kv[up_key]
    if "System Name" in kv:
        out["system_name"] = kv["System Name"]
    return out


def _between(text: str, start: str, ends: list[str]) -> str:
    """Slice the text from just after ``start`` to the first of ``ends``."""
    i = text.find(start)
    if i == -1:
        return ""
    i += len(start)
    j = len(text)
    for e in ends:
        k = text.find(e, i)
        if k != -1:
            j = min(j, k)
    return text[i:j]


def _aggregate_state(states: list[str], ok_values: tuple[str, ...]) -> str:
    bad = [s for s in states if s.lower() not in ok_values]
    return "OK" if not bad else f"{len(bad)} of {len(states)} not OK"


def parse_environment(text: str) -> dict[str, Any]:
    """show environment -> temperature, fan, and PSU health.

    Each sub-table (sensors, fans, power modules) is sliced out by its section
    heading before parsing, so the shared ``_table`` separator-finder doesn't
    re-read the first table for all three.
    """
    out: dict[str, Any] = {}
    kv = _kv(text)
    if "Temp (C)" in kv:
        out["temperature_c"] = _to_int(kv["Temp (C)"])

    temp_sec = _between(text, "Temperature Sensors", ["Fans", "Power Modules"])
    temp_rows = _table(temp_sec, ["unit", "sensor", "description", "temp",
                                  "state", "max_temp"])
    if temp_rows:
        if "temperature_c" not in out:
            out["temperature_c"] = _to_int(temp_rows[0].get("temp", "0"))
        states = [r.get("state", "").strip() for r in temp_rows
                  if r.get("state", "").strip()]
        if states:
            out["temperature_state"] = (
                "Normal" if all(s.lower() == "normal" for s in states)
                else next((s for s in states if s.lower() != "normal"), "Normal")
            )

    fan_sec = _between(text, "Fans", ["Power Modules"])
    fan_rows = _table(fan_sec, ["unit", "fan", "description", "type", "speed",
                                "duty", "state"])
    fan_states = [r.get("state", "").strip() for r in fan_rows
                  if r.get("state", "").strip()]
    if fan_states:
        out["fan_status"] = _aggregate_state(fan_states, ("operational", "ok"))

    psu_sec = _between(text, "Power Modules", [])
    psu_states = re.findall(r"\b(Operational|Failed|Not Present|Powered)\b", psu_sec)
    if psu_states:
        out["psu_status"] = _aggregate_state(psu_states, ("operational",))
    return out


def parse_process_cpu(text: str) -> dict[str, Any]:
    """show process cpu -> total CPU utilisation (5/60/300s) and memory."""
    out: dict[str, Any] = {}
    # The memory table reports raw bytes on some firmware (the CLI manual's
    # "status     bytes") but already-KB values on the M4250
    # ("status     KBytes"). Honour the column-header unit so we don't divide
    # KB by 1024 a second time and report memory 1024x too small.
    unit_m = re.search(r"status\s+(\w+)", text, re.I)
    in_kb = bool(unit_m and unit_m.group(1).lower().startswith("kb"))

    def _kb(raw: int) -> int:
        return raw if in_kb else raw // 1024

    free = re.search(r"free\s+(\d+)", text)
    alloc = re.search(r"alloc\s+(\d+)", text)
    if free:
        out["mem_free_kb"] = _kb(int(free.group(1)))
    if alloc:
        out["mem_alloc_kb"] = _kb(int(alloc.group(1)))
    if free and alloc:
        f, a = int(free.group(1)), int(alloc.group(1))
        total = f + a
        if total:
            out["mem_used_percent"] = round(a / total * 100, 1)
    m = re.search(
        r"Total CPU Utilization\s+([\d.]+)%\s+([\d.]+)%\s+([\d.]+)%", text)
    if m:
        out["cpu_util_5s"] = float(m.group(1))
        out["cpu_util_60s"] = float(m.group(2))
        out["cpu_util_300s"] = float(m.group(3))
    return out


def parse_poe_global(text: str) -> dict[str, Any]:
    """show poe -> global PoE controller status and power budget."""
    kv = _kv(text)
    out: dict[str, Any] = {}
    if "PSE Main Operational Status" in kv:
        out["poe_status"] = kv["PSE Main Operational Status"]
    if "Total Power Available" in kv:
        out["poe_total_power_w"] = _to_float(kv["Total Power Available"])
    if "Threshold Power" in kv:
        out["poe_threshold_power_w"] = _to_float(kv["Threshold Power"])
    if "Total Power Consumed" in kv:
        out["poe_consumed_power_w"] = _to_float(kv["Total Power Consumed"])
    if "Usage Threshold" in kv:
        out["poe_usage_threshold"] = _to_int(kv["Usage Threshold"])
    if "Power Management Mode" in kv:
        out["poe_power_mgmt_mode"] = kv["Power Management Mode"]
    return out


def parse_poe_port_info(text: str) -> dict[str, dict[str, Any]]:
    """show poe port info all -> per-interface live PoE state (mW -> W)."""
    rows = _table(text, ["intf", "high_power", "max_power", "poe_class",
                         "power", "current", "voltage", "poe_status", "fault"])
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        iface = r["intf"]
        out[iface] = {
            "poe_capable": True,
            "poe_max_power_w": round(_to_int(r["max_power"]) / 1000, 1),
            "poe_class": r["poe_class"],
            "poe_power_w": round(_to_int(r["power"]) / 1000, 2),
            "poe_current_ma": _to_float(r["current"]),
            "poe_voltage_v": _to_float(r["voltage"]),
            "poe_status": r["poe_status"],
            "poe_fault": r["fault"],
        }
    return out


def parse_poe_port_config(text: str) -> dict[str, dict[str, Any]]:
    """show poe port configuration all -> per-interface PoE admin/priority."""
    rows = _table(text, ["intf", "admin", "priority", "limit", "limit_type",
                         "high_power", "detection", "timer"])
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        out[r["intf"]] = {
            "poe_admin": "enabled" if _is_enabled(r["admin"]) else "disabled",
            "poe_priority": r["priority"].lower(),
        }
    return out


def parse_port_table(text: str) -> dict[str, dict[str, Any]]:
    """show port all -> per-interface admin/link/speed."""
    rows = _table(text, ["intf", "type", "admin", "phys_mode", "phys_status",
                         "link", "link_trap", "lacp", "actor_timeout"])
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if _iface_to_id(r["intf"]) is None:
            continue
        out[r["intf"]] = {
            "admin_status": "enabled" if _is_enabled(r["admin"]) else "disabled",
            "link_status": "up" if r["link"].strip().lower() == "up" else "down",
            "speed": r["phys_status"].strip(),
        }
    return out


def parse_interfaces_status(text: str) -> dict[str, dict[str, Any]]:
    """show interfaces status all -> description, media type, VLAN per port."""
    rows = _table(text, ["intf", "name", "link", "phys_mode", "phys_status",
                         "media", "flow", "vlan"])
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if _iface_to_id(r["intf"]) is None:
            continue
        out[r["intf"]] = {
            "description": r.get("name", "").strip(),
            "media_type": r.get("media", "").strip(),
            "vlan": r.get("vlan", "").strip(),
        }
    return out


def parse_igmp_snooping(text: str) -> dict[str, Any]:
    """show igmpsnooping -> global admin mode."""
    kv = _kv(text)
    out: dict[str, Any] = {}
    if "Admin Mode" in kv:
        out["igmp_snooping"] = _is_enabled(kv["Admin Mode"])
    return out


def parse_igmp_querier(text: str) -> dict[str, Any]:
    """show igmpsnooping querier -> querier admin mode + address."""
    kv = _kv(text)
    out: dict[str, Any] = {}
    # The mode label differs by family/firmware: the CLI manual uses
    # "Admin Mode"; the M4250 prints "IGMP Snooping Querier Mode".
    for mode_key in ("Admin Mode", "IGMP Snooping Querier Mode",
                     "Querier Admin Mode"):
        if mode_key in kv:
            out["igmp_querier"] = _is_enabled(kv[mode_key])
            break
    for key in ("Querier Address", "Snooping Querier Address"):
        if key in kv and kv[key]:
            out["igmp_querier_address"] = kv[key]
    return out


def parse_igmp_groups(text: str) -> list[dict[str, str]]:
    """show igmpsnooping group -> detected multicast subscriptions."""
    return _table(text, ["vlan", "subscriber", "group", "interface", "type",
                         "timeout"])


def parse_lldp_remote(text: str) -> dict[str, dict[str, Any]]:
    """show lldp remote-device all -> connected device name/chassis per port."""
    rows = _table(text, ["intf", "rem_id", "chassis_id", "port_id", "name"])
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        iface = r["intf"]
        if _iface_to_id(iface) is None:
            continue
        if not r.get("chassis_id") and not r.get("name"):
            continue  # empty neighbor row
        out.setdefault(iface, {
            "lldp_system_name": r.get("name", "").strip(),
            "lldp_chassis_id": r.get("chassis_id", "").strip(),
        })
    return out


def parse_cablestatus(text: str) -> dict[str, str]:
    """cablestatus <iface> -> cable diagnostic result."""
    kv = _kv(text)
    out: dict[str, str] = {}
    if "Cable Status" in kv:
        out["cable_status"] = kv["Cable Status"]
    if "Cable Length" in kv:
        out["cable_length"] = kv["Cable Length"]
    return out


def parse_power_redundancy(text: str) -> dict[str, Any]:
    """show power redundancy (M4350) -> N+1 PSU redundancy state."""
    kv = _kv(text)
    out: dict[str, Any] = {}
    if "N+1 Active" in kv:
        out["psu_redundancy"] = (
            "Active" if _is_enabled(kv["N+1 Active"]) else "Not active"
        )
    return out


def parse_vlan_count(text: str) -> int:
    """show vlan -> number of VLANs (rows in the VLAN table)."""
    rows = _table(text, ["vlan_id", "name", "type", "rest"])
    return len([r for r in rows if r["vlan_id"].strip().isdigit()])


def parse_rgb_led(text: str) -> dict[str, dict[str, Any]]:
    """show rgb-led color configuration (select M4350) -> AV profile per port."""
    rows = _table(text, ["intf", "color", "profile"])
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if _iface_to_id(r["intf"]) is None:
            continue
        profile = (r.get("profile") or r.get("color") or "").strip()
        if profile:
            out[r["intf"]] = {"av_profile": profile}
    return out


# ────────────────────────── child schema ──────────────────────────

def _port_state_vars() -> dict[str, dict[str, Any]]:
    """Per-port (child) state. `online` + `label` are injected by the platform."""
    return {
        "interface": {"type": "string", "label": "Interface"},
        "description": {"type": "string", "label": "Description"},
        "link_status": {"type": "enum", "values": ["up", "down"],
                        "label": "Link", "cloud_priority": "high"},
        "speed": {"type": "string", "label": "Speed / Duplex"},
        "admin_status": {"type": "enum", "values": ["enabled", "disabled"],
                         "label": "Admin", "cloud_priority": "high"},
        "media_type": {"type": "string", "label": "Media"},
        "vlan": {"type": "string", "label": "VLAN"},
        "poe_capable": {"type": "boolean", "label": "PoE Capable"},
        "poe_admin": {"type": "enum", "values": ["enabled", "disabled"],
                      "label": "PoE Admin", "cloud_priority": "high"},
        "poe_status": {"type": "string", "label": "PoE Status",
                       "cloud_priority": "high"},
        "poe_power_w": {"type": "number", "label": "PoE Power (W)",
                        "cloud_priority": "high"},
        "poe_class": {"type": "string", "label": "PoE Class"},
        "poe_voltage_v": {"type": "number", "label": "PoE Voltage (V)",
                          "cloud_priority": "low"},
        "poe_current_ma": {"type": "number", "label": "PoE Current (mA)",
                           "cloud_priority": "low"},
        "poe_max_power_w": {"type": "number", "label": "PoE Max (W)",
                            "cloud_priority": "low"},
        "poe_priority": {"type": "string", "label": "PoE Priority"},
        "poe_fault": {"type": "string", "label": "PoE Fault"},
        "lldp_system_name": {"type": "string", "label": "Connected Device",
                             "cloud_priority": "low"},
        "lldp_chassis_id": {"type": "string", "label": "Connected MAC",
                            "cloud_priority": "low"},
        "multicast_groups": {"type": "integer", "label": "Multicast Groups",
                             "cloud_priority": "low"},
        "av_profile": {"type": "string", "label": "AV Profile (RGB LED)",
                       "cloud_priority": "low"},
    }


class NetgearM4250M4350Driver(BaseDriver):
    """NETGEAR M4250 / M4350 AV Line managed switch (CLI over SSH or telnet)."""

    DRIVER_INFO = {
        "id": "netgear_m4250_m4350",
        "name": "NETGEAR M4250 / M4350 AV Line Switch",
        "manufacturer": "NETGEAR",
        "category": "utility",
        "version": "1.1.0",
        "author": "OpenAVC",
        "min_platform_version": "0.15.0",
        "description": (
            "Monitor and control NETGEAR M4250 and M4350 AV Line managed "
            "switches over their CLI: per-port PoE power-cycling and draw, "
            "link state, multicast/IGMP health, system health (temperature, "
            "fans, PSU, CPU), reboot, and config save. Each port is a child "
            "entity."
        ),
        "source_url": "https://www.netgear.com/business/wired/switches/fully-managed/m4250/avline/",
        "tags": ["network", "switch", "poe", "av-over-ip", "igmp", "multicast"],
        "verified": True,
        "simulated": True,
        "protocols": ["netgear_cli"],
        "ports": [22, 23],
        "transport": "ssh",
        "discovery": {
            # Validated against a real M4250-40G8XF-PoE+ (2026-06-07):
            #   * OUI 28:94:01 is the unit's base-MAC vendor block.
            #   * SSDP/UPnP rootDesc.xml reports
            #     <manufacturer>NETGEAR</manufacturer>; the platform mines that
            #     into the "netgear" manufacturer_alias below, so a scan
            #     surfaces this driver as a possible match. The switch's SSDP
            #     device type is the generic InternetGatewayDevice:1 (every
            #     router advertises it), so it is deliberately NOT declared as
            #     an ssdp: fingerprint — that would false-positive every gateway.
            #   * SSH ident (generic OpenSSH) and SNMP (v2c off at factory) are
            #     not usable discriminators.
            # Both signals are soft: the host surfaces as a possible match and
            # the integrator confirms. The trailing prefixes are additional
            # NETGEAR OUI blocks (vendor-name display only).
            "oui": ["28:94:01", "b0:7f:b9", "08:bd:43", "9c:d3:6d",
                    "a0:40:a0", "3c:37:86"],
            "manufacturer_alias": ["netgear"],
        },
        "compatible_models": [
            {
                "manufacturer": "NETGEAR",
                "models": ["M4250 series"],
                "confidence": "full",
                "notes": "Validated against an M4250-40G8XF-PoE+ (software "
                         "13.0.5.14): PoE draw/control, ports, IGMP/multicast, "
                         "system health, config save, and reboot exercised on "
                         "hardware.",
            },
            {
                "manufacturer": "NETGEAR",
                "models": ["M4350 series"],
                "confidence": "untested",
                "notes": "Shares the CLI per the M4250/M4350 CLI Reference "
                         "Manual; not yet validated on M4350 hardware.",
            },
        ],
        "help": {
            "overview": (
                "Controls a NETGEAR M4250 or M4350 AV Line managed switch. Add "
                "it as one device; every physical port appears as a child "
                "entity under the Child Entities tab with its link state, PoE "
                "draw, and connected device. The driver auto-detects which "
                "family you have."
            ),
            "setup": (
                "1. Enable SSH on the switch (System > Management Access, or "
                "'ip ssh server enable' in the CLI). SSH is the recommended "
                "transport.\n"
                "2. Recommended: use key auth. Set Auth Method to 'key', then "
                "install the OpenAVC public key on the switch. Otherwise set "
                "Auth Method to 'password' and enter the admin password.\n"
                "3. Enter the switch IP and the admin username (default "
                "'admin'). Leave the port at 22 for SSH.\n"
                "4. If the admin account lands in User EXEC mode, set the "
                "Enable Password so the driver can enter Privileged EXEC.\n"
                "5. Ports, PoE status, and connected devices are discovered "
                "automatically on connect."
            ),
        },
        "default_config": {
            "host": "",
            "port": 22,
            "username": "admin",
            "transport": "ssh",
            "ssh_auth_method": "key",
            "poll_interval": 15,
            "detail_poll_interval": 60,
            # SSH connect validates via the banner read below, so skip the
            # generic post-open verify settle.
            "verify_timeout": 0,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "transport": {
                "type": "enum", "values": ["ssh", "tcp"], "default": "ssh",
                "label": "Connection",
                "description": "ssh = secure CLI (recommended). tcp = telnet "
                               "(plaintext; lab/simulator only).",
            },
            "port": {
                "type": "integer", "default": 22, "label": "Port",
                "description": "22 for SSH, 23 for telnet.",
            },
            "username": {
                "type": "string", "default": "admin", "label": "Username",
            },
            "ssh_auth_method": {
                "type": "enum", "values": ["key", "password"], "default": "key",
                "label": "Auth Method",
                "description": "key (recommended) or password. Ignored for telnet.",
            },
            "password": {
                "type": "password", "label": "Password", "secret": True,
                "description": "Login password (password auth, or telnet login).",
            },
            "key_path": {
                "type": "string", "label": "SSH Private Key Path",
                "description": "Path to the OpenAVC private key (key auth).",
            },
            "enable_password": {
                "type": "password", "label": "Enable Password", "secret": True,
                "description": "Privileged EXEC password, if the account needs "
                               "'enable'. Leave blank if none.",
            },
            "host_key_policy": {
                "type": "enum", "values": ["accept-new", "strict", "off"],
                "default": "accept-new", "label": "SSH Host Key Policy",
            },
            "poll_interval": {
                "type": "integer", "default": 15, "min": 0,
                "label": "Status Poll Interval (sec)",
                "description": "How often to poll port link/PoE status. 0 disables.",
            },
            "detail_poll_interval": {
                "type": "integer", "default": 60, "min": 0,
                "label": "Detail Poll Interval (sec)",
                "description": "How often to refresh system health, neighbors, "
                               "multicast, and inventory. 0 disables the heavy refresh.",
            },
        },
        "state_variables": {
            "model": {"type": "string", "label": "Model"},
            "series": {"type": "string", "label": "Series"},
            "firmware_version": {"type": "string", "label": "Firmware"},
            "boot_version": {"type": "string", "label": "Boot Code"},
            "serial_number": {"type": "string", "label": "Serial Number"},
            "mac_address": {"type": "string", "label": "Base MAC"},
            "system_name": {"type": "string", "label": "System Name"},
            "uptime": {"type": "string", "label": "Uptime"},
            "temperature_c": {"type": "integer", "label": "Temperature (C)"},
            "temperature_state": {"type": "string", "label": "Temperature State"},
            "fan_status": {"type": "string", "label": "Fans"},
            "psu_status": {"type": "string", "label": "Power Supplies"},
            "psu_redundancy": {"type": "string", "label": "PSU Redundancy (N+1)"},
            "cpu_util_5s": {"type": "number", "label": "CPU 5s (%)"},
            "cpu_util_60s": {"type": "number", "label": "CPU 60s (%)"},
            "cpu_util_300s": {"type": "number", "label": "CPU 300s (%)"},
            "mem_free_kb": {"type": "integer", "label": "Free Memory (KB)"},
            "mem_alloc_kb": {"type": "integer", "label": "Used Memory (KB)"},
            "mem_used_percent": {"type": "number", "label": "Memory Used (%)"},
            "poe_status": {"type": "string", "label": "PoE Controller"},
            "poe_total_power_w": {"type": "number", "label": "PoE Budget (W)"},
            "poe_consumed_power_w": {"type": "number", "label": "PoE Consumed (W)"},
            "poe_threshold_power_w": {"type": "number", "label": "PoE Threshold (W)"},
            "poe_usage_threshold": {"type": "integer", "label": "PoE Usage Threshold (%)"},
            "poe_power_mgmt_mode": {"type": "string", "label": "PoE Power Mgmt"},
            "poe_ports_delivering": {"type": "integer", "label": "PoE Ports Delivering"},
            "igmp_snooping": {"type": "boolean", "label": "IGMP Snooping"},
            "igmp_querier": {"type": "boolean", "label": "IGMP Querier"},
            "igmp_querier_address": {"type": "string", "label": "IGMP Querier Address"},
            "multicast_group_count": {"type": "integer", "label": "Multicast Groups"},
            "vlan_count": {"type": "integer", "label": "VLANs"},
            "port_count": {"type": "integer", "label": "Ports"},
            "ports_up": {"type": "integer", "label": "Ports Up"},
        },
        "child_entity_types": {
            "port": {
                "label": "Port",
                "label_plural": "Ports",
                "id_format": {"type": "integer", "min": 1, "max": PORT_MAX,
                              "pad_width": 5},
                "state_variables": _port_state_vars(),
                "summary_fields": ["interface", "link_status", "speed",
                                   "poe_status", "poe_power_w"],
                "label_field": "interface",
            },
        },
        "commands": {},  # populated by _build_commands() at module load
    }

    def __init__(self, device_id: str, config: dict[str, Any], state, events) -> None:
        self._rx_buffer = ""
        self._responses: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._cmd_lock = asyncio.Lock()
        self._poll_count = 0
        # naming: 2 = M4250 (slot/port), 3 = M4350 (unit/slot/port). Detected
        # from show version; defaults to 3 until then.
        self._naming = 3
        self._is_m4350 = True
        super().__init__(device_id, config, state, events)

    # Raw byte pipe — we do our own prompt framing (works over SSH and telnet).
    def _resolve_delimiter(self) -> bytes | None:
        return None

    # ── CLI framing ──

    async def on_data_received(self, data: bytes) -> None:
        """Accumulate the byte stream and emit one (text, boundary) unit each
        time the device pauses at a prompt or interactive sub-prompt.
        """
        self._rx_buffer += _ANSI_RE.sub("", data.decode("latin-1", errors="replace"))

        # Auto-answer the pager if it ever appears (terminal length 0 normally
        # prevents it). Advance a page with a space.
        if _PAGER_RE.search(self._rx_buffer):
            self._rx_buffer = _PAGER_STRIP_RE.sub("", self._rx_buffer)
            self._bg(self._safe_send(b" "))
            return

        for regex, kind in ((_PROMPT_RE, "prompt"),
                            (_PASSWORD_RE, "password"),
                            (_CONFIRM_RE, "confirm")):
            m = regex.search(self._rx_buffer)
            if m:
                unit = _clean(self._rx_buffer[:m.start()])
                self._rx_buffer = self._rx_buffer[m.end():]
                self._responses.put_nowait((unit, kind))
                return

    def _bg(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _safe_send(self, data: bytes) -> None:
        try:
            if self.transport:
                await self.transport.send(data)
        except (ConnectionError, OSError):
            pass

    @staticmethod
    def _strip_echo(text: str, sent: str) -> str:
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if lines and lines[0].strip() == sent.strip():
            lines = lines[1:]
        return "\n".join(lines).strip("\n")

    def _clear(self) -> None:
        self._rx_buffer = ""
        while not self._responses.empty():
            self._responses.get_nowait()

    async def _exchange(self, wire: str, timeout: float) -> tuple[str, str]:
        """Send one line and return (response_text, boundary_kind). Caller holds
        the lock and has cleared prior state.
        """
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self.transport.send(wire.encode("ascii", errors="replace") + b"\r\n")
        try:
            raw, kind = await asyncio.wait_for(self._responses.get(), timeout=timeout)
        except asyncio.TimeoutError as e:
            raise TimeoutError(f"[{self.device_id}] No response to {wire!r}") from e
        return self._strip_echo(raw, wire), kind

    async def _send_request(self, wire: str, timeout: float = 8.0) -> str:
        """Send one command line and return its response text (echo + prompt
        stripped). Serialised so responses correlate to commands.
        """
        async with self._cmd_lock:
            self._clear()
            text, _kind = await self._exchange(wire, timeout)
            return text

    async def _send_sequence(self, lines: list[str], timeout: float = 8.0) -> str:
        """Run several command lines atomically (e.g. a config-mode sequence)
        without another command interleaving. Returns the last response.
        """
        async with self._cmd_lock:
            last = ""
            for line in lines:
                self._clear()
                last, _kind = await self._exchange(line, timeout)
            return last

    async def _interface_config(self, iface: str, lines: list[str]) -> str:
        """configure -> interface <iface> -> <lines> -> exit -> exit."""
        return await self._send_sequence(
            ["configure", f"interface {iface}", *lines, "exit", "exit"]
        )

    # ── connection setup ──

    async def _post_connect(self) -> None:
        """Read the login banner, enter Privileged EXEC, disable paging, and
        detect the switch family. Runs (via BaseDriver.connect) right after the
        transport opens and before the device is marked connected.
        """
        # Banner ends at the first prompt/sub-prompt.
        try:
            _text, kind = await asyncio.wait_for(self._responses.get(), timeout=15.0)
        except asyncio.TimeoutError as e:
            err = getattr(self.transport, "last_error", "") or ""
            raise ConnectionError(
                f"[{self.device_id}] No CLI prompt from "
                f"{self.config.get('host', '?')}"
                + (f" ({err.splitlines()[-1]})" if err else "")
            ) from e

        if kind == "password":  # SSH/telnet asked for a login password at banner
            await self._answer(self.config.get("password", ""))

        await self._enter_privileged()

        # Disable paging for the session (M4350: terminal length 0; harmless
        # error on M4250, where the --More-- auto-answer covers paging).
        try:
            await self._send_request("terminal length 0", timeout=6.0)
        except Exception:
            log.debug(f"[{self.device_id}] terminal length 0 not accepted",
                      exc_info=True)

        await self._detect_family()

    async def _answer(self, value: str) -> None:
        """Reply to an interactive sub-prompt and wait for the next unit."""
        async with self._cmd_lock:
            await self.transport.send((value + "\r\n").encode("ascii", "replace"))
            try:
                await asyncio.wait_for(self._responses.get(), timeout=8.0)
            except asyncio.TimeoutError:
                pass

    async def _enter_privileged(self) -> None:
        """Send 'enable' and answer the enable-password prompt if asked."""
        async with self._cmd_lock:
            self._clear()
            try:
                _text, kind = await self._exchange("enable", timeout=8.0)
            except (TimeoutError, ConnectionError):
                return  # already privileged or enable unsupported
            if kind == "password":
                pw = self.config.get("enable_password", "")
                await self.transport.send((pw + "\r\n").encode("ascii", "replace"))
                try:
                    await asyncio.wait_for(self._responses.get(), timeout=8.0)
                except asyncio.TimeoutError:
                    pass

    async def _detect_family(self) -> None:
        """show version -> identity + M4250/M4350 family (interface naming)."""
        try:
            resp = await self._send_request("show version", timeout=8.0)
        except Exception:
            return
        info = parse_version(resp)
        model = info.get("model", "")
        if re.search(r"M4250", model, re.I):
            self._naming, self._is_m4350 = 2, False
            info["series"] = "M4250"
        elif re.search(r"M4350", model, re.I):
            self._naming, self._is_m4350 = 3, True
            info["series"] = "M4350"
        declared = self.DRIVER_INFO["state_variables"]
        self.set_states({k: v for k, v in info.items() if k in declared})

    # ── polling ──

    async def poll(self) -> None:
        await self._poll_fast()
        self._poll_count += 1
        detail = self.config.get("detail_poll_interval", 60)
        interval = self.config.get("poll_interval", 15) or 15
        if detail and detail > 0:
            every = max(1, round(detail / interval))
            if self._poll_count % every == 0:
                await self._poll_slow()

    async def _poll_fast(self) -> None:
        """Port link/admin/speed + PoE status into device state + port children.

        The primary queries are unwrapped so transport errors propagate to the
        poll watchdog (a swallowed error would let `connected` lie).
        """
        port_map = parse_port_table(await self._send_request("show port all"))
        poe_info = parse_poe_port_info(
            await self._send_request("show poe port info all"))
        poe_cfg = parse_poe_port_config(
            await self._send_request("show poe port configuration all"))

        merged: dict[str, dict[str, Any]] = {}
        for iface, props in port_map.items():
            merged.setdefault(iface, {}).update(props)
        for iface, props in poe_info.items():
            merged.setdefault(iface, {}).update(props)
        for iface, props in poe_cfg.items():
            merged.setdefault(iface, {}).update(props)

        self._reconcile_ports(merged)
        self.set_state("port_count", len(self.list_children("port")))
        self.set_state("ports_up",
                       sum(1 for p in merged.values() if p.get("link_status") == "up"))
        self.set_state("poe_ports_delivering",
                       sum(1 for p in merged.values()
                           if str(p.get("poe_status", "")).lower().startswith("delivering")))

        try:
            self._set_declared(parse_poe_global(await self._send_request("show poe")))
        except (ConnectionError, TimeoutError, OSError):
            raise
        except Exception:
            log.debug(f"[{self.device_id}] show poe parse failed", exc_info=True)

    async def _poll_slow(self) -> None:
        """System health, neighbors, multicast, inventory — best-effort, but
        transport errors still propagate to the watchdog.
        """
        await self._guarded("show version", lambda r: self._set_declared(parse_version(r)))
        await self._guarded("show environment",
                            lambda r: self._set_declared(parse_environment(r)))
        await self._guarded("show process cpu",
                            lambda r: self._set_declared(parse_process_cpu(r)))
        await self._guarded("show igmpsnooping",
                            lambda r: self._set_declared(parse_igmp_snooping(r)))
        await self._guarded("show igmpsnooping querier",
                            lambda r: self._set_declared(parse_igmp_querier(r)))
        await self._guarded("show vlan",
                            lambda r: self.set_state("vlan_count", parse_vlan_count(r)))
        await self._guarded("show interfaces status all", self._apply_iface_status)
        await self._guarded("show lldp remote-device all", self._apply_lldp)
        await self._guarded("show igmpsnooping group", self._apply_multicast)
        if self._is_m4350:
            await self._guarded("show power redundancy",
                                lambda r: self._set_declared(parse_power_redundancy(r)))
            await self._guarded("show rgb-led color configuration all",
                                self._apply_rgb_led)

    async def _guarded(self, wire: str, apply) -> None:
        """Run one query and apply its parser; swallow protocol/parse errors but
        re-raise transport errors (so the watchdog still sees a dead session).
        """
        try:
            resp = await self._send_request(wire)
            apply(resp)
        except (ConnectionError, TimeoutError, OSError):
            raise
        except Exception:
            log.debug(f"[{self.device_id}] {wire!r} failed", exc_info=True)

    def _set_declared(self, updates: dict[str, Any]) -> None:
        declared = self.DRIVER_INFO["state_variables"]
        clean = {k: v for k, v in updates.items() if k in declared}
        if clean:
            self.set_states(clean)

    def _apply_iface_status(self, resp: str) -> None:
        schema = self.get_child_entity_types()["port"]["state_variables"]
        for iface, props in parse_interfaces_status(resp).items():
            cid = _iface_to_id(iface)
            if cid is not None and self.is_child_registered("port", cid):
                clean = {k: v for k, v in props.items() if k in schema and v}
                if clean:
                    self.set_child_state_batch("port", cid, clean)

    def _apply_lldp(self, resp: str) -> None:
        seen = parse_lldp_remote(resp)
        for cid in self.list_children("port"):
            iface = self.get_child_state("port", cid).get("interface", "")
            props = seen.get(iface, {"lldp_system_name": "", "lldp_chassis_id": ""})
            self.set_child_state_batch("port", cid, props)

    def _apply_multicast(self, resp: str) -> None:
        rows = parse_igmp_groups(resp)
        self.set_state("multicast_group_count",
                       len({r["group"] for r in rows if r.get("group")}))
        per_iface: dict[str, int] = {}
        for r in rows:
            per_iface[r.get("interface", "")] = per_iface.get(r.get("interface", ""), 0) + 1
        for cid in self.list_children("port"):
            iface = self.get_child_state("port", cid).get("interface", "")
            self.set_child_state("port", cid, "multicast_groups",
                                 per_iface.get(iface, 0))

    def _apply_rgb_led(self, resp: str) -> None:
        schema = self.get_child_entity_types()["port"]["state_variables"]
        for iface, props in parse_rgb_led(resp).items():
            cid = _iface_to_id(iface)
            if cid is not None and self.is_child_registered("port", cid):
                self.set_child_state_batch(
                    "port", cid, {k: v for k, v in props.items() if k in schema})

    def _reconcile_ports(self, merged: dict[str, dict[str, Any]]) -> None:
        """Register newly-seen ports, update known ones, drop any that vanished
        (e.g. after an M4350 100G breakout reconfiguration).
        """
        schema = self.get_child_entity_types()["port"]["state_variables"]
        current = set(self.list_children("port"))
        seen: set[int] = set()
        batch: list[tuple[str, int, dict[str, Any]]] = []
        for iface, props in merged.items():
            cid = _iface_to_id(iface)
            if cid is None:
                continue
            seen.add(cid)
            clean = {k: v for k, v in props.items() if k in schema}
            clean["interface"] = iface
            clean["online"] = True
            if cid not in current:
                self.register_child("port", cid, initial_state=clean)
            else:
                batch.append(("port", cid, clean))
        if batch:
            self.set_children_state_batch(batch)
        for cid in current - seen:
            self.deregister_child("port", cid)

    async def refresh_children(self) -> dict[str, Any]:
        await self._poll_fast()
        await self._poll_slow()
        return {"ports": len(self.list_children("port"))}

    # ── command dispatch ──

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = dict(params or {})
        self._coerce_child_ids(command, params)

        if command == "reboot":
            return await self._reboot()
        if command == "save_config":
            return await self._send_request("write memory confirm", timeout=20.0)
        if command == "poe_reset_all":
            return await self._send_sequence(["configure", "poe reset", "exit"])
        if command in _PORT_COMMANDS:
            return await self._port_command(command, params)
        if command == "cable_test":
            return await self._cable_test(params)
        if command == "clear_port_counters":
            cid = int(params["port"])
            iface = self.get_child_state("port", cid).get("interface")
            if not iface:
                raise ValueError(f"Port {cid} is not a known interface")
            return await self._send_confirmable(f"clear counters {iface}")
        raise ValueError(f"Unknown command: {command}")

    async def _port_command(self, command: str, params: dict[str, Any]) -> str:
        cid = int(params["port"])
        iface = self.get_child_state("port", cid).get("interface")
        if not iface:
            raise ValueError(f"Port {cid} is not a known interface")
        spec = _PORT_COMMANDS[command]
        lines = [line.format(**params) for line in spec["lines"]]
        resp = await self._interface_config(iface, lines)
        # Optimistic state so the UI reflects the change before the next poll.
        if spec.get("optimistic"):
            self.set_child_state_batch("port", cid, spec["optimistic"](params))
        return resp

    async def _reboot(self) -> str:
        """reload, answering the confirmation prompt."""
        async with self._cmd_lock:
            self._clear()
            _text, kind = await self._exchange("reload", timeout=10.0)
            if kind == "confirm":
                await self.transport.send(b"y\r\n")
            return "Reload requested"

    async def _send_confirmable(self, wire: str, timeout: float = 10.0) -> str:
        """Send a command, answering a single (y/n) confirmation if the device
        asks for one (e.g. clear counters)."""
        async with self._cmd_lock:
            self._clear()
            text, kind = await self._exchange(wire, timeout)
            if kind == "confirm":
                await self.transport.send(b"y\r\n")
                try:
                    text, _ = await asyncio.wait_for(
                        self._responses.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass
            return text

    async def _cable_test(self, params: dict[str, Any]) -> dict[str, str]:
        cid = int(params["port"])
        iface = self.get_child_state("port", cid).get("interface")
        if not iface:
            raise ValueError(f"Port {cid} is not a known interface")
        return parse_cablestatus(await self._send_request(f"cablestatus {iface}",
                                                          timeout=20.0))

    def _coerce_child_ids(self, command: str, params: dict[str, Any]) -> None:
        cmd_def = self.DRIVER_INFO["commands"].get(command, {})
        for pname, pdef in cmd_def.get("params", {}).items():
            if pdef.get("type") == "child_id" and params.get(pname) not in (None, ""):
                try:
                    params[pname] = int(params[pname])
                except (TypeError, ValueError) as e:
                    raise ValueError(
                        f"{command}: parameter {pname!r} must be an integer id, "
                        f"got {params[pname]!r}"
                    ) from e

    # ── device settings (device-level, with offline pending-queue) ──

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if key == "poe_usage_threshold":
            pct = int(value)
            cmd = (f"poe usagethreshold all {pct}" if self._is_m4350
                   else f"poe usagethreshold {pct}")
            await self._send_sequence(["configure", cmd, "exit"])
            self.set_state("poe_usage_threshold", pct)
            return
        if key == "poe_power_mgmt_mode":
            mode = str(value).lower()
            cmd = (f"poe power management all {mode}" if self._is_m4350
                   else f"poe power management {mode}")
            await self._send_sequence(["configure", cmd, "exit"])
            self.set_state("poe_power_mgmt_mode", mode.capitalize())
            return
        raise ValueError(f"Unknown device setting: {key}")


# ────────────────────────── command surface ──────────────────────────

# Per-port commands: interface-config line templates + optional optimistic state.
_PORT_COMMANDS: dict[str, dict[str, Any]] = {
    "poe_cycle_port": {"lines": ["poe reset"]},
    "poe_enable_port": {
        "lines": ["poe"],
        "optimistic": lambda p: {"poe_admin": "enabled"},
    },
    "poe_disable_port": {
        "lines": ["no poe"],
        "optimistic": lambda p: {"poe_admin": "disabled", "poe_status": "Disabled"},
    },
    "port_enable": {
        "lines": ["no shutdown"],
        "optimistic": lambda p: {"admin_status": "enabled"},
    },
    "port_disable": {
        "lines": ["shutdown"],
        "optimistic": lambda p: {"admin_status": "disabled", "link_status": "down"},
    },
    "set_poe_priority": {
        "lines": ["poe priority {priority}"],
        "optimistic": lambda p: {"poe_priority": str(p["priority"]).lower()},
    },
    "set_port_description": {
        # Quote the text: the switch rejects a bare multi-word description
        # ("% Invalid input") but accepts it quoted, and stores it quoted.
        "lines": ['description "{description}"'],
        "optimistic": lambda p: {"description": str(p["description"])},
    },
}


def _port_param() -> dict[str, Any]:
    return {"type": "child_id", "child_type": "port", "required": True,
            "label": "Port"}


def _build_commands() -> dict[str, dict[str, Any]]:
    return {
        "reboot": {
            "label": "Reboot Switch",
            "params": {},
            "help": "Reload the switch (all connections drop while it restarts).",
        },
        "save_config": {
            "label": "Save Configuration",
            "params": {},
            "help": "Save the running configuration so changes survive a reboot.",
        },
        "poe_cycle_port": {
            "label": "Power-Cycle PoE Port",
            "params": {"port": _port_param()},
            "help": "Reset PoE on the port to reboot a frozen powered device.",
        },
        "poe_enable_port": {
            "label": "Enable PoE on Port",
            "params": {"port": _port_param()},
            "help": "Turn PoE on for the port.",
        },
        "poe_disable_port": {
            "label": "Disable PoE on Port",
            "params": {"port": _port_param()},
            "help": "Turn PoE off for the port.",
        },
        "poe_reset_all": {
            "label": "Reset All PoE Ports",
            "params": {},
            "help": "Reset PoE on every port (bulk recovery).",
        },
        "port_enable": {
            "label": "Enable Port",
            "params": {"port": _port_param()},
            "help": "Bring the port up (no shutdown).",
        },
        "port_disable": {
            "label": "Disable Port",
            "params": {"port": _port_param()},
            "help": "Shut the port down.",
        },
        "set_poe_priority": {
            "label": "Set PoE Priority",
            "params": {
                "port": _port_param(),
                "priority": {"type": "enum", "values": ["crit", "high", "medium", "low"],
                             "required": True, "label": "Priority"},
            },
            "help": "Set the PoE power priority used when the budget is tight.",
        },
        "set_port_description": {
            "label": "Set Port Description",
            "params": {
                "port": _port_param(),
                "description": {"type": "string", "required": True,
                                "label": "Description"},
            },
            "help": "Set the port's text description on the switch.",
        },
        "cable_test": {
            "label": "Cable Test",
            "params": {"port": _port_param()},
            "help": "Run TDR cable diagnostics on the port (briefly disrupts the link).",
        },
        "clear_port_counters": {
            "label": "Clear Port Counters",
            "params": {"port": _port_param()},
            "help": "Reset the port's traffic/error counters.",
        },
    }


NetgearM4250M4350Driver.DRIVER_INFO["commands"] = _build_commands()
