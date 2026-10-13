"""NETIO PowerPDU, PowerBOX, PowerCable and PowerDIN driver (JSON over HTTP).

NETIO's networked power sockets, rack PDUs and DIN-rail units all run the same
"universal" firmware and all speak NETIO's JSON M2M API: one status document,
``netio.json``, read with an HTTP GET and written with an HTTP POST of the
same document carrying only the changes. A write is answered with the whole
status document again, so every command reads its own result back.

What the document carries:

  Agent          model, name, MAC, serial number, firmware, JSON version,
                 clock, uptime, output and input counts
  GlobalMeasure  voltage, frequency, total current, load, power factor and
                 phase, energy counters (metered models only)
  Outputs        name, on/off, short on/off time, and on a metered output its
                 current, load, power factor, phase, voltage, frequency and
                 energy counters
  Inputs         open/closed, S0 pulse counter, temperature and humidity from
                 a NETIO sensor
  PAB, Watchdogs, Rules
                 the unit's own automation (power analysis blocks, IP
                 watchdogs, condition rules): name, enabled, latest result

Outputs are switched on, off and over (toggle), rebooted (short off), switched
on for a time (short on), renamed, have their energy counter reset and their
schedule enabled or disabled. That is every write the document defines.

**Polling, deliberately.** NETIO's push protocols do not report an output
switching. Netio Push sends this same document on a fixed period (60 s by
default) and at once only when current moves past a set delta; MQTT-flex
publishes to a broker the site has to run. Both are set up only in the unit's
own web interface, never over this API. One GET returns everything, so a poll
every few seconds carries the same information on the same schedule.

**Why Python, not YAML.**
  * The JSON API takes one login for reading and another for writing (its
    settings page has both, and NETIO's public demo units read with no login
    and write only with one). A YAML HTTP driver has one login for every
    request.
  * The unit's own automation is a list of named entries the unit decides
    (watchdogs, rules, power analysis blocks). They are child entities keyed
    by name here; YAML cannot build a roster the device enumerates.
  * A write with its own short on/off time must end its HTTP connection (see
    ``_write``), and a refused write's error body is not valid JSON.

Source: NETIO "JSON over HTTP NETIO M2M API protocols", JSON version 2.6,
published 2026-02-24. Checked on 2026-10-13 against NETIO's public demo units
(PowerPDU 8QS, 4PS and 4KS, PowerDIN 4PZ, PowerCable 2PZ, PowerBOX 4KF, on
firmware 5.3.1 and 5.3.2). Where the units and the document disagree the code
follows the units, and says so where it reads the value.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.utils.logger import get_logger

log = get_logger(__name__)

STATUS_PATH = "/netio.json"

# "Not available": a sensor that is not connected, a figure the unit does not
# measure (a power factor with no load on a 4KS reads 999.00).
SENTINEL = 999.0
# Humidity with no sensor reads 999 on the demo units; the document's own
# listings show 9990. Both mean the same thing.
HUMIDITY_ABSENT = (999.0, 9990.0)

# Output actions (document, "Output actions"). 6 is what a read returns
# ("ignored"), never sent.
ACTION_OFF = 0
ACTION_ON = 1
ACTION_SHORT_OFF = 2
ACTION_SHORT_ON = 3
ACTION_TOGGLE = 4
ACTION_NO_CHANGE = 5
ACTION_SCHEDULE_OFF = 20
ACTION_SCHEDULE_ON = 21

# The largest NETIO unit has eight outputs and two inputs; these leave room for
# a model that does not exist yet without accepting nonsense.
MAX_OUTPUTS = 24
MAX_INPUTS = 8

# A string child id takes letters, digits, '_' and '-' only.
_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


class NetioHTTPError(Exception):
    """The unit answered with an HTTP error status."""

    def __init__(self, status: int, message: str = "") -> None:
        self.status = status
        self.message = message
        super().__init__(f"HTTP {status}" + (f" ({message})" if message else ""))


class NetioReplyError(Exception):
    """The unit answered 200 with something that is not its status document."""


class NetioCommandError(RuntimeError):
    """A command the unit refused, or one that cannot be sent to this unit.

    A RuntimeError so the platform reports the sentence as the command's
    failure rather than reading it as a missing device.
    """


# ── Value readers ────────────────────────────────────────────────────────────


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _measure(value: Any) -> float | None:
    """A measured figure, or None where the unit reports 999 (not available)."""
    number = _as_float(value)
    if number is None or number == SENTINEL:
        return None
    return number


def _milliamps(value: Any) -> float | None:
    """The unit reports current in mA; the state is amps."""
    number = _as_float(value)
    if number is None:
        return None
    return round(number / 1000.0, 3)


def _bit(value: Any) -> bool | None:
    """An output or input State: 1 or 0."""
    number = _as_int(value)
    if number is None:
        return None
    return number == 1


def _timestamp(value: Any) -> str | None:
    """A watchdog's Timestamp as the unit's own clock reads it; 0 means never
    checked.

    The document calls it a "Linux timestamp", but on the demo units it counts
    the unit's local wall-clock time as if it were UTC: a check made at 16:06
    UTC on a unit whose clock read 18:06 carried 18:06. So it is shown as a
    clock reading with no zone, comparable with the unit's own Device Clock.
    """
    seconds = _as_int(value)
    if not seconds or seconds < 0:
        return None
    try:
        moment = datetime.fromtimestamp(seconds, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def child_key(name: str, taken: set[str]) -> str:
    """The child id for a named entry: its name with anything a child id cannot
    hold turned into '_', made unique against ``taken``."""
    base = _ID_UNSAFE.sub("_", str(name).strip()).strip("_")[:60] or "entry"
    key = base
    n = 2
    while key in taken:
        key = f"{base}-{n}"
        n += 1
    taken.add(key)
    return key


# ── Reply parsing ────────────────────────────────────────────────────────────


def parse_error(text: str) -> str:
    """The message in an error answer, or "".

    The document shows ``{"errors": [{"code", "severity", "message"}]}``. The
    demo units answer a refused write with the same list but no enclosing
    braces (``"errors":[{"code":400,"message":"Bad request"}]``), which is not
    valid JSON on its own, so both forms are read.
    """
    text = (text or "").strip()
    if not text:
        return ""
    doc: Any = None
    for candidate in (text, "{" + text + "}"):
        try:
            doc = json.loads(candidate)
            break
        except ValueError:
            continue
    if isinstance(doc, dict) and isinstance(doc.get("errors"), list):
        messages = [
            str(e.get("message")).strip()
            for e in doc["errors"]
            if isinstance(e, dict) and e.get("message")
        ]
        return "; ".join(m for m in messages if m)
    # A plain-text refusal ("401 Unauthorized") is short; keep it.
    if len(text) <= 80 and "<" not in text:
        return text
    return ""


def parse_status(text: str) -> dict[str, Any]:
    """The status document as a dict. Raises NetioReplyError when the reply is
    not JSON or carries no Agent block (something other than a NETIO answered,
    or the request reached the wrong service on the port)."""
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise NetioReplyError("the reply was not JSON") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("Agent"), dict):
        raise NetioReplyError("the reply has no Agent block")
    return doc


def parse_agent(agent: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    """Device identity. NumInputs is absent on the oldest firmware (a PowerBOX
    3Px on JSON 2.3), where the Inputs list decides."""
    inputs = doc.get("Inputs")
    outputs = doc.get("Outputs")
    num_outputs = _as_int(agent.get("NumOutputs"))
    num_inputs = _as_int(agent.get("NumInputs"))
    return {
        "model": _as_str(agent.get("Model")),
        "device_name": _as_str(agent.get("DeviceName")),
        "mac_address": _as_str(agent.get("MAC")),
        # The document: "When both tags available, use the SerialNumber, it's
        # identical with printed label on the device." Old firmware has MAC only.
        "serial_number": _as_str(agent.get("SerialNumber") or agent.get("MAC")),
        "firmware": _as_str(agent.get("Version")),
        "json_version": _as_str(agent.get("JSONVer")),
        "device_time": _as_str(agent.get("Time")),
        "uptime_s": _as_int(agent.get("Uptime")),
        "output_count": num_outputs if num_outputs is not None
        else (len(outputs) if isinstance(outputs, list) else 0),
        "input_count": num_inputs if num_inputs is not None
        else (len(inputs) if isinstance(inputs, list) else 0),
    }


def parse_global(measure: Any) -> dict[str, Any]:
    """Whole-unit metering. Absent on an unmetered model (4PS, 2PZ, 3Px).

    Frequency is absent from this block on the 8QS and the PowerDIN 4PZ
    (firmware 5.3.1), present on the 4KS and 4KF; the document's 8QS listing
    has it. The Total* power factor and phase replace the Overall* ones, which
    the document keeps "for historical compatibility only"; the oldest firmware
    has only OverallPowerFactor and Phase, read as a fallback.
    """
    if not isinstance(measure, dict):
        return {
            "metered": False,
            "voltage_v": None, "frequency_hz": None, "total_current_a": None,
            "total_load_w": None, "total_power_factor": None,
            "total_phase_deg": None, "total_energy_wh": None,
            "total_energy_nr_wh": None, "total_reverse_energy_wh": None,
            "total_reverse_energy_nr_wh": None, "energy_start": None,
        }
    power_factor = measure.get("TotalPowerFactor", measure.get("OverallPowerFactor"))
    phase = measure.get("TotalPhase", measure.get("Phase"))
    return {
        "metered": True,
        "voltage_v": _measure(measure.get("Voltage")),
        "frequency_hz": _measure(measure.get("Frequency")),
        "total_current_a": _milliamps(measure.get("TotalCurrent")),
        "total_load_w": _measure(measure.get("TotalLoad")),
        "total_power_factor": _measure(power_factor),
        "total_phase_deg": _measure(phase),
        "total_energy_wh": _as_int(measure.get("TotalEnergy")),
        "total_energy_nr_wh": _as_int(measure.get("TotalEnergyNR")),
        "total_reverse_energy_wh": _as_int(measure.get("TotalReverseEnergy")),
        "total_reverse_energy_nr_wh": _as_int(measure.get("TotalReverseEnergyNR")),
        "energy_start": _as_str(measure.get("EnergyStart")),
    }


def parse_output(entry: dict[str, Any]) -> dict[str, Any]:
    """One output. Metering keys are present only on a metered output: every
    output of a K model, output 1 of an 8QS, outputs 1 and 2 of a PowerDIN 4PZ
    (its 3 and 4 are relay contacts). The K models also report the output's
    own Voltage and Frequency, which the document's table leaves out."""
    metered = "Current" in entry or "Load" in entry
    values: dict[str, Any] = {
        "name": _as_str(entry.get("Name")),
        "state": _bit(entry.get("State")),
        "delay_ms": _as_int(entry.get("Delay")),
        "metered": metered,
        "voltage_v": None, "frequency_hz": None, "current_a": None,
        "load_w": None, "power_factor": None, "phase_deg": None,
        "energy_wh": None, "energy_nr_wh": None, "reverse_energy_wh": None,
        "reverse_energy_nr_wh": None, "energy_start": None,
    }
    if metered:
        values.update({
            "voltage_v": _measure(entry.get("Voltage")),
            "frequency_hz": _measure(entry.get("Frequency")),
            "current_a": _milliamps(entry.get("Current")),
            "load_w": _measure(entry.get("Load")),
            "power_factor": _measure(entry.get("PowerFactor")),
            "phase_deg": _measure(entry.get("Phase")),
            "energy_wh": _as_int(entry.get("Energy")),
            "energy_nr_wh": _as_int(entry.get("EnergyNR")),
            "reverse_energy_wh": _as_int(entry.get("ReverseEnergy")),
            "reverse_energy_nr_wh": _as_int(entry.get("ReverseEnergyNR")),
            "energy_start": _as_str(entry.get("EnergyStart")),
        })
    return values


def parse_input(entry: dict[str, Any]) -> dict[str, Any]:
    """One digital input. State 1 is a closed contact and 0 an open one (both
    places the document defines it agree on that, and disagree on which one it
    calls ON). One of the document's listings spells TempertureC; the units send
    TemperatureC, and either is read."""
    humidity = _as_float(entry.get("Humidity"))
    if humidity in HUMIDITY_ABSENT:
        humidity = None
    return {
        "name": _as_str(entry.get("Name")),
        "closed": _bit(entry.get("State")),
        "s0_counter": _as_int(entry.get("S0Counter")),
        "temperature_c": _measure(entry.get("TemperatureC", entry.get("TempertureC"))),
        "temperature_f": _measure(entry.get("TemperatureF")),
        "humidity": humidity,
    }


def parse_watchdog(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": _as_str(entry.get("Name")),
        "enabled": _as_bool(entry.get("Enabled")),
        "failed": _as_bool(entry.get("Fail")),
        "last_ok": _as_bool(entry.get("LastStatus")),
        "last_check": _timestamp(entry.get("Timestamp")),
    }


def parse_rule(entry: dict[str, Any]) -> dict[str, Any]:
    """Result is null until the rule has been evaluated (a disabled rule)."""
    return {
        "name": _as_str(entry.get("Name")),
        "enabled": _as_bool(entry.get("Enabled")),
        "result": _as_bool(entry.get("Result")),
    }


def parse_pab(entry: dict[str, Any]) -> dict[str, Any]:
    """A power analysis block. The document defines Type RANGE with an In
    result; the 4KS demo also carries Type ZONES, which reports a Zone number
    instead."""
    return {
        "name": _as_str(entry.get("Name")),
        "pab_type": _as_str(entry.get("Type")),
        "enabled": _as_bool(entry.get("Enabled")),
        "in_range": _as_bool(entry.get("In")) if "In" in entry else None,
        "zone": _as_int(entry.get("Zone")) if "Zone" in entry else None,
    }


def numbered(entries: Any, limit: int, parse) -> dict[int, dict[str, Any]]:
    """Outputs or inputs by their ID, in order. An entry with no usable ID, or
    one past ``limit``, is left out."""
    found: dict[int, dict[str, Any]] = {}
    if not isinstance(entries, list):
        return found
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        local_id = _as_int(entry.get("ID"))
        if local_id is None or not 1 <= local_id <= limit:
            continue
        found[local_id] = parse(entry)
    return dict(sorted(found.items()))


def named(entries: Any, parse) -> dict[str, dict[str, Any]]:
    """Watchdogs, rules or PABs keyed by a child id made from each name."""
    found: dict[str, dict[str, Any]] = {}
    if not isinstance(entries, list):
        return found
    taken: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("Name"):
            continue
        found[child_key(str(entry["Name"]), taken)] = parse(entry)
    return found


def summarize(doc: dict[str, Any]) -> dict[str, Any]:
    """Everything the document reports, split by where it goes."""
    device = parse_agent(doc["Agent"], doc)
    device.update(parse_global(doc.get("GlobalMeasure")))
    return {
        "device": device,
        "output": numbered(doc.get("Outputs"), MAX_OUTPUTS, parse_output),
        "input": numbered(doc.get("Inputs"), MAX_INPUTS, parse_input),
        "watchdog": named(doc.get("Watchdogs"), parse_watchdog),
        "rule": named(doc.get("Rules"), parse_rule),
        "pab": named(doc.get("PAB"), parse_pab),
    }


# ── Driver ───────────────────────────────────────────────────────────────────


CHILD_TYPES = ("output", "input", "watchdog", "rule", "pab")



class NetioDriver(BaseDriver):
    """NETIO power units over the JSON M2M API."""

    DRIVER_INFO = {
        "id": "netio",
        "name": "NETIO PowerPDU, PowerBOX, PowerCable and PowerDIN",
        "manufacturer": "NETIO",
        "category": "power",
        "version": "1.0.0",
        "author": "OpenAVC",
        # confirm on the commands that reset or switch everything needs 0.36.0.
        "min_platform_version": "0.36.0",
        "description": (
            "Controls NETIO rack PDUs, smart power strips, power cables and DIN-rail units over "
            "NETIO's JSON API: switch each output on or off, toggle it, reboot it or turn it on for "
            "a set time, and read each output's load, current, power factor and energy on metered "
            "models. Digital inputs, NETIO temperature and humidity sensors, and the unit's own "
            "watchdogs, rules and power analysis blocks are reported as live values."
        ),
        "source_url": "https://www.netio-products.com/files/M2M-HTTP-JSON_NETIO-API-protocol_v2-6_.pdf",
        "tags": ["pdu", "power", "outlet-control", "metering", "energy", "rack", "power-strip", "din-rail"],
        "verified": False,
        "simulated": True,
        "protocols": ["netio-json"],
        "ports": [80],
        "transport": "http",
        "discovery": {
            # Answers only on a unit whose JSON API reads with no login. A unit
            # with NETIO's factory login (netio / netio for reading) refuses the
            # probe, and the MAC block below marks it as a possible NETIO.
            "tcp_probe": {
                "port": 80,
                "send_ascii": "GET /netio.json HTTP/1.0\r\n\r\n",
                "expect_regex": r'"Agent"\s*:\s*\{\s*"Model"\s*:',
                "timeout_ms": 1500,
                "extract": {
                    "model": {"regex": r'"Model"\s*:\s*"([^"]+)"'},
                    "device_name": {"regex": r'"DeviceName"\s*:\s*"([^"]+)"'},
                    "firmware": {"regex": r'"Version"\s*:\s*"([^"]+)"'},
                    "serial": {"regex": r'"SerialNumber"\s*:\s*"([^"]+)"'},
                },
            },
            # NETIO products a.s. (IEEE MA-L 24A42C). Every demo unit's MAC is in it.
            "oui": ["24:a4:2c"],
            "manufacturer_alias": ["netio", "netio products", "netio products a.s."],
        },
        "compatible_models": [
            {
                "manufacturer": "NETIO",
                "models": [
                    "PowerPDU 8QS", "PowerPDU 4PS", "PowerPDU 4KS", "PowerDIN 4PZ",
                    "PowerCable 2PZ", "PowerBOX 4KF",
                ],
                "confidence": "partial",
                "notes": (
                    "Run against NETIO's public demo units on firmware 5.3.1 and 5.3.2: identity, "
                    "outputs, inputs, metering, watchdogs, rules and power analysis blocks read; "
                    "outputs switched, toggled, rebooted, switched on for a time and renamed. "
                    "Uploading certificates and firmware is not part of this driver."
                ),
            },
            {
                "manufacturer": "NETIO",
                "models": [
                    "PowerPDU 4KB", "PowerPDU 4PB", "PowerPDU 4QV", "PowerPDU 8QB", "PowerPDU 8QV",
                    "PowerPDU 8KS", "PowerPDU 8KF", "PowerPDU 8KB", "PowerPDU FK6", "PowerPDU VK6",
                    "PowerBOX 3PF", "PowerBOX 3PE", "PowerBOX 3PG", "PowerBOX 4KE", "PowerBOX 4KG",
                    "PowerCable REST 101x", "PowerCable 1Kx", "PowerCable 2KZ", "PowerCable 2PF",
                    "PowerCable 2KF", "PowerCable 2PE", "PowerCable 2KE", "PowerCable 2PG",
                    "PowerCable 2KG", "PowerCable 2PS", "PowerCable 2KS", "PowerCable 2PB",
                    "PowerCable 2KB", "PowerDIN ZP3", "PowerDIN ZK3",
                ],
                "confidence": "untested",
                "notes": (
                    "Same universal firmware and JSON API as the units above; the outlet type in the "
                    "model name (S, B, F, E, G, Z) does not change the API. Metering follows the "
                    "model: K models meter every output, Q models the whole unit and output 1, P "
                    "models switch without metering."
                ),
            },
        ],
        "help": {
            "overview": (
                "NETIO makes networked rack PDUs, power strips, power cables and DIN-rail units. "
                "Each output is a child entity here, so a macro, schedule or panel button can switch "
                "one output, reboot a frozen device, or switch an output on for a set time. On "
                "metered models each output's load and energy are reported too, along with the "
                "status of the unit's own IP watchdogs and rules."
            ),
            "setup": (
                "1. In the NETIO's web interface, open M2M API Protocols, then the JSON API tab. "
                "Turn on Enable JSON API, Enable READ-ONLY and Enable WRITE, and save. The unit "
                "restarts its web server, so it answers errors for a few seconds.\n"
                "2. The same tab has a username and password for reading and another pair for "
                "writing. NETIO ships both as netio / netio. Enter the reading login as Username "
                "and Password here. Fill in Write Username and Write Password only when the writing "
                "login is different.\n"
                "3. The port on that tab is the one to enter here (80 unless it was changed). For "
                "HTTPS (firmware 5.0.3 and later), turn on Use HTTPS and enter the HTTPS port.\n"
                "4. Outputs, inputs, watchdogs, rules and power analysis blocks appear as children "
                "once connected."
            ),
        },
        "default_config": {
            "host": "",
            "port": 80,
            "ssl": False,
            "verify_ssl": False,
            "username": "netio",
            "password": "",
            "write_username": "",
            "write_password": "",
            "poll_interval": 10,
        },
        "config_schema": {
            "host": {
                "type": "string", "required": True, "label": "IP Address",
                "description": "The NETIO unit's IP address or hostname.",
            },
            "port": {
                "type": "integer", "default": 80, "label": "Port", "min": 1, "max": 65535,
                "description": "The JSON API port, set on the unit's JSON API tab. 80 unless it was changed.",
            },
            "ssl": {
                "type": "boolean", "default": False, "label": "Use HTTPS",
                "description": "Firmware 5.0.3 and later can serve the JSON API over HTTPS.",
            },
            "verify_ssl": {
                "type": "boolean", "default": False, "label": "Verify HTTPS Certificate",
                "description": "Off accepts the unit's own self-signed certificate.",
            },
            "username": {
                "type": "string", "default": "netio", "label": "Username",
                "description": "The JSON API login for reading. Leave Username and Password blank if "
                               "the unit reads with no login.",
            },
            "password": {
                "type": "string", "secret": True, "label": "Password",
                "description": "The JSON API password for reading. NETIO ships it as netio.",
            },
            "write_username": {
                "type": "string", "default": "", "label": "Write Username",
                "description": "The JSON API login for writing, when it differs from the one for "
                               "reading. Blank uses the reading login.",
            },
            "write_password": {
                "type": "string", "secret": True, "label": "Write Password",
                "description": "The JSON API password for writing, when it differs from the one for "
                               "reading.",
            },
            "poll_interval": {
                "type": "integer", "default": 10, "label": "Poll Interval (sec)", "min": 0,
                "description": "How often to read the unit's status. 0 turns polling off.",
            },
        },
        "state_variables": {
            "model": {"type": "string", "label": "Model"},
            "device_name": {"type": "string", "label": "Device Name"},
            "serial_number": {"type": "string", "label": "Serial Number"},
            "mac_address": {"type": "string", "label": "MAC Address", "cloud_priority": "low"},
            "firmware": {"type": "string", "label": "Firmware Version"},
            "json_version": {"type": "string", "label": "JSON API Version", "cloud_priority": "low"},
            "device_time": {"type": "string", "label": "Device Clock", "cloud_priority": "low"},
            "uptime_s": {"type": "integer", "label": "Uptime", "unit": "s", "min": 0, "cloud_priority": "low"},
            "output_count": {"type": "integer", "label": "Outputs", "min": 0},
            "input_count": {"type": "integer", "label": "Inputs", "min": 0},
            "metered": {"type": "boolean", "label": "Metered"},
            "voltage_v": {
                "type": "number", "label": "Voltage", "unit": "V", "min": 0, "max": 300, "step": 0.01,
                "cloud_priority": "low",
            },
            "frequency_hz": {
                "type": "number", "label": "Frequency", "unit": "Hz", "min": 0, "max": 70, "step": 0.01,
                "cloud_priority": "low",
            },
            "total_current_a": {
                "type": "number", "label": "Total Current", "unit": "A", "min": 0, "max": 20, "step": 0.001,
                "cloud_priority": "high",
            },
            "total_load_w": {
                "type": "number", "label": "Total Load", "unit": "W", "min": 0, "cloud_priority": "high",
            },
            "total_power_factor": {
                "type": "number", "label": "Total Power Factor", "min": 0, "max": 1, "step": 0.01,
                "cloud_priority": "low",
            },
            "total_phase_deg": {
                "type": "number", "label": "Total Phase", "unit": "°", "min": 0, "max": 360, "step": 0.01,
                "cloud_priority": "low",
            },
            "total_energy_wh": {
                "type": "integer", "label": "Total Energy", "unit": "Wh", "min": 0, "cloud_priority": "low",
            },
            "total_energy_nr_wh": {
                "type": "integer", "label": "Total Energy (not resettable)", "unit": "Wh", "min": 0,
                "cloud_priority": "low",
            },
            "total_reverse_energy_wh": {
                "type": "integer", "label": "Total Reverse Energy", "unit": "Wh", "min": 0,
                "cloud_priority": "low",
            },
            "total_reverse_energy_nr_wh": {
                "type": "integer", "label": "Total Reverse Energy (not resettable)", "unit": "Wh", "min": 0,
                "cloud_priority": "low",
            },
            "energy_start": {"type": "string", "label": "Energy Counted Since", "cloud_priority": "low"},
            "last_error": {"type": "string", "label": "Last Error"},
        },
        "child_entity_types": {
            "output": {
                "label": "Output",
                "label_plural": "Outputs",
                "id_format": {"type": "integer", "min": 1, "max": 24},
                "state_variables": {
                    "name": {"type": "string", "label": "Name", "cloud_priority": "low"},
                    "state": {"type": "boolean", "label": "On", "control": True, "cloud_priority": "high"},
                    "delay_ms": {
                        "type": "integer", "label": "Short On/Off Time", "unit": "ms", "min": 0,
                        "cloud_priority": "low",
                    },
                    "metered": {"type": "boolean", "label": "Metered", "cloud_priority": "low"},
                    "voltage_v": {
                        "type": "number", "label": "Voltage", "unit": "V", "min": 0, "max": 300, "step": 0.01,
                        "cloud_priority": "low",
                    },
                    "frequency_hz": {
                        "type": "number", "label": "Frequency", "unit": "Hz", "min": 0, "max": 70, "step": 0.01,
                        "cloud_priority": "low",
                    },
                    "current_a": {
                        "type": "number", "label": "Current", "unit": "A", "min": 0, "max": 20, "step": 0.001,
                        "cloud_priority": "low",
                    },
                    "load_w": {"type": "number", "label": "Load", "unit": "W", "min": 0, "cloud_priority": "low"},
                    "power_factor": {
                        "type": "number", "label": "Power Factor", "min": 0, "max": 1, "step": 0.01,
                        "cloud_priority": "low",
                    },
                    "phase_deg": {
                        "type": "number", "label": "Phase", "unit": "°", "min": 0, "max": 360, "step": 0.01,
                        "cloud_priority": "low",
                    },
                    "energy_wh": {"type": "integer", "label": "Energy", "unit": "Wh", "min": 0, "cloud_priority": "low"},
                    "energy_nr_wh": {
                        "type": "integer", "label": "Energy (not resettable)", "unit": "Wh", "min": 0,
                        "cloud_priority": "low",
                    },
                    "reverse_energy_wh": {
                        "type": "integer", "label": "Reverse Energy", "unit": "Wh", "min": 0, "cloud_priority": "low",
                    },
                    "reverse_energy_nr_wh": {
                        "type": "integer", "label": "Reverse Energy (not resettable)", "unit": "Wh", "min": 0,
                        "cloud_priority": "low",
                    },
                    "energy_start": {"type": "string", "label": "Energy Counted Since", "cloud_priority": "low"},
                },
                "summary_fields": ["name", "state", "load_w"],
                "label_field": "name",
            },
            "input": {
                "label": "Input",
                "label_plural": "Inputs",
                "id_format": {"type": "integer", "min": 1, "max": 8},
                "state_variables": {
                    "name": {"type": "string", "label": "Name", "cloud_priority": "low"},
                    "closed": {"type": "boolean", "label": "Contact Closed", "cloud_priority": "high"},
                    "s0_counter": {"type": "integer", "label": "S0 Pulse Count", "min": 0, "cloud_priority": "low"},
                    "temperature_c": {
                        "type": "number", "label": "Temperature", "unit": "°C", "min": -40, "max": 125, "step": 0.1,
                        "cloud_priority": "high",
                    },
                    "temperature_f": {
                        "type": "number", "label": "Temperature (F)", "unit": "°F", "min": -40, "max": 257, "step": 0.1,
                        "cloud_priority": "low",
                    },
                    "humidity": {
                        "type": "number", "label": "Humidity", "unit": "%", "min": 0, "max": 100, "step": 1,
                        "cloud_priority": "high",
                    },
                },
                "summary_fields": ["name", "closed", "temperature_c"],
                "label_field": "name",
            },
            "watchdog": {
                "label": "Watchdog",
                "label_plural": "Watchdogs",
                "id_format": {"type": "string"},
                "state_variables": {
                    "name": {"type": "string", "label": "Name", "cloud_priority": "low"},
                    "enabled": {"type": "boolean", "label": "Enabled", "cloud_priority": "low"},
                    "failed": {"type": "boolean", "label": "Failed", "cloud_priority": "high"},
                    "last_ok": {"type": "boolean", "label": "Last Check Passed", "cloud_priority": "high"},
                    "last_check": {"type": "string", "label": "Last Checked (device clock)", "cloud_priority": "low"},
                },
                "summary_fields": ["name", "enabled", "failed"],
                "label_field": "name",
            },
            "rule": {
                "label": "Rule",
                "label_plural": "Rules",
                "id_format": {"type": "string"},
                "state_variables": {
                    "name": {"type": "string", "label": "Name", "cloud_priority": "low"},
                    "enabled": {"type": "boolean", "label": "Enabled", "cloud_priority": "low"},
                    "result": {"type": "boolean", "label": "Result", "cloud_priority": "high"},
                },
                "summary_fields": ["name", "enabled", "result"],
                "label_field": "name",
            },
            "pab": {
                "label": "Power Analysis Block",
                "label_plural": "Power Analysis Blocks",
                "id_format": {"type": "string"},
                "state_variables": {
                    "name": {"type": "string", "label": "Name", "cloud_priority": "low"},
                    "pab_type": {"type": "string", "label": "Type", "cloud_priority": "low"},
                    "enabled": {"type": "boolean", "label": "Enabled", "cloud_priority": "low"},
                    "in_range": {"type": "boolean", "label": "In Range", "cloud_priority": "high"},
                    "zone": {"type": "integer", "label": "Zone", "min": 0, "cloud_priority": "high"},
                },
                "summary_fields": ["name", "enabled", "in_range"],
                "label_field": "name",
            },
        },
        "quick_actions": ["output_on", "output_off", "output_reboot", "refresh"],
        "actions": [
            {"id": "refresh", "kind": "command", "icon": "refresh-cw"},
            {
                "id": "all_outputs_off",
                "kind": "command",
                "icon": "power-off",
                "confirm": "Switch off every output on this NETIO. Everything plugged into it loses power. Continue?",
            },
            {
                "id": "all_outputs_reboot",
                "kind": "command",
                "icon": "rotate-ccw",
                "confirm": "Reboot every output on this NETIO. Everything plugged into it loses power and comes back. Continue?",
            },
        ],
        "commands": {
            "refresh": {
                "label": "Refresh Status",
                "params": {},
                "help": "Reads every value from the NETIO now.",
            },
            "output_on": {
                "label": "Turn Output On",
                "params": {"output": {"type": "child_id", "child_type": "output", "required": True, "label": "Output"}},
                "sets": {"state": True},
                "help": "Switches the output on.",
            },
            "output_off": {
                "label": "Turn Output Off",
                "params": {"output": {"type": "child_id", "child_type": "output", "required": True, "label": "Output"}},
                "sets": {"state": False},
                "help": "Switches the output off.",
            },
            "output_toggle": {
                "label": "Toggle Output",
                "params": {"output": {"type": "child_id", "child_type": "output", "required": True, "label": "Output"}},
                "help": "Switches the output to the opposite of what it is now.",
            },
            "output_reboot": {
                "label": "Reboot Output",
                "params": {
                    "output": {"type": "child_id", "child_type": "output", "required": True, "label": "Output"},
                    "seconds": {
                        "type": "number", "required": False, "label": "Off Time (sec)", "min": 0.1,
                        "help": "How long the output stays off. Blank uses the output's own short "
                                "on/off time, set in the NETIO's web interface.",
                    },
                },
                "help": "Switches the output off, then on again after its short on/off time "
                        "(or the time given).",
            },
            "output_on_for": {
                "label": "Turn Output On for a Time",
                "params": {
                    "output": {"type": "child_id", "child_type": "output", "required": True, "label": "Output"},
                    "seconds": {
                        "type": "number", "required": False, "label": "On Time (sec)", "min": 0.1,
                        "help": "How long the output stays on. Blank uses the output's own short "
                                "on/off time, set in the NETIO's web interface.",
                    },
                },
                "help": "Switches the output on, then off again after its short on/off time "
                        "(or the time given).",
            },
            "all_outputs_on": {
                "label": "Turn All Outputs On",
                "params": {},
                "help": "Switches every output on.",
            },
            "all_outputs_off": {
                "label": "Turn All Outputs Off",
                "params": {},
                "confirm": "Switches off every output on this NETIO. Everything plugged into it loses power.",
                "help": "Switches every output off.",
            },
            "all_outputs_reboot": {
                "label": "Reboot All Outputs",
                "params": {},
                "confirm": "Reboots every output on this NETIO. Everything plugged into it loses power "
                           "and comes back.",
                "help": "Switches every output off, then on again, each after its own short on/off time.",
            },
            "rename_output": {
                "label": "Rename Output",
                "params": {
                    "output": {"type": "child_id", "child_type": "output", "required": True, "label": "Output"},
                    "name": {"type": "string", "required": True, "label": "Name"},
                },
                "sets": {"name": "{name}"},
                "help": "Names the output, so the NETIO's web interface and this device page both "
                        "show what is plugged into it.",
            },
            "reset_output_energy": {
                "label": "Reset Output Energy Counter",
                "params": {"output": {"type": "child_id", "child_type": "output", "required": True, "label": "Output"}},
                "confirm": "Zeroes this output's energy counter. The counted total cannot be recovered; "
                           "the not-resettable counter keeps counting.",
                "help": "Zeroes the output's resettable energy counter and restarts it from now. "
                        "Metered outputs only.",
            },
            "enable_output_schedule": {
                "label": "Enable Output Schedule",
                "params": {"output": {"type": "child_id", "child_type": "output", "required": True, "label": "Output"}},
                "help": "Turns on the schedule assigned to this output in the NETIO's web interface, "
                        "so the output is switched at its scheduled times again.",
            },
            "disable_output_schedule": {
                "label": "Disable Output Schedule",
                "params": {"output": {"type": "child_id", "child_type": "output", "required": True, "label": "Output"}},
                "help": "Turns off the schedule assigned to this output, so the output stays as it "
                        "is until it is switched by hand or by OpenAVC.",
            },
        },
    }

    # The driver owns its HTTP session, so the platform's silence check does not
    # watch it: this probe does, alongside the poll and while polling is off.
    HEALTH_INTERVAL_S = 30.0
    HEALTH_TIMEOUT_S = 10.0
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = "The NETIO stopped answering status reads."

    _client: httpx.AsyncClient | None = None

    def __init__(self, device_id: str, config: dict[str, Any], state: Any, events: Any) -> None:
        super().__init__(device_id, config, state, events)
        self._client = None
        self._connect_doc: dict[str, Any] | None = None
        self._rosters: dict[str, list[Any]] = {t: [] for t in CHILD_TYPES}
        for key in ("password", "write_password"):
            secret = str(config.get(key, "") or "")
            if secret:
                self.redact_in_log(secret)

    # ── Config ──

    @property
    def _host(self) -> str:
        return str(self.config.get("host", "") or "").strip()

    @property
    def _port(self) -> int:
        port = _as_int(self.config.get("port"))
        return port if port and 0 < port < 65536 else 80

    @property
    def _scheme(self) -> str:
        return "https" if _as_bool(self.config.get("ssl")) else "http"

    @property
    def _username(self) -> str:
        return str(self.config.get("username", "") or "").strip()

    @property
    def _password(self) -> str:
        return str(self.config.get("password", "") or "")

    def _read_auth(self) -> httpx.BasicAuth | None:
        # NETIO: "Empty Username and Password means no authentication."
        if not self._username and not self._password:
            return None
        return httpx.BasicAuth(self._username, self._password)

    def _write_auth(self) -> httpx.BasicAuth | None:
        username = str(self.config.get("write_username", "") or "").strip()
        password = str(self.config.get("write_password", "") or "")
        if not username and not password:
            return self._read_auth()
        return httpx.BasicAuth(username, password)

    def _where(self) -> str:
        return f"{self._host}:{self._port}"

    def _not_responding(self, exc: Exception) -> ConnectionError:
        # A timeout's own text is empty; its name says what happened.
        detail = str(exc).strip() or type(exc).__name__
        return ConnectionError(f"{self._where()} is not responding: {detail}")

    # ── Sentences ──

    def _read_refused(self) -> ConnectionFaultError:
        message = "The NETIO refused the JSON API login for reading (HTTP 401)."
        if not self._password:
            message += " No password is entered; NETIO ships the reading login as netio / netio."
        return ConnectionFaultError(message, code="auth_failed")

    def _api_off(self) -> str:
        return (
            f"The device at {self._where()} answered, but not its JSON API (HTTP 404). Turn on "
            f"JSON API under M2M API Protocols on the NETIO, and check the port set there."
        )

    @staticmethod
    def _write_refused(exc: NetioHTTPError) -> str:
        if exc.status == 401:
            return "The NETIO refused the JSON API login for writing (HTTP 401), so nothing changed."
        if exc.status == 403:
            return "The NETIO takes reads only (HTTP 403): writing is off on its JSON API tab."
        if exc.status == 404:
            return "The NETIO's JSON API is off (HTTP 404), so nothing changed."
        if exc.status == 400:
            detail = f": {exc.message}" if exc.message else ""
            return f"The NETIO refused the command (HTTP 400{detail})."
        detail = f" ({exc.message})" if exc.message else ""
        return f"The NETIO answered the command with HTTP {exc.status}{detail}."

    # ── Connection lifecycle ──

    async def _create_transport(self, transport_type: str) -> None:
        if not self._host:
            raise ConnectionFaultError("No IP address configured", code="invalid_config")
        if not await self._verify_reachable(self._host, self._port):
            raise ConnectionError(f"{self._where()} is not responding")
        self._client = httpx.AsyncClient(
            base_url=f"{self._scheme}://{self._where()}",
            verify=bool(_as_bool(self.config.get("verify_ssl"))),
            timeout=httpx.Timeout(10.0, connect=5.0),
            # A refusal must arrive as itself, never as a page a redirect led to.
            follow_redirects=False,
        )

    async def _post_connect(self) -> None:
        try:
            doc = await self._read()
        except NetioHTTPError as exc:
            if exc.status == 401:
                raise self._read_refused() from exc
            if exc.status == 403:
                # A refusal on the core read at connect: retrying cannot help
                # until someone changes the unit, so it pauses like a refused login.
                raise ConnectionFaultError(
                    "The NETIO refused to share its status (HTTP 403): reading is off on its "
                    "JSON API tab.",
                    code="auth_failed",
                ) from exc
            if exc.status == 404:
                raise ConnectionFaultError(self._api_off(), code="invalid_config") from exc
            raise ConnectionError(f"The NETIO answered the status read with {exc}") from exc
        except NetioReplyError as exc:
            raise ConnectionFaultError(
                f"The device at {self._where()} answered, but not with NETIO's JSON status: {exc}.",
                code="invalid_config",
            ) from exc
        except httpx.TransportError as exc:
            raise self._not_responding(exc) from exc
        self._connect_doc = doc
        agent = doc.get("Agent") or {}
        log.info(
            f"[{self.device_id}] Connected to NETIO {agent.get('Model', '?')} "
            f"{agent.get('DeviceName', '')} at {self._where()} (firmware {agent.get('Version', '?')})"
        )

    async def _initial_sync(self) -> None:
        doc, self._connect_doc = self._connect_doc, None
        if doc is not None:
            self._apply(doc)

    def _link_alive(self) -> bool:
        return self._client is not None

    async def _close_session(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _liveness_probe(self) -> None:
        """The status read. Any answer is a live unit; only a refused reading
        login is a fault of its own."""
        try:
            await self._read()
        except NetioHTTPError as exc:
            if exc.status == 401:
                raise self._read_refused() from exc
            return
        except NetioReplyError:
            return
        except httpx.TransportError as exc:
            raise self._not_responding(exc) from exc

    # ── HTTP ──

    def _require_client(self) -> httpx.AsyncClient:
        client = self._client
        if client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        return client

    @staticmethod
    def _status_from(resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code != 200:
            raise NetioHTTPError(resp.status_code, parse_error(resp.text))
        return parse_status(resp.text)

    async def _read(self) -> dict[str, Any]:
        client = self._require_client()
        resp = await client.get(STATUS_PATH, auth=self._read_auth())
        return self._status_from(resp)

    async def _write(self, outputs: list[dict[str, Any]], *, end_session: bool = False) -> None:
        """POST the changes and take the status the unit answers with.

        ``end_session`` closes the HTTP connection after the answer. A short
        on/off time sent with a command "is valid only for a single protocol
        session", and the unit goes back to each output's own time "when the
        connection is closed" (document, "Output actions"). This client keeps
        its connection open between requests, so without the close a later
        reboot sent with no time would still use the one given here.
        """
        client = self._require_client()
        headers = {"Connection": "close"} if end_session else None
        try:
            resp = await client.post(
                STATUS_PATH, json={"Outputs": outputs}, auth=self._write_auth(), headers=headers,
            )
            doc = self._status_from(resp)
        except NetioHTTPError as exc:
            message = self._write_refused(exc)
            self.set_state("last_error", message)
            raise NetioCommandError(message) from exc
        except NetioReplyError as exc:
            message = f"The NETIO answered the command with something other than its status: {exc}."
            self.set_state("last_error", message)
            raise NetioCommandError(message) from exc
        except httpx.TransportError as exc:
            raise self._not_responding(exc) from exc
        self._apply_write_reply(doc)

    # ── State ──

    def _apply(self, doc: dict[str, Any]) -> None:
        summary = summarize(doc)
        self.set_states(summary["device"])
        for child_type in CHILD_TYPES:
            self._sync_children(child_type, summary[child_type])

    def _apply_write_reply(self, doc: dict[str, Any]) -> None:
        """Take what a write changes from its answer, and nothing else.

        The answer to a switch is a shorter status than a read (the 4KS demo on
        5.3.2): each output leaves out its own voltage, frequency,
        not-resettable counters and EnergyStart, and reports power factor 0
        where a read of the same idle output says 1.0. So only each output's
        name, on/off and short on/off time are taken from it; metering comes
        from the next read.
        """
        for local_id, values in numbered(doc.get("Outputs"), MAX_OUTPUTS, parse_output).items():
            if local_id in self._rosters["output"]:
                self.set_child_state_batch(
                    "output", local_id,
                    {key: values[key] for key in ("name", "state", "delay_ms")},
                )

    def _sync_children(self, child_type: str, entries: dict[Any, dict[str, Any]]) -> None:
        """The unit is the authority on which children exist: a new one is
        registered, one it no longer reports is removed."""
        known = set(self._rosters[child_type])
        for local_id, values in entries.items():
            if local_id in known:
                self.set_child_state_batch(child_type, local_id, values)
            else:
                self.register_child(child_type, local_id, initial_state=values)
        for local_id in known - set(entries):
            self.deregister_child(child_type, local_id)
        self._rosters[child_type] = list(entries)

    # ── Polling ──

    async def poll(self) -> None:
        """One status read. A refused reading login drops the connection; any
        other refusal leaves the unit connected and named in last_error, written
        again by every poll while it holds."""
        if self._client is None:
            return
        try:
            doc = await self._read()
        except NetioHTTPError as exc:
            if exc.status == 401:
                raise self._read_refused() from exc
            if exc.status == 404:
                self.set_state("last_error", self._api_off())
            elif exc.status == 403:
                self.set_state("last_error", "The NETIO refused to share its status (HTTP 403): "
                                             "reading is off on its JSON API tab.")
            else:
                self.set_state("last_error", f"The NETIO answered the status read with {exc}.")
            return
        except NetioReplyError as exc:
            self.set_state("last_error", f"The NETIO's status could not be read: {exc}.")
            return
        except httpx.TransportError as exc:
            raise self._not_responding(exc) from exc
        self._apply(doc)

    async def refresh_children(self) -> Any:
        try:
            doc = await self._read()
        except (NetioHTTPError, NetioReplyError) as exc:
            raise NetioCommandError(f"The NETIO's status could not be read: {exc}") from exc
        except httpx.TransportError as exc:
            raise self._not_responding(exc) from exc
        self._apply(doc)
        return {t: list(ids) for t, ids in self._rosters.items()}

    # ── Commands ──

    def _output_id(self, params: dict[str, Any]) -> int:
        output = _as_int(params.get("output"))
        roster = self._rosters["output"]
        if output is None:
            raise NetioCommandError("No output was given.")
        if roster and output not in roster:
            raise NetioCommandError(
                f"This NETIO has no output {output}; its outputs are {min(roster)} to {max(roster)}."
            )
        return output

    @staticmethod
    def _delay_ms(params: dict[str, Any]) -> int | None:
        seconds = _as_float(params.get("seconds"))
        if seconds is None:
            return None
        if seconds <= 0:
            raise NetioCommandError("The time must be more than 0 seconds.")
        return int(round(seconds * 1000))

    async def _set_action(self, params: dict[str, Any], action: int) -> None:
        await self._write([{"ID": self._output_id(params), "Action": action}])

    async def _short(self, params: dict[str, Any], action: int) -> None:
        entry: dict[str, Any] = {"ID": self._output_id(params), "Action": action}
        delay = self._delay_ms(params)
        if delay is not None:
            entry["Delay"] = delay
        await self._write([entry], end_session=delay is not None)

    async def _all(self, action: int) -> None:
        outputs = self._rosters["output"]
        if not outputs:
            raise NetioCommandError("The NETIO has not reported its outputs yet.")
        await self._write([{"ID": output, "Action": action} for output in outputs])

    async def _cmd_refresh(self, params: dict[str, Any]) -> None:
        await self.poll()

    async def _cmd_output_on(self, params: dict[str, Any]) -> None:
        await self._set_action(params, ACTION_ON)

    async def _cmd_output_off(self, params: dict[str, Any]) -> None:
        await self._set_action(params, ACTION_OFF)

    async def _cmd_output_toggle(self, params: dict[str, Any]) -> None:
        await self._set_action(params, ACTION_TOGGLE)

    async def _cmd_output_reboot(self, params: dict[str, Any]) -> None:
        await self._short(params, ACTION_SHORT_OFF)

    async def _cmd_output_on_for(self, params: dict[str, Any]) -> None:
        await self._short(params, ACTION_SHORT_ON)

    async def _cmd_all_outputs_on(self, params: dict[str, Any]) -> None:
        await self._all(ACTION_ON)

    async def _cmd_all_outputs_off(self, params: dict[str, Any]) -> None:
        await self._all(ACTION_OFF)

    async def _cmd_all_outputs_reboot(self, params: dict[str, Any]) -> None:
        await self._all(ACTION_SHORT_OFF)

    async def _cmd_rename_output(self, params: dict[str, Any]) -> None:
        name = str(params.get("name", "") or "").strip()
        if not name:
            raise NetioCommandError("The output's name cannot be blank.")
        await self._write([{"ID": self._output_id(params), "Name": name, "Action": ACTION_NO_CHANGE}])

    async def _cmd_reset_output_energy(self, params: dict[str, Any]) -> None:
        # The document spells the key two ways (resetEnergyCounter in its list
        # of commands, ResetEnergyCounter in its full example). The 4KS demo
        # took ResetEnergyCounter: the output's EnergyStart moved to the time
        # of the write (2026-10-13). A read follows, since the write's answer
        # is not where metering is taken from.
        await self._write([
            {"ID": self._output_id(params), "ResetEnergyCounter": True, "Action": ACTION_NO_CHANGE}
        ])
        await self.poll()

    async def _cmd_enable_output_schedule(self, params: dict[str, Any]) -> None:
        await self._set_action(params, ACTION_SCHEDULE_ON)

    async def _cmd_disable_output_schedule(self, params: dict[str, Any]) -> None:
        await self._set_action(params, ACTION_SCHEDULE_OFF)

    _DISPATCH = {
        "refresh": _cmd_refresh,
        "output_on": _cmd_output_on,
        "output_off": _cmd_output_off,
        "output_toggle": _cmd_output_toggle,
        "output_reboot": _cmd_output_reboot,
        "output_on_for": _cmd_output_on_for,
        "all_outputs_on": _cmd_all_outputs_on,
        "all_outputs_off": _cmd_all_outputs_off,
        "all_outputs_reboot": _cmd_all_outputs_reboot,
        "rename_output": _cmd_rename_output,
        "reset_output_energy": _cmd_reset_output_energy,
        "enable_output_schedule": _cmd_enable_output_schedule,
        "disable_output_schedule": _cmd_disable_output_schedule,
    }

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        handler = self._DISPATCH.get(command)
        if handler is None:
            raise ValueError(f"Unknown command: {command}")
        self._require_client()
        return await handler(self, params or {})
