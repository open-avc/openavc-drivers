"""
OpenAVC AVPro Edge MXnet network switch driver.

Covers the MXnet managed switches that carry an MXnet AV-over-IP system:
AC-MXNET-SW8P (formerly SW12) and its 8/24/48-port siblings. Every physical
port is modelled as an OpenAVC *child entity* of the switch (state keyed
``device.<id>.port.<padded_id>.<prop>``), so PoE draw, link state, error
counters, and the endpoint attached to each port are visible and controllable
through one OpenAVC device.

**This is infrastructure, not a router.** An MXnet switch has no AV crosspoint
-- video routing lives in the CBOX and is driven by ``avproedge_mxnet_1g``,
where encoders and decoders are child entities. The two drivers are
complementary: the CBOX says what *should* be routed, this one says why it
isn't (port down, PoE not delivering, endpoint never joined the group).

Why this matters for AV: PoE troubleshooting (power-cycle a wedged
encoder/decoder without walking to the rack), per-port PoE draw / class /
priority against the switch's power budget, IGMP snooping health and
per-port multicast group membership (the usual cause of "no video" on
AV-over-IP), link and CRC/error counters, endpoint-to-port mapping from the
switch's own MAC table, and system health.

Transport / protocol:

* The CLI is a DCN-family managed-switch CLI (AVPro's published command guide
  carries ``Author: dcn``). **Telnet** (``transport: tcp``, port 23) is the
  default: it reaches the CLI on every switch out of the box and is what the
  bundled simulator speaks. **SSH** (``transport: ssh``, port 22) reaches the
  same CLI through the platform SSH transport, which shells out to the OS
  OpenSSH client -- but the switch's SSH server offers only ``hmac-sha1`` as
  its MAC algorithm, and OpenSSH 8.8+ dropped that from its defaults, so the
  handshake fails with ``no matching MAC found`` on any current client even
  though ``ssh-server enable`` ships on. KEX, host key and ciphers all
  negotiate fine; the MAC list is the whole problem. Until the platform SSH
  transport offers a legacy-algorithm opt-in, telnet is the working path.
* Strictly request/response. The switch echoes each command, prints the
  response, then re-prints its prompt (``switch#``, ``switch(config)#``,
  ``switch(config-if-ethernet1/0/2)#``). The prompt is the end-of-response
  marker. Interactive sub-prompts (``login:``, ``Password:``, a reload
  confirmation) are recognised so the driver can answer them.
* Paging is disabled per session with ``terminal length 0``; the ``-More-``
  pager is also auto-answered as a fallback.

**Python rather than YAML** because the CLI needs prompt-based framing over a
raw byte pipe, one query fans out into twelve child entities (``show interface
ethernet status`` and ``show power inline interface`` are column tables), the
IGMP membership table has to be inverted from group-major to port-major, and
``ssh`` has no ``.avcdriver`` surface at all.

Hardware notes (AC-MXNET-SW8P, software V705R002C013, 2026-08-17):

* **``power inline reset`` is acked and does nothing.** It is the obvious
  candidate for a PoE power-cycle and it returns cleanly, but measured against
  a live 4 W encoder at 1.5 s sampling the port never drops: power, voltage
  (52 V) and class (3) stay put and the link never goes down. The driver
  therefore implements ``poe_cycle_port`` as ``no power inline enable`` ->
  wait -> ``power inline enable``, which does cut power hard (verified: the
  same port reads ``disable off 0 ... 0 0 low 0`` within 1.5 s, then returns
  to ~4 W class 3 on re-enable).
* Ports 9-12 on an SW8P are SFP+ and report ``Power inline is not supported on
  interface EthernetX``; they are registered as children with
  ``poe_capable: false`` rather than dropped.
* The factory management address is ``192.168.1.238/24`` with **no default
  gateway**, so an untouched switch is only reachable from its own subnet.

License: MIT.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from openavc.drivers.base import BaseDriver
from openavc.utils.logger import get_logger

log = get_logger(__name__)

# Strip ANSI/VT escape sequences a PTY session can emit.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
# CLI prompt: "switch#", "switch(config)#", "switch(config-if-ethernet1/0/2)#".
# The hostname is user-settable, so match "<word>[(...)]#" generically. Anchored
# to end-of-buffer so a prompt string quoted inside output can't trigger it.
_PROMPT_RE = re.compile(r"[^\s()#>]+(?:\([^()\r\n]*\))?\s*[#>]\s*$")
# Login prompts. The switch presents "login:" then "Password:" over telnet; SSH
# password auth is handled by the transport, so neither fires there.
_LOGIN_RE = re.compile(r"(?:[Ll]ogin|[Uu]ser(?:name)?)\s*:\s*$")
_PASSWORD_RE = re.compile(r"[Pp]assword\s*:\s*$")
# Interactive confirmations (reload, and anything else that asks before acting).
_CONFIRM_RE = re.compile(
    r"\[[Yy]/[Nn]\][\s.:?]*$|\([Yy]/[Nn]\)[\s.:?]*$|\[confirm\][\s.:?]*$", re.I)
# Pager prompt (fallback when terminal length 0 is unavailable).
_PAGER_RE = re.compile(r"-{1,2}More-{1,2}[^\n]*$", re.I)
_PAGER_STRIP_RE = re.compile(r"-{1,2}More-{1,2}[^\n]*", re.I)

# Telnet IAC option negotiation. The switch opens with IAC WILL ECHO / WILL SGA;
# SSH and the bundled simulator never negotiate. Values per RFC 854.
_IAC, _DONT, _DO, _WONT, _WILL, _SB, _SE = 255, 254, 253, 252, 251, 250, 240

# Child-id encoding: unit*10000 + slot*1000 + port, matching the convention the
# catalog's other managed-switch driver uses. Units matter because these
# switches stack (VSF, "member 1..4"), so a bare port number is not unique.
PORT_MAX = 99999

# The MXnet OUI, shared by the switch and by MXnet encoders/decoders. Used only
# to label what is plugged into a port -- see _endpoint_kind().
_MXNET_OUI = "18:8a:6a"


# ────────────────────────── interface id helpers ──────────────────────────

def _iface_parts(iface: str) -> tuple[int, int, int] | None:
    """Parse 'unit/slot/port' (or a bare 'slot/port') into ints.

    Returns None for anything that is not a physical interface (port-channels,
    Vlan interfaces, blanks).
    """
    parts = iface.strip().split("/")
    if not all(p.strip().isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts]
    if len(nums) == 2:
        return (1, nums[0], nums[1])
    if len(nums) == 3:
        return (nums[0], nums[1], nums[2])
    return None


def _iface_to_id(iface: str) -> int | None:
    """Deterministic integer child id, so a port keeps the same id across polls."""
    parts = _iface_parts(iface)
    if parts is None:
        return None
    unit, slot, port = parts
    return unit * 10000 + slot * 1000 + port


def _normalise_iface(raw: str) -> str:
    """'Ethernet1/0/3' or '1/0/3' -> '1/0/3'."""
    return re.sub(r"(?i)^ethernet\s*", "", raw.strip())


def _normalise_mac(raw: str) -> str:
    """'18-8a-6a-00-87-ab' / '18:8A:6A:00:87:AB' -> '18:8a:6a:00:87:ab'."""
    hexonly = re.sub(r"[^0-9a-fA-F]", "", raw)
    if len(hexonly) != 12:
        return ""
    return ":".join(hexonly[i:i + 2] for i in range(0, 12, 2)).lower()


def _endpoint_kind(mac: str) -> str:
    """What is on this port, as far as the switch can tell.

    The switch sees MAC addresses, not device types -- it cannot tell an
    encoder from a decoder. So this reports only what is honestly derivable:
    whether the attached device is MXnet gear or something else.
    """
    if not mac:
        return ""
    return "MXnet endpoint" if mac.startswith(_MXNET_OUI) else "other"


# ────────────────────────── generic parse helpers ──────────────────────────

def _clean(text: str) -> str:
    """Drop ANSI escapes and pager tokens from a response body."""
    return _PAGER_STRIP_RE.sub("", _ANSI_RE.sub("", text))


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _mw_to_w(milliwatts: Any) -> float:
    """Milliwatts (what the CLI prints) to watts, rounded to 0.1 W."""
    return round(_to_int(milliwatts) / 1000.0, 3)


# ────────────────────────── response parsers ──────────────────────────
#
# Every parser takes the raw response text and returns plain dicts, so each one
# is directly testable without a transport. Sample output for each is in the
# driver's shipped record.

def parse_version(text: str) -> dict[str, Any]:
    """``show version``.

        AC-MXNET-SW8P Device, Compiled on Mar 11 13:11:45 2025
        CPU Mac 18:8a:6a:03:c9:4e
        Vlan MAC 18:8a:6a:03:c9:4d
        SoftWare Package Version V705R002C013
        BootRom Version 7.5.15
        HardWare Version 1.0.2
        Serial No.:SW100126030400073
        Uptime is 0 weeks, 0 days, 0 hours, 43 minutes
    """
    out: dict[str, Any] = {}
    if m := re.search(r"^\s*(AC-[A-Z0-9\-]+)\s+Device", text, re.M | re.I):
        out["model"] = m.group(1).upper()
    if m := re.search(r"SoftWare Package Version\s+(\S+)", text, re.I):
        out["firmware_version"] = m.group(1)
    if m := re.search(r"BootRom Version\s+(\S+)", text, re.I):
        out["boot_version"] = m.group(1)
    if m := re.search(r"HardWare Version\s+(\S+)", text, re.I):
        out["hardware_version"] = m.group(1)
    if m := re.search(r"Serial No\.?\s*:\s*(\S+)", text, re.I):
        out["serial_number"] = m.group(1)
    if m := re.search(r"Vlan MAC\s+([0-9a-fA-F:.\-]{12,17})", text, re.I):
        out["mac_address"] = _normalise_mac(m.group(1))
    if m := re.search(r"Uptime is\s+(.+?)\s*$", text, re.M | re.I):
        out["uptime"] = m.group(1).strip()
    return out


def parse_interface_status(text: str) -> dict[str, dict[str, Any]]:
    """``show interface ethernet status`` -> {iface: props}.

        Interface       Link/Protocol  Speed   Duplex  Vlan   Type    Alias Name
        1/0/1           UP/UP          a-1G    a-FULL  1      G-TX
        1/0/9           DOWN/DOWN      auto    auto    1      SFP+
    """
    out: dict[str, dict[str, Any]] = {}
    for line in _clean(text).splitlines():
        m = re.match(
            r"^\s*(\d+/\d+/\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*(.*)$",
            line)
        if not m:
            continue
        iface, link, speed, duplex, vlan, media, alias = m.groups()
        link_state = link.split("/")[0].strip().lower()
        props: dict[str, Any] = {
            "interface": iface,
            "link_status": "up" if link_state == "up" else "down",
            "speed": speed,
            "duplex": duplex,
            "vlan": vlan,
            "media_type": media,
            # "A-Down" in the Link column is the switch's administratively-down
            # marker (see the Codes: legend the command prints above the table).
            "admin_status": "disabled" if link_state.startswith("a-") else "enabled",
        }
        alias = alias.strip()
        if alias and alias.lower() != "(null)":
            props["description"] = alias
        out[iface] = props
    return out


def parse_poe_ports(text: str) -> dict[str, dict[str, Any]]:
    """``show power inline interface`` -> {iface: props}.

        Interface       Status  Oper  Power(mW) Max-type Max(mW) Current(mA) Volt(V) Priority Class
        Ethernet1/0/1    enable    on      3300    class   30000          62      52      low     3
        Power inline is not supported on interface Ethernet1/0/9.

    The "not supported" line is how the SFP+ ports report themselves; they get
    ``poe_capable: false`` rather than being dropped, so the child roster still
    matches the physical switch.
    """
    out: dict[str, dict[str, Any]] = {}
    for line in _clean(text).splitlines():
        m = re.match(
            r"^\s*Ethernet(\d+/\d+/\d+)\s+(\S+)\s+(\S+)\s+(\d+)\s+(\S+)\s+"
            r"(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\d+)\s*$", line)
        if m:
            (iface, status, oper, power_mw, _max_type, max_mw,
             current_ma, volt, priority, cls) = m.groups()
            out[iface] = {
                "poe_capable": True,
                "poe_admin": "enabled" if status.lower().startswith("enable")
                             else "disabled",
                "poe_status": oper.lower(),
                "poe_power_w": _mw_to_w(power_mw),
                "poe_max_power_w": _mw_to_w(max_mw),
                "poe_current_ma": _to_int(current_ma),
                "poe_voltage_v": _to_int(volt),
                "poe_priority": priority.lower(),
                "poe_class": _to_int(cls),
            }
            continue
        m = re.match(
            r"^\s*Power inline is not supported on interface Ethernet(\d+/\d+/\d+)",
            line, re.I)
        if m:
            out[m.group(1)] = {
                "poe_capable": False,
                "poe_admin": "disabled",
                "poe_status": "n/a",
                "poe_power_w": 0.0,
                "poe_current_ma": 0,
                "poe_voltage_v": 0,
                "poe_class": 0,
            }
    return out


def parse_poe_global(text: str) -> dict[str, Any]:
    """``show power inline``.

        Power Inline Status: On
        Power Available: 125 W
        Power Used: 7 W
        Power Remaining: 118 W
        Police: Off
        Legacy: Off
    """
    out: dict[str, Any] = {}
    body = _clean(text)
    if m := re.search(r"Power Inline Status\s*:\s*(\S+)", body, re.I):
        out["poe_status"] = m.group(1).strip().lower()
    for key, field in (("Power Available", "poe_budget_w"),
                       ("Power Used", "poe_consumed_w"),
                       ("Power Remaining", "poe_remaining_w")):
        if m := re.search(rf"{key}\s*:\s*([\d.]+)\s*W", body, re.I):
            out[field] = _to_float(m.group(1))
    for key, field in (("Police", "poe_police"), ("Legacy", "poe_legacy")):
        if m := re.search(rf"^\s*{key}\s*:\s*(\S+)", body, re.I | re.M):
            out[field] = "enabled" if m.group(1).strip().lower() in (
                "on", "enable", "enabled") else "disabled"
    if m := re.search(r"Pse Type\s*:\s*(.+?)\s*$", body, re.I | re.M):
        out["poe_pse_type"] = m.group(1).strip()
    return out


def parse_temperature(text: str) -> dict[str, Any]:
    """``show temperature`` -> ``Temperature: 44C/111F``."""
    if m := re.search(r"Temperature\s*:\s*(-?\d+)\s*C", text, re.I):
        return {"temperature_c": _to_int(m.group(1))}
    return {}


def parse_cpu(text: str) -> dict[str, Any]:
    """``show cpu usage``. The switch reports IDLE; usage is the complement."""
    out: dict[str, Any] = {}
    for label, field in ((r"5\s*second", "cpu_usage_5s"),
                         (r"30\s*second", "cpu_usage_30s"),
                         (r"5\s*minute", "cpu_usage_5m")):
        if m := re.search(rf"Last\s+{label}\s+CPU IDLE:\s*(\d+)\s*%", text, re.I):
            out[field] = round(100.0 - _to_float(m.group(1)), 1)
    return out


def parse_memory(text: str) -> dict[str, Any]:
    """``show memory usage``.

        The memory total 256 MB , free 105050112 bytes , usage is 60.87%
    """
    out: dict[str, Any] = {}
    if m := re.search(r"memory total\s+(\d+)\s*MB", text, re.I):
        out["memory_total_mb"] = _to_int(m.group(1))
    if m := re.search(r"usage is\s+([\d.]+)\s*%", text, re.I):
        out["memory_usage_percent"] = round(_to_float(m.group(1)), 1)
    return out


def parse_igmp_snooping(text: str) -> dict[str, Any]:
    """``show ip igmp snooping`` (global).

        Global igmp snooping status   :Enabled
        Igmp snooping is turned on for vlan 1(querier)
    """
    out: dict[str, Any] = {}
    if m := re.search(r"Global igmp snooping status\s*:\s*(\S+)", text, re.I):
        out["igmp_snooping"] = m.group(1).strip().lower().startswith("enable")
    vlans = re.findall(r"turned on for vlan\s+(\d+)(\(querier\))?", text, re.I)
    if vlans:
        out["igmp_snooping_vlans"] = ", ".join(v[0] for v in vlans)
        out["igmp_querier"] = any(v[1] for v in vlans)
    return out


def parse_igmp_groups(text: str) -> list[dict[str, str]]:
    """``show ip igmp snooping vlan <n>`` membership table -> flat rows.

    The table is group-major with continuation lines for extra ports:

        Groups          Sources   Ports          Exptime   SrcMac            ...
        225.1.0.0       *         Ethernet1/0/1  00:04:01  18:8A:6A:02:09:DB
                                  Ethernet1/0/3  00:04:01  18:8A:6A:00:87:AB

    so the current group carries down until a new one appears.
    """
    rows: list[dict[str, str]] = []
    current = ""
    started = False
    for line in _clean(text).splitlines():
        if re.search(r"^\s*Groups\s+Sources\s+Ports", line, re.I):
            started = True
            continue
        if not started:
            continue
        if re.search(r"current/limit groups|mrouter port", line, re.I):
            break
        m = re.match(
            r"^\s*(\d+\.\d+\.\d+\.\d+)?\s+\S*\s*Ethernet(\d+/\d+/\d+)\s+"
            r"(\S+)\s*([0-9A-Fa-f:.\-]{12,17})?", line)
        if not m:
            # A continuation line has no group and no source column.
            m2 = re.match(
                r"^\s+Ethernet(\d+/\d+/\d+)\s+(\S+)\s*([0-9A-Fa-f:.\-]{12,17})?",
                line)
            if m2 and current:
                rows.append({"group": current, "interface": m2.group(1),
                             "mac": _normalise_mac(m2.group(3) or "")})
            continue
        group, iface, _exp, mac = m.groups()
        if group:
            current = group
        if current:
            rows.append({"group": current, "interface": iface,
                         "mac": _normalise_mac(mac or "")})
    return rows


def parse_mac_table(text: str) -> dict[str, list[str]]:
    """``show mac-address-table`` -> {iface: [mac, ...]}.

        Vlan Mac Address                 Type    Creator   Ports
        1    18-8a-6a-00-87-ab           DYNAMIC Hardware Ethernet1/0/3
    """
    out: dict[str, list[str]] = {}
    for line in _clean(text).splitlines():
        m = re.match(
            r"^\s*\d+\s+([0-9a-fA-F][0-9a-fA-F][-:.][0-9a-fA-F:.\-]{10,})\s+"
            r"\S+\s+\S+\s+Ethernet(\d+/\d+/\d+)\s*$", line)
        if not m:
            continue
        mac = _normalise_mac(m.group(1))
        if mac:
            out.setdefault(m.group(2), []).append(mac)
    return out


def parse_port_counters(text: str) -> dict[str, dict[str, Any]]:
    """``show interface ethernet <range>`` -> {iface: counters}.

    One block per interface; the block header names the interface and the
    Input/Output statistics sections follow.
    """
    out: dict[str, dict[str, Any]] = {}
    current = ""
    for line in _clean(text).splitlines():
        if m := re.match(r"^\s*Ethernet(\d+/\d+/\d+)\s+is\s+\S+,\s*line protocol",
                         line, re.I):
            current = m.group(1)
            out.setdefault(current, {})
            continue
        if not current:
            continue
        if m := re.search(r"(\d+)\s+input errors,\s*(\d+)\s+CRC", line, re.I):
            out[current]["input_errors"] = _to_int(m.group(1))
            out[current]["crc_errors"] = _to_int(m.group(2))
            continue
        if m := re.search(r"(\d+)\s+output errors,\s*(\d+)\s+collisions", line, re.I):
            out[current]["output_errors"] = _to_int(m.group(1))
            continue
        if m := re.search(r"^\s*(\d+)\s+input packets,\s*(\d+)\s+bytes", line, re.I):
            out[current]["rx_packets"] = _to_int(m.group(1))
            out[current]["rx_bytes"] = _to_int(m.group(2))
            continue
        if m := re.search(r"^\s*(\d+)\s+output packets,\s*(\d+)\s+bytes", line, re.I):
            out[current]["tx_packets"] = _to_int(m.group(1))
            out[current]["tx_bytes"] = _to_int(m.group(2))
            continue
        # The rate lines sit in the block's "Statistics:" preamble, ABOVE the
        # Input/Output packet-statistics headers -- so they must not be gated on
        # having seen one of those headers.
        if m := re.search(
                r"The last 5 second (input|output) rate\s+(\d+)\s+bits/sec",
                line, re.I):
            key = "rx_rate_bps" if m.group(1).lower() == "input" else "tx_rate_bps"
            out[current][key] = _to_int(m.group(2))
    return out


def parse_vlan_count(text: str) -> int:
    """``show vlan`` -> number of VLAN rows."""
    return len(re.findall(r"^\s*(\d+)\s+\S+\s+(?:Static|Dynamic)",
                          _clean(text), re.I | re.M))


def parse_transceivers(text: str) -> dict[str, dict[str, Any]]:
    """``show transceiver`` -> {iface: optical readings}. Empty when no SFPs."""
    out: dict[str, dict[str, Any]] = {}
    for line in _clean(text).splitlines():
        m = re.match(
            r"^\s*(?:Ethernet)?(\d+/\d+/\d+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+"
            r"(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)", line)
        if not m:
            continue
        iface, temp, volt, bias, rx, tx = m.groups()
        out[iface] = {
            "sfp_temperature_c": _to_float(temp),
            "sfp_voltage_v": _to_float(volt),
            "sfp_bias_ma": _to_float(bias),
            "sfp_rx_power_dbm": _to_float(rx),
            "sfp_tx_power_dbm": _to_float(tx),
        }
    return out


# ────────────────────────── child schema ──────────────────────────

def _port_state_vars() -> dict[str, dict[str, Any]]:
    """Per-port (child) state. `online` + `label` are injected by the platform."""
    return {
        "interface": {"type": "string", "label": "Interface"},
        "description": {"type": "string", "label": "Description"},
        "link_status": {"type": "enum", "values": ["up", "down"],
                        "label": "Link", "control": True,
                        "cloud_priority": "high"},
        "speed": {"type": "string", "label": "Speed"},
        "duplex": {"type": "string", "label": "Duplex"},
        "vlan": {"type": "string", "label": "VLAN"},
        "media_type": {"type": "string", "label": "Media"},
        "admin_status": {"type": "enum", "values": ["enabled", "disabled"],
                         "label": "Admin", "cloud_priority": "high"},
        # ── PoE ──
        "poe_capable": {"type": "boolean", "label": "PoE Capable"},
        "poe_admin": {"type": "enum", "values": ["enabled", "disabled"],
                      "label": "PoE Admin", "control": True,
                      "cloud_priority": "high"},
        "poe_status": {"type": "string", "label": "PoE Delivering",
                       "cloud_priority": "high"},
        "poe_power_w": {"type": "number", "label": "PoE Power", "unit": "W",
                        "min": 0, "max": 90, "step": 0.1,
                        "cloud_priority": "high"},
        "poe_class": {"type": "integer", "label": "PoE Class",
                      "min": 0, "max": 8},
        "poe_voltage_v": {"type": "integer", "label": "PoE Voltage", "unit": "V",
                          "min": 0, "max": 60, "cloud_priority": "low"},
        "poe_current_ma": {"type": "integer", "label": "PoE Current", "unit": "mA",
                           "min": 0, "max": 2000, "cloud_priority": "low"},
        "poe_max_power_w": {"type": "number", "label": "PoE Max", "unit": "W",
                            "min": 0, "max": 90, "step": 0.1,
                            "cloud_priority": "low"},
        "poe_priority": {"type": "enum", "values": ["critical", "high", "low"],
                         "label": "PoE Priority"},
        # ── what is plugged in ──
        "connected_mac": {"type": "string", "label": "Connected MAC",
                          "cloud_priority": "low"},
        "connected_kind": {"type": "string", "label": "Connected Device",
                           "cloud_priority": "low"},
        "multicast_groups": {"type": "integer", "label": "Multicast Groups",
                             "min": 0, "cloud_priority": "low"},
        "multicast_group_list": {"type": "string", "label": "Groups Joined",
                                 "cloud_priority": "low"},
        # ── counters ──
        "rx_rate_bps": {"type": "integer", "label": "Rx Rate", "unit": "bps",
                        "min": 0, "cloud_priority": "low"},
        "tx_rate_bps": {"type": "integer", "label": "Tx Rate", "unit": "bps",
                        "min": 0, "cloud_priority": "low"},
        "rx_packets": {"type": "integer", "label": "Rx Packets", "min": 0,
                       "cloud_priority": "low"},
        "tx_packets": {"type": "integer", "label": "Tx Packets", "min": 0,
                       "cloud_priority": "low"},
        "input_errors": {"type": "integer", "label": "Input Errors", "min": 0,
                         "cloud_priority": "high"},
        "crc_errors": {"type": "integer", "label": "CRC Errors", "min": 0,
                       "cloud_priority": "high"},
        "output_errors": {"type": "integer", "label": "Output Errors", "min": 0,
                          "cloud_priority": "high"},
        # ── optics (SFP+ ports with a module fitted) ──
        "sfp_temperature_c": {"type": "number", "label": "SFP Temperature",
                              "unit": "C", "cloud_priority": "low"},
        "sfp_rx_power_dbm": {"type": "number", "label": "SFP Rx Power",
                             "unit": "dBm", "cloud_priority": "low"},
        "sfp_tx_power_dbm": {"type": "number", "label": "SFP Tx Power",
                             "unit": "dBm", "cloud_priority": "low"},
    }


class AVProEdgeMXnetSwitchDriver(BaseDriver):
    """AVPro Edge MXnet managed network switch (CLI over SSH or telnet)."""

    DRIVER_INFO = {
        "id": "avproedge_mxnet_switch",
        "name": "AVPro Edge MXnet Network Switch",
        "manufacturer": "AVPro Edge",
        "category": "utility",
        "version": "1.1.0",
        "author": "OpenAVC",
        # Computed by build_index.py, not chosen: the `web_ui` field below
        # carries a 0.24.0 floor. Well behind the current release, so this
        # locks nobody out.
        "min_platform_version": "0.24.0",
        "description": (
            "Monitor and control an AVPro Edge MXnet network switch over its "
            "CLI: per-port PoE power-cycling and draw, link state and error "
            "counters, IGMP/multicast group membership per port, the endpoint "
            "attached to each port, and system health (temperature, CPU, "
            "memory, PoE budget). Each physical port is a child entity. This "
            "is the switch that carries an MXnet system; video routing lives "
            "in the MXnet Control Box driver."
        ),
        "source_url": (
            "https://support.avproglobal.com/portal/en/kb/articles/"
            "mxnet-network-switch-guides"
        ),
        "tags": ["network", "switch", "poe", "av-over-ip", "igmp", "multicast",
                 "mxnet"],
        # Validated end to end against a real AC-MXNET-SW8P, including the
        # mutating PoE path against a live powered endpoint.
        "verified": True,
        "simulated": True,
        "protocols": ["mxnet_switch_cli"],
        "ports": [22, 23],
        "transport": "ssh",
        "transports": ["ssh", "tcp"],
        # The switch serves its web GUI on plain HTTP. Declared rather than
        # left to auto-detect because a bare "GET /" without a Host header gets
        # a 302 to http://localhost/index.html (a firmware quirk) -- a probe
        # could reasonably read that as unusable.
        "web_ui": "http://{host}",
        "discovery": {
            # Verified against an AC-MXNET-SW8P (software V705R002C013,
            # 2026-08-17):
            #   * The SSH server answers with the ident "SSH-2.0-SERVER_1.01"
            #     before any credential, so a banner-read probe on 22 (no send)
            #     identifies the CLI family. It is marked cross_vendor because
            #     that ident belongs to the DCN firmware stack this switch is
            #     built on, not to AVPro -- the matcher then uses the OUI below
            #     to pick this driver over any other switch on the same stack.
            #   * OUI 18:8a:6a is AVPro's MXnet block. It is shared with MXnet
            #     encoders and decoders, so on its own it means "MXnet gear",
            #     not "MXnet switch"; combined with the SSH ident it is decisive
            #     (endpoints run no SSH server).
            #   * The telnet banner is a bare "login:" and the HTTP body carries
            #     no vendor string, so neither is usable as a fingerprint.
            "tcp_probe": {
                "port": 22,
                "expect_regex": r"^SSH-2\.0-SERVER_",
                "cross_vendor": True,
                "timeout_ms": 3000,
            },
            "oui": ["18:8a:6a"],
            # Only 23. The switch also listens on 22 and 80, but the hint
            # parser rejects those as too generic to be evidence of anything
            # (they would match every web/SSH device on the subnet).
            "port_open": [23],
            "manufacturer_alias": ["avpro", "avpro edge", "avproglobal"],
        },
        "compatible_models": [
            {
                "manufacturer": "AVPro Edge",
                "models": ["AC-MXNET-SW8P", "AC-MXNET-SW12"],
                "confidence": "full",
                "notes": (
                    "Validated against an AC-MXNET-SW8P (software "
                    "V705R002C013): identity, all 12 ports, PoE draw and "
                    "enable/disable/power-cycle against a live encoder, IGMP "
                    "membership, MAC table, counters, and system health "
                    "exercised on hardware. SW12 is the same switch under its "
                    "former SKU."
                ),
            },
            {
                "manufacturer": "AVPro Edge",
                "models": ["AC-MXNET-SW24", "AC-MXNET-SW24P",
                           "AC-MXNET-SW48", "AC-MXNET-SW48P"],
                "confidence": "untested",
                "notes": (
                    "Same CLI and the same published command guide; the port "
                    "roster is read from the switch, so port count is not "
                    "assumed. Not yet validated on this hardware."
                ),
            },
        ],
        "help": {
            "overview": (
                "Controls an AVPro Edge MXnet network switch. Add it as one "
                "device; every physical port appears as a child entity under "
                "the Child Entities tab with its link state, PoE draw, error "
                "counters, and the multicast groups joined on it.\n\n"
                "This is the switch, not the matrix. Video routing in an MXnet "
                "system is done by the MXnet Control Box — add that separately "
                "with the MXnet 1G driver, where encoders and decoders appear "
                "as children. Use this driver to see why a route that should "
                "work doesn't: a port that is down, an endpoint drawing no PoE, "
                "or a decoder that never joined its stream's multicast group."
            ),
            "setup": (
                "1. Reach the switch. A factory switch answers on "
                "192.168.1.238 with a 255.255.255.0 mask and no default "
                "gateway, so your computer has to be on that same subnet to "
                "reach it the first time. If your network uses a different "
                "range, connect a computer directly to one of the switch's "
                "ports, give it an address like 192.168.1.50, and change the "
                "switch's address from there.\n"
                "2. Log in. The default username and password are both "
                "'admin'. SSH is already enabled from the factory.\n"
                "3. Add it here: enter the switch IP, leave Connection on "
                "'ssh' and the port on 22, and enter the username and "
                "password.\n"
                "4. Ports, PoE status, and attached endpoints are discovered "
                "automatically once it connects.\n\n"
                "Note: 192.168.1.238 is a common address for other equipment "
                "to hold. If the switch is unreachable after you connect it to "
                "your network, check that nothing else is using that address, "
                "and give the switch one of its own."
            ),
            "connection": (
                "Check that Connection and Port agree: 'tcp' needs port 23, "
                "'ssh' needs port 22. Then check the IP address, the username "
                "(default 'admin') and the password (default 'admin'). A "
                "factory switch has no default gateway configured, so it can "
                "only be reached from a computer on the same subnet as the "
                "switch — 192.168.1.x out of the box. If your network uses a "
                "different range, connect to the switch directly to change its "
                "address first."
            ),
        },
        "default_config": {
            "host": "",
            "port": 22,
            "username": "admin",
            "transport": "ssh",
            "ssh_auth_method": "password",
            "poll_interval": 15,
            "detail_poll_interval": 60,
            "poe_cycle_seconds": 4,
            # The SSH connect is validated by reading the CLI prompt in
            # _post_connect, so skip the generic post-open settle.
            "verify_timeout": 0,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address",
                     "description": "Factory default is 192.168.1.238."},
            "transport": {
                "type": "enum", "values": ["ssh", "tcp"], "default": "tcp",
                "label": "Connection",
                "description": "tcp = telnet on port 23 (default: works with "
                               "every switch out of the box). ssh = encrypted "
                               "CLI on port 22; the switch's SSH server offers "
                               "only the hmac-sha1 MAC, which OpenSSH 8.8 and "
                               "newer refuse by default, so ssh fails to "
                               "negotiate on most current systems. Remember to "
                               "change Port to match.",
            },
            "port": {
                "type": "integer", "default": 23, "min": 1, "max": 65535,
                "label": "Port",
                "description": "23 for telnet, 22 for SSH. Must match the "
                               "Connection setting above.",
            },
            "username": {"type": "string", "default": "admin",
                         "label": "Username",
                         "description": "Factory default is 'admin'."},
            "password": {"type": "password", "secret": True, "label": "Password",
                         "description": "Factory default is 'admin'."},
            "ssh_auth_method": {
                "type": "enum", "values": ["password", "key"],
                "default": "password", "label": "SSH Auth Method",
                "description": "The switch ships with password auth only. "
                               "Ignored for telnet.",
            },
            "key_path": {
                "type": "string", "label": "SSH Private Key Path",
                "description": "Path to the OpenAVC private key (key auth).",
            },
            "host_key_policy": {
                "type": "enum", "values": ["accept-new", "strict", "off"],
                "default": "accept-new", "label": "SSH Host Key Policy",
            },
            "poll_interval": {
                "type": "integer", "default": 15, "min": 0, "max": 3600,
                "label": "Status Poll Interval (sec)",
                "description": "How often to poll port link and PoE status. "
                               "0 disables.",
            },
            "detail_poll_interval": {
                "type": "integer", "default": 60, "min": 0, "max": 86400,
                "label": "Detail Poll Interval (sec)",
                "description": "How often to refresh system health, multicast "
                               "membership, attached endpoints, and port "
                               "counters. 0 disables the heavy refresh.",
            },
            "poe_cycle_seconds": {
                "type": "integer", "default": 4, "min": 1, "max": 60,
                "label": "PoE Power-Cycle Off Time (sec)",
                "description": "How long PoE stays off during a power-cycle. "
                               "Long enough for the powered device to fully "
                               "discharge.",
            },
        },
        "state_variables": {
            "model": {"type": "string", "label": "Model"},
            "firmware_version": {"type": "string", "label": "Firmware"},
            "boot_version": {"type": "string", "label": "Boot ROM"},
            "hardware_version": {"type": "string", "label": "Hardware"},
            "serial_number": {"type": "string", "label": "Serial Number"},
            "mac_address": {"type": "string", "label": "Base MAC"},
            "uptime": {"type": "string", "label": "Uptime"},
            "temperature_c": {"type": "integer", "label": "Temperature",
                              "unit": "C", "min": 0, "max": 100,
                              "cloud_priority": "high"},
            "cpu_usage_5s": {"type": "number", "label": "CPU 5s", "unit": "%",
                             "min": 0, "max": 100, "cloud_priority": "low"},
            "cpu_usage_30s": {"type": "number", "label": "CPU 30s", "unit": "%",
                              "min": 0, "max": 100, "cloud_priority": "low"},
            "cpu_usage_5m": {"type": "number", "label": "CPU 5m", "unit": "%",
                             "min": 0, "max": 100, "cloud_priority": "low"},
            "memory_total_mb": {"type": "integer", "label": "Memory Total",
                                "unit": "MB", "min": 0},
            "memory_usage_percent": {"type": "number", "label": "Memory Used",
                                     "unit": "%", "min": 0, "max": 100,
                                     "cloud_priority": "low"},
            # ── PoE ──
            "poe_status": {"type": "string", "label": "PoE Controller",
                           "cloud_priority": "high"},
            "poe_budget_w": {"type": "number", "label": "PoE Budget", "unit": "W",
                             "min": 0, "max": 1000},
            "poe_consumed_w": {"type": "number", "label": "PoE Consumed",
                               "unit": "W", "min": 0, "max": 1000,
                               "cloud_priority": "high"},
            "poe_remaining_w": {"type": "number", "label": "PoE Remaining",
                                "unit": "W", "min": 0, "max": 1000,
                                "cloud_priority": "high"},
            "poe_police": {"type": "enum", "values": ["enabled", "disabled"],
                           "label": "PoE Power Policing"},
            "poe_legacy": {"type": "enum", "values": ["enabled", "disabled"],
                           "label": "PoE Legacy PD Support"},
            "poe_pse_type": {"type": "string", "label": "PoE Controller Type"},
            "poe_ports_delivering": {"type": "integer",
                                     "label": "PoE Ports Delivering", "min": 0},
            # ── multicast / AV health ──
            "igmp_snooping": {"type": "boolean", "label": "IGMP Snooping",
                              "cloud_priority": "high"},
            "igmp_querier": {"type": "boolean", "label": "IGMP Querier",
                             "cloud_priority": "high"},
            "igmp_snooping_vlans": {"type": "string",
                                    "label": "IGMP Snooping VLANs"},
            "multicast_group_count": {"type": "integer",
                                      "label": "Multicast Groups", "min": 0,
                                      "cloud_priority": "high"},
            # ── inventory ──
            "vlan_count": {"type": "integer", "label": "VLANs", "min": 0},
            "port_count": {"type": "integer", "label": "Ports", "min": 0},
            "ports_up": {"type": "integer", "label": "Ports Up", "min": 0,
                         "cloud_priority": "high"},
            "mxnet_endpoints": {"type": "integer", "label": "MXnet Endpoints",
                                "min": 0, "cloud_priority": "high"},
        },
        "device_settings": {
            "poe_police": {
                "type": "enum",
                "values": [{"value": "enabled", "label": "Enabled"},
                           {"value": "disabled", "label": "Disabled"}],
                "label": "PoE Power Policing",
                "state_key": "poe_police",
                "default": "disabled",
                "setup": False,
                "help": "When enabled, the switch enforces per-port PoE "
                        "priority as the power budget fills up, shedding "
                        "low-priority ports first.",
            },
            "poe_legacy": {
                "type": "enum",
                "values": [{"value": "enabled", "label": "Enabled"},
                           {"value": "disabled", "label": "Disabled"}],
                "label": "PoE Legacy PD Support",
                "state_key": "poe_legacy",
                "default": "disabled",
                "setup": False,
                "help": "Power non-standard devices that the switch does not "
                        "detect as a valid PoE load. Turn on only if an "
                        "endpoint refuses to power up.",
            },
        },
        "child_entity_types": {
            "port": {
                "label": "Port",
                "label_plural": "Ports",
                # unit*10000 + slot*1000 + port, so a stacked switch's
                # member-2 port 1 can't collide with member-1 port 1.
                "id_format": {"type": "integer", "min": 1, "max": PORT_MAX,
                              "pad_width": 5},
                "state_variables": _port_state_vars(),
                "summary_fields": ["interface", "link_status", "speed",
                                   "poe_power_w", "connected_kind"],
                "label_field": "interface",
            },
        },
        "quick_actions": ["poe_cycle_port", "save_config"],
        "commands": {},  # populated by _build_commands() at module load
    }

    def __init__(self, device_id: str, config: dict[str, Any], state, events) -> None:
        self._rx_buffer = ""
        self._iac_buf = b""   # unparsed raw bytes (possible partial telnet IAC)
        self._responses: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._cmd_lock = asyncio.Lock()
        self._poll_count = 0
        super().__init__(device_id, config, state, events)

    # Raw byte pipe — the driver does its own prompt framing (SSH and telnet
    # alike), so no delimiter-based framing.
    def _resolve_delimiter(self) -> bytes | None:
        return None

    # ── CLI framing ──

    def _demux_telnet_iac(self, data: bytes) -> tuple[str, bytes]:
        """Pull Telnet IAC negotiation out of the raw byte stream.

        Returns the cleaned text plus any bytes owed to the peer (a refusal for
        every option offered). Partial sequences are buffered across chunks.
        SSH and the simulator never send IAC, so this fast-paths to a decode.
        """
        if _IAC not in data and not self._iac_buf:
            return data.decode("latin-1", errors="replace"), b""
        b = self._iac_buf + data
        i = 0
        cleaned = bytearray()
        refusals = bytearray()
        while i < len(b):
            c = b[i]
            if c != _IAC:
                cleaned.append(c)
                i += 1
                continue
            if i + 1 >= len(b):
                break  # partial IAC — wait for the next chunk
            cmd = b[i + 1]
            if cmd in (_DO, _DONT, _WILL, _WONT):
                if i + 2 >= len(b):
                    break  # partial option byte
                opt = b[i + 2]
                if cmd == _DO:
                    refusals += bytes([_IAC, _WONT, opt])
                elif cmd == _WILL:
                    refusals += bytes([_IAC, _DONT, opt])
                i += 3
                continue
            if cmd == _SB:                       # subnegotiation: skip to IAC SE
                end = b.find(bytes([_IAC, _SE]), i + 2)
                if end == -1:
                    break
                i = end + 2
                continue
            if cmd == _IAC:                      # escaped literal 0xFF
                cleaned.append(_IAC)
                i += 2
                continue
            i += 2                               # other 2-byte command
        self._iac_buf = b[i:]
        return cleaned.decode("latin-1", errors="replace"), bytes(refusals)

    async def on_data_received(self, data: bytes) -> None:
        """Accumulate the byte stream and emit one (text, boundary) unit each
        time the device pauses at a prompt or interactive sub-prompt.
        """
        text, refusals = self._demux_telnet_iac(data)
        if refusals:
            await self._safe_send(refusals)
        self._rx_buffer += _ANSI_RE.sub("", text)

        # Auto-answer the pager if it appears (terminal length 0 normally
        # prevents it). Any key advances a page on this CLI; use a space.
        if _PAGER_RE.search(self._rx_buffer):
            self._rx_buffer = _PAGER_STRIP_RE.sub("", self._rx_buffer)
            self._bg(self._safe_send(b" "))
            return

        # Order matters: the login/password/confirm sub-prompts are checked
        # before the generic prompt so a password prompt is never mistaken for
        # the CLI being ready.
        for regex, kind in ((_LOGIN_RE, "login"),
                            (_PASSWORD_RE, "password"),
                            (_CONFIRM_RE, "confirm"),
                            (_PROMPT_RE, "prompt")):
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

    async def _send_request(self, wire: str, timeout: float = 10.0) -> str:
        """Send one command line and return its response text (echo + prompt
        stripped). Serialised so responses correlate to commands.
        """
        async with self._cmd_lock:
            self._clear()
            text, _kind = await self._exchange(wire, timeout)
            return text

    async def _send_sequence(self, lines: list[str], timeout: float = 10.0) -> str:
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
        """config -> interface ethernet <iface> -> <lines> -> exit -> exit."""
        return await self._send_sequence(
            ["config", f"interface ethernet {iface}", *lines, "exit", "exit"]
        )

    async def _global_config(self, lines: list[str]) -> str:
        """config -> <lines> -> exit."""
        return await self._send_sequence(["config", *lines, "exit"])

    # ── connection setup ──

    async def _post_connect(self) -> None:
        """Read the banner, log in if asked, and disable paging. Runs (via
        BaseDriver.connect) right after the transport opens and before the
        device is reported connected.
        """
        self._iac_buf = b""   # fresh telnet IAC state for this (re)connect
        try:
            _text, kind = await asyncio.wait_for(self._responses.get(), timeout=20.0)
        except asyncio.TimeoutError as e:
            err = getattr(self.transport, "last_error", "") or ""
            raise ConnectionError(
                f"[{self.device_id}] No CLI prompt from "
                f"{self.config.get('host', '?')}"
                + (f" ({err.splitlines()[-1]})" if err else "")
            ) from e

        # Telnet presents "login:" then "Password:". SSH password auth is done
        # by the transport, so it lands straight at the CLI prompt. The
        # simulator skips login entirely.
        if kind == "login":
            kind = await self._login_reply(self.config.get("username", ""))
        if kind == "password":
            kind = await self._login_reply(self.config.get("password", ""))
        if kind in ("login", "password"):
            # Re-prompted instead of dropping to the CLI: the credentials were
            # rejected. Say so rather than hanging on the first query.
            raise ConnectionError(
                f"[{self.device_id}] Login failed — the switch re-prompted for "
                f"credentials. Check the username and password (the factory "
                f"default is admin / admin)."
            )

        # Non-stop output for this session (0 = no pager).
        try:
            await self._send_request("terminal length 0", timeout=8.0)
        except Exception:
            log.debug(f"[{self.device_id}] terminal length 0 not accepted",
                      exc_info=True)

        await self._read_identity()

    async def _login_reply(self, value: str, timeout: float = 12.0) -> str:
        """Send one credential and return the boundary kind of the next pause —
        "login"/"password" if the switch re-prompts (rejected), "prompt" once
        the CLI is reached.
        """
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self.transport.send((value + "\r\n").encode("ascii", "replace"))
        try:
            _text, kind = await asyncio.wait_for(
                self._responses.get(), timeout=timeout)
        except asyncio.TimeoutError as e:
            raise ConnectionError(
                f"[{self.device_id}] No response after sending a login credential"
            ) from e
        return kind

    async def _read_identity(self) -> None:
        """show version -> model / firmware / serial / base MAC."""
        try:
            resp = await self._send_request("show version", timeout=10.0)
        except Exception:
            log.debug(f"[{self.device_id}] show version failed", exc_info=True)
            return
        self._set_declared(parse_version(resp))

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
        """Port link + PoE into device state and port children.

        The primary queries are unwrapped so transport errors propagate to the
        poll watchdog (a swallowed error would let `connected` lie).
        """
        status = parse_interface_status(
            await self._send_request("show interface ethernet status"))
        poe = parse_poe_ports(
            await self._send_request("show power inline interface"))

        merged: dict[str, dict[str, Any]] = {}
        for iface, props in status.items():
            merged.setdefault(iface, {}).update(props)
        for iface, props in poe.items():
            merged.setdefault(iface, {}).update(props)

        self._reconcile_ports(merged)
        self.set_state("port_count", len(self.list_children("port")))
        self.set_state("ports_up",
                       sum(1 for p in merged.values()
                           if p.get("link_status") == "up"))
        self.set_state("poe_ports_delivering",
                       sum(1 for p in merged.values()
                           if str(p.get("poe_status", "")).lower() == "on"))

        await self._guarded("show power inline",
                            lambda r: self._set_declared(parse_poe_global(r)))

    async def _poll_slow(self) -> None:
        """System health, multicast membership, attached endpoints, counters.

        Best-effort per query, but transport errors still propagate so the
        watchdog sees a dead session.
        """
        await self._guarded("show version",
                            lambda r: self._set_declared(parse_version(r)))
        await self._guarded("show temperature",
                            lambda r: self._set_declared(parse_temperature(r)))
        await self._guarded("show cpu usage",
                            lambda r: self._set_declared(parse_cpu(r)))
        await self._guarded("show memory usage",
                            lambda r: self._set_declared(parse_memory(r)))
        await self._guarded("show ip igmp snooping",
                            lambda r: self._set_declared(parse_igmp_snooping(r)))
        await self._guarded("show vlan",
                            lambda r: self.set_state("vlan_count",
                                                     parse_vlan_count(r)))
        await self._guarded("show mac-address-table", self._apply_mac_table)
        await self._apply_multicast()
        await self._guarded("show transceiver", self._apply_transceivers)
        rng = self._port_range()
        if rng:
            await self._guarded(f"show interface ethernet {rng}",
                                self._apply_counters)

    def _port_range(self) -> str:
        """A single 'unit/slot/first-last' range covering every known port.

        The CLI accepts a range, so all twelve ports' counters come back in one
        round trip instead of twelve.
        """
        ifaces = [self.get_child_state("port", cid).get("interface", "")
                  for cid in self.list_children("port")]
        parts = [p for p in (_iface_parts(i) for i in ifaces) if p]
        if not parts:
            return ""
        units = {(u, s) for u, s, _ in parts}
        if len(units) != 1:
            # A stacked switch spans units; fall back to no range rather than
            # emitting one that silently covers only part of the roster.
            return ""
        unit, slot = units.pop()
        ports = sorted(p for _, _, p in parts)
        if ports[0] == ports[-1]:
            return f"{unit}/{slot}/{ports[0]}"
        return f"{unit}/{slot}/{ports[0]}-{ports[-1]}"

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

    # ── applying parsed data to children ──

    def _child_schema(self) -> dict[str, Any]:
        return self.get_child_entity_types()["port"]["state_variables"]

    def _apply_mac_table(self, resp: str) -> None:
        """Attach the MAC seen on each port, and count MXnet endpoints.

        A port with several MACs behind it (a downstream switch) reports the
        count rather than picking one arbitrarily.
        """
        table = parse_mac_table(resp)
        mxnet = 0
        for cid in self.list_children("port"):
            iface = self.get_child_state("port", cid).get("interface", "")
            macs = table.get(iface, [])
            if len(macs) == 1:
                mac = macs[0]
                self.set_child_state_batch("port", cid, {
                    "connected_mac": mac,
                    "connected_kind": _endpoint_kind(mac),
                })
            elif len(macs) > 1:
                self.set_child_state_batch("port", cid, {
                    "connected_mac": "",
                    "connected_kind": f"{len(macs)} devices",
                })
            else:
                self.set_child_state_batch("port", cid, {
                    "connected_mac": "", "connected_kind": "",
                })
            mxnet += sum(1 for m in macs if m.startswith(_MXNET_OUI))
        self.set_state("mxnet_endpoints", mxnet)

    async def _apply_multicast(self) -> None:
        """Per-port IGMP group membership, across every snooped VLAN.

        The membership table is per-VLAN, so which VLANs to ask for comes from
        the global snooping state read moments earlier.
        """
        vlans = [v.strip() for v in
                 str(self.get_state("igmp_snooping_vlans") or "1").split(",")
                 if v.strip().isdigit()] or ["1"]
        per_iface: dict[str, set[str]] = {}
        groups: set[str] = set()
        for vlan in vlans[:16]:   # bounded: a huge VLAN list is not worth a poll
            try:
                resp = await self._send_request(
                    f"show ip igmp snooping vlan {vlan}")
            except (ConnectionError, TimeoutError, OSError):
                raise
            except Exception:
                log.debug(f"[{self.device_id}] igmp snooping vlan {vlan} failed",
                          exc_info=True)
                continue
            for row in parse_igmp_groups(resp):
                groups.add(row["group"])
                per_iface.setdefault(row["interface"], set()).add(row["group"])
        self.set_state("multicast_group_count", len(groups))
        for cid in self.list_children("port"):
            iface = self.get_child_state("port", cid).get("interface", "")
            joined = sorted(per_iface.get(iface, set()))
            self.set_child_state_batch("port", cid, {
                "multicast_groups": len(joined),
                "multicast_group_list": ", ".join(joined),
            })

    def _apply_transceivers(self, resp: str) -> None:
        schema = self._child_schema()
        readings = parse_transceivers(resp)
        for cid in self.list_children("port"):
            iface = self.get_child_state("port", cid).get("interface", "")
            props = readings.get(iface)
            if props:
                self.set_child_state_batch(
                    "port", cid, {k: v for k, v in props.items() if k in schema})

    def _apply_counters(self, resp: str) -> None:
        schema = self._child_schema()
        for iface, props in parse_port_counters(resp).items():
            cid = _iface_to_id(iface)
            if cid is not None and self.is_child_registered("port", cid):
                clean = {k: v for k, v in props.items() if k in schema}
                if clean:
                    self.set_child_state_batch("port", cid, clean)

    def _reconcile_ports(self, merged: dict[str, dict[str, Any]]) -> None:
        """Register newly-seen ports, update known ones, drop any that vanished."""
        schema = self._child_schema()
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
            return await self._save_config()
        if command == "poe_cycle_port":
            return await self._poe_cycle_port(params)
        if command == "clear_port_counters":
            iface = self._iface_for(params)
            return await self._send_confirmable(
                f"clear counters interface ethernet {iface}")
        if command == "cable_test":
            iface = self._iface_for(params)
            return await self._send_request(
                f"virtual-cable-test interface ethernet {iface}", timeout=30.0)
        if command in _PORT_COMMANDS:
            return await self._port_command(command, params)
        raise ValueError(f"Unknown command: {command}")

    def _iface_for(self, params: dict[str, Any]) -> str:
        cid = int(params["port"])
        iface = self.get_child_state("port", cid).get("interface")
        if not iface:
            raise ValueError(f"Port {cid} is not a known interface")
        return iface

    async def _port_command(self, command: str, params: dict[str, Any]) -> str:
        iface = self._iface_for(params)
        spec = _PORT_COMMANDS[command]
        if derive := spec.get("derive"):
            params.update(derive(params))
        lines = [line.format(**params) for line in spec["lines"]]
        resp = await self._interface_config(iface, lines)
        # Optimistic state so the UI reflects the change before the next poll.
        if spec.get("optimistic"):
            self.set_child_state_batch("port", int(params["port"]),
                                       spec["optimistic"](params))
        return resp

    async def _poe_cycle_port(self, params: dict[str, Any]) -> str:
        """Power-cycle a port's PoE by cutting power and restoring it.

        NOT ``power inline reset``: on SW8P firmware V705R002C013 that command
        is accepted and never interrupts power (measured against a live 4 W
        encoder — power, 52 V and class 3 all held, and the link never
        dropped). Cutting the port is what actually reboots the endpoint.
        """
        iface = self._iface_for(params)
        cid = int(params["port"])
        off_seconds = max(1, min(60, _to_int(
            self.config.get("poe_cycle_seconds", 4), 4)))
        await self._interface_config(iface, ["no power inline enable"])
        self.set_child_state_batch("port", cid, {
            "poe_admin": "disabled", "poe_status": "off", "poe_power_w": 0.0,
        })
        try:
            await asyncio.sleep(off_seconds)
        finally:
            # Restore power even if the wait is cancelled — leaving an endpoint
            # dark because a task was torn down would be the worst outcome here.
            await self._interface_config(iface, ["power inline enable"])
            self.set_child_state_batch("port", cid, {"poe_admin": "enabled"})
        return f"PoE power-cycled on {iface} ({off_seconds}s off)"

    async def _save_config(self) -> str:
        """``write`` — persist running-config to startup-config."""
        resp = await self._send_confirmable("write", timeout=30.0)
        return resp or "Configuration saved"

    async def _reboot(self) -> str:
        """``reload``, answering the confirmation prompt."""
        async with self._cmd_lock:
            self._clear()
            _text, kind = await self._exchange("reload", timeout=12.0)
            if kind == "confirm":
                await self.transport.send(b"y\r\n")
            return "Reboot requested"

    async def _send_confirmable(self, wire: str, timeout: float = 15.0) -> str:
        """Send a command, answering a single confirmation if one is asked."""
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

    # ── device settings (device-level, with offline pending queue) ──

    async def set_device_setting(self, key: str, value: Any) -> Any:
        enabled = str(value).strip().lower() in ("enabled", "enable", "on", "true", "1")
        if key == "poe_police":
            await self._global_config(
                ["power inline police enable" if enabled
                 else "no power inline police enable"])
            self.set_state("poe_police", "enabled" if enabled else "disabled")
            return
        if key == "poe_legacy":
            await self._global_config(
                ["power inline legacy enable" if enabled
                 else "no power inline legacy enable"])
            self.set_state("poe_legacy", "enabled" if enabled else "disabled")
            return
        raise ValueError(f"Unknown device setting: {key}")


# ────────────────────────── command surface ──────────────────────────

# Per-port commands: interface-config line templates + optional optimistic state.
_PORT_COMMANDS: dict[str, dict[str, Any]] = {
    "poe_enable_port": {
        "lines": ["power inline enable"],
        "optimistic": lambda p: {"poe_admin": "enabled"},
    },
    "poe_disable_port": {
        "lines": ["no power inline enable"],
        "optimistic": lambda p: {"poe_admin": "disabled", "poe_status": "off",
                                 "poe_power_w": 0.0},
    },
    "port_enable": {
        "lines": ["no shutdown"],
        "optimistic": lambda p: {"admin_status": "enabled"},
    },
    "port_disable": {
        "lines": ["shutdown"],
        "optimistic": lambda p: {"admin_status": "disabled",
                                 "link_status": "down"},
    },
    "set_poe_priority": {
        "lines": ["power inline priority {priority}"],
        "optimistic": lambda p: {"poe_priority": str(p["priority"]).lower()},
    },
    "set_poe_max_power": {
        # The CLI takes milliwatts; the parameter is watts because that is what
        # an endpoint's spec sheet quotes.
        "lines": ["power inline max {milliwatts}"],
        "derive": lambda p: {"milliwatts": int(round(float(p["watts"]) * 1000))},
        "optimistic": lambda p: {"poe_max_power_w": float(p["watts"])},
    },
    "set_port_description": {
        "lines": ['description {description}'],
        "optimistic": lambda p: {"description": str(p["description"])},
    },
}


def _port_param() -> dict[str, Any]:
    return {"type": "child_id", "child_type": "port", "required": True,
            "label": "Port"}


def _build_commands() -> dict[str, dict[str, Any]]:
    return {
        "poe_cycle_port": {
            "label": "Power-Cycle PoE Port",
            "params": {"port": _port_param()},
            "help": "Cut PoE on the port and restore it, to reboot a frozen "
                    "encoder, decoder, or other powered device.",
        },
        "poe_enable_port": {
            "label": "Enable PoE on Port",
            "params": {"port": _port_param()},
            "help": "Turn PoE on for the port.",
        },
        "poe_disable_port": {
            "label": "Disable PoE on Port",
            "params": {"port": _port_param()},
            "help": "Turn PoE off for the port. The attached device loses power.",
        },
        "set_poe_priority": {
            "label": "Set PoE Priority",
            "params": {
                "port": _port_param(),
                "priority": {"type": "enum",
                             "values": ["critical", "high", "low"],
                             "required": True, "label": "Priority"},
            },
            "help": "Set the port's PoE priority, used to decide which ports "
                    "keep power when the budget is tight.",
        },
        "set_poe_max_power": {
            "label": "Set PoE Max Power",
            "params": {
                "port": _port_param(),
                "watts": {"type": "number", "required": True, "label": "Max Power",
                          "unit": "W", "min": 0, "max": 90},
            },
            "help": "Cap how much power the port may deliver.",
        },
        "port_enable": {
            "label": "Enable Port",
            "params": {"port": _port_param()},
            "help": "Bring the port up (no shutdown).",
        },
        "port_disable": {
            "label": "Disable Port",
            "params": {"port": _port_param()},
            "help": "Shut the port down. Traffic and PoE both stop.",
        },
        "set_port_description": {
            "label": "Set Port Description",
            "params": {
                "port": _port_param(),
                "description": {"type": "string", "required": True,
                                "label": "Description",
                                "pattern": r"^[A-Za-z0-9 _.\-]{1,64}$"},
            },
            "help": "Label the port on the switch, so it is identifiable from "
                    "the switch's own CLI and web GUI too.",
        },
        "clear_port_counters": {
            "label": "Clear Port Counters",
            "params": {"port": _port_param()},
            "help": "Reset the port's traffic and error counters, to see "
                    "whether errors are still accumulating.",
        },
        "cable_test": {
            "label": "Cable Test",
            "params": {"port": _port_param()},
            "help": "Run cable diagnostics on the port. This briefly interrupts "
                    "the link.",
        },
        "save_config": {
            "label": "Save Configuration",
            "params": {},
            "help": "Save the running configuration so changes survive a reboot.",
        },
        "reboot": {
            "label": "Reboot Switch",
            "params": {},
            "help": "Restart the switch. Every connected device loses its "
                    "network link and PoE power while it restarts.",
        },
    }


AVProEdgeMXnetSwitchDriver.DRIVER_INFO["commands"] = _build_commands()
