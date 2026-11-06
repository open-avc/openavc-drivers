"""GUDE Expert Power Control 8291 driver (HTTP / HTTPS, CGI requests and JSON).

The Expert Power Control 8291-2 is a 1 RU AC / DC power distribution unit for
AV racks and furniture: 21 switched power ports in five banks, 17 DC ports
(Bank A 5 V; Banks B, C and D each 12 V or 24 V) behind electronic fuses
(eFuses) and 4 AC ports (Bank E, IEC C13), fed by a built-in 300 W power
supply. It meters the mains input (voltage, current, power, frequency, power
factor, energy, residual current), each bank, each DC power source, every DC
port (the 8291-2 only), and up to two plug-in temperature / humidity / air
pressure sensors. The 8291-1, its predecessor, has the same banks, ports and
interface without the per-port DC metering.

What the driver reads, from one status request (``status.json``):

  outputs        each port's name, on / off, switch count and running timer
  hardware       the banks, which ports each one holds, and the state of the
                 overvoltage protection where a bank has one
  sensor_descr   the device's own description of every sensor it carries:
  sensor_values  its types, fields, units and names, and the readings in the
                 same layout. GUDE documents this pair as the way to read any
                 of its devices without per-model knowledge, and the driver
                 does exactly that: the line meter, the system sensors, the
                 banks (each port's eFuse state), the DC power sources and the
                 external sensors are read by type and field name, and a
                 sensor type it does not map (the 8291-2's per-port DC
                 metering among them) still reaches state as a meter child
                 built from the device's own description of it.

What it sends: a port on, off or over (toggle), a power cycle (Reset: off for
the port's Reset Duration, then on), a timed switch (one state, a wait, then
another: the batch mode), cancelling a port's running timer, and restarting
the PDU's controller (the ports keep their power).

**Sign-in.** Current firmware signs in with a cookie session by default, and
the HTTP setting "Authentication Mode" decides what else it accepts: Basic
Compatible takes HTTP Basic or a session, Session takes only a session (and
switching only as a POST), and Session Extended also wants the session's
CSRF token in a ``sessionidX`` header (manual section 4.4, "HTTP
Authentication"). So with a password entered the driver signs in through
``login.json`` and sends every request in session form, which every mode
accepts; a unit whose firmware has no ``login.json`` (it answers 404) is sent
HTTP Basic, as the wiki's interface page describes. With no password entered
it sends no credentials, which is the factory state (no passwords are set).

**Polling.** The unit pushes in three ways: MQTT messages to a broker the
site runs, SNMP traps to a manager address set on the unit, and "push
messages" on an open Telnet / SSH console session. None is a channel this
driver can open on the device: a broker is site infrastructure, the platform
has no trap listener (the ``snmp_v2c`` row of the pending-push tracker in the
driver roadmap), and the console's push message format is not documented.
Nothing is lost between polls: a port changes only when switched (by a
command, the front panel, a timer, a watchdog or a schedule) and stays
changed, a tripped eFuse stays tripped until the port is switched on again,
and the overvoltage protection, once failed, stays failed. One request reads
everything, so a poll every few seconds shows a change within that time.

**Why Python, not YAML.** The sensor readings are a nested array laid out by
a second array the device sends (``sensor_descr``), whose order and contents
change with what is plugged in (an external sensor adds an entry) and with
the hardware revision (only an 8291-2 carries per-port DC metering). A YAML
``json`` rule addresses a fixed path, so it would read the wrong value the
day a sensor is plugged in. Also: the sign-in is a cookie session with an
optional CSRF header (no ``auth.type`` covers it), the banks group the ports
two levels deep (one bank's ports and its supply in one entry), and a sensor
type the driver does not know becomes a child whose schema is the device's
own field list.

Sources:
  GUDE Wiki, "EPC HTTP Interface" (the CGI commands and the status JSON
  components) -- https://wiki.gude-systems.com/EPC_HTTP_Interface
  GUDE Wiki, "HTTP JSON Sensor Data" (sensor_descr / sensor_values, the
  sensor type numbers, the 8291's bank groups) --
  https://wiki.gude-systems.com/HTTP_JSON_Sensor_Data
  Expert Power Control 8291-1 manual v1.5.1 (2026-04): banks, power sources,
  eFuse states, overvoltage protection, HTTP Authentication (section 4.4) --
  https://gude-systems.com/app/uploads/2026/04/manual-epc8291-1-series_v1.5.1.pdf
  GUDE's own port.py (MIT), linked from the wiki's quick start: toggle as
  ``cmd=1&s=2`` and the overvoltage state in ``hardware.banks[].fuse`` --
  https://github.com/gudesystems/port.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from typing import Any

import httpx

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.utils.logger import get_logger

try:  # OpenAVC 0.37.1 and later: the device's reason reaches Send Command.
    from openavc.drivers.base import CommandRefusedError
except ImportError:  # 0.37.0 and earlier refuse the same way, untyped.
    CommandRefusedError = RuntimeError

log = get_logger(__name__)

STATUS_PATH = "/status.json"
LOGIN_PATH = "/login.json"

# statusjsn.js / status.json "components" bits (EPC HTTP Interface wiki, and
# HTTP JSON Sensor Data for the "extended" marker that adds sensor groups).
C_OUTPUTS = 0x1
C_ETHERNET = 0x8
C_MISC = 0x10
C_PORT_SUMMARY = 0x100
C_HARDWARE = 0x200
C_SENSOR_VALUES = 0x4000
C_SENSOR_DESCR = 0x10000
C_EXTENDED = 0x800000

# Every poll, and the answer every command asks for: the command's own reply
# is then its read-back.
POLL_COMPONENTS = C_OUTPUTS | C_PORT_SUMMARY | C_HARDWARE | C_SENSOR_VALUES | C_EXTENDED
# At connect, on Refresh, every FULL_EVERY_POLLS polls, and whenever the
# sensor layout looks changed: the description, identity and MAC too.
FULL_COMPONENTS = POLL_COMPONENTS | C_MISC | C_ETHERNET | C_SENSOR_DESCR
FULL_EVERY_POLLS = 12

# CGI commands (EPC HTTP Interface wiki).
CMD_SWITCH = 1  # p, s: 0 off, 1 on (2 toggle: GUDE's port.py)
CMD_CANCEL_BATCH = 2  # p
CMD_BATCH = 5  # p, a1, s (wait seconds), a2
CMD_RESET = 12  # p: off for the port's Reset Duration, then on
CMD_RESTART_DEVICE = 39  # ports and configuration untouched

# Sensor type numbers (HTTP JSON Sensor Data wiki).
T_LINE_METER = 9  # line power meter with residual current (the 8291)
T_SYSTEM = 20
T_BANK = 101
T_POWER_SOURCE = 102
T_EXT_SENSORS = (51, 52, 53)

# Plug-in sensor connectors on the 8291 (manual 1.3).
EXT_SENSOR_PORTS = 2

# A port switched on waits for the unit's one-second gap since the last port
# switched (manual 3.1), so a burst of switching settles over a few seconds.
SWITCH_SETTLE_S = 5.0
SWITCH_RECHECK_S = 0.5

# eFuse "State" (manual 1.5, "Use of eFuses"): 0 switched off, 1 switched on,
# 2 eFuse triggered (Error 1), 3 external voltage source (Error 2).
EFUSE_FAULT = {2: "efuse_tripped", 3: "external_voltage"}
FAULT_SENTENCE = {
    "efuse_tripped": "its eFuse tripped (an overload or a short circuit)",
    "external_voltage": "it reports an external voltage on its output",
}

# Overvoltage protection, hardware.banks[].fuse, read as the MIB's
# epc8291OVPStatus: failure(0), ok(1), unknown(2).
OVP_STATE = {0: "failed", 1: "ok", 2: "unknown"}

LINE_FIELDS = {
    "voltage": "voltage",
    "current": "current",
    "frequency": "frequency",
    "activepower": "power",
    "apparentpower": "apparent_power",
    "reactivepower": "reactive_power",
    "powerfactor": "power_factor",
    "residualcurrent": "residual_current",
    "absactenergynonres": "energy_total",
    "absactenergyres": "energy_resettable",
}
EXT_FIELDS = {
    "temperature": "temperature",
    "humidity": "humidity",
    "dewpoint": "dew_point",
    "dewdiff": "dew_point_difference",
    "pressure": "pressure",
}


def _key(name: Any) -> str:
    """A field name as a lookup key: lower case, letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def _slug(name: Any) -> str:
    """A field or id as a state-variable or child-id fragment."""
    text = re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_")
    return text or "value"


def _cell(cell: Any) -> Any:
    """The value of one ``{"v": ...}`` cell (None for a missing cell)."""
    return cell.get("v") if isinstance(cell, dict) else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


class GudeEpc8291Driver(BaseDriver):
    """GUDE Expert Power Control 8291-2 / 8291-1 over the HTTP interface."""

    DRIVER_INFO = {
        "id": "gude_epc8291",
        "name": "GUDE Expert Power Control 8291",
        "manufacturer": "GUDE",
        "category": "power",
        "version": "1.0.0",
        "author": "OpenAVC",
        # restarts_device_for needs 0.34.0; the code's newest platform call is
        # child_fault("not_fitted"), 0.33.0. CommandRefusedError is imported
        # where it exists.
        "min_platform_version": "0.34.0",
        "description": (
            "Controls the GUDE Expert Power Control 8291-2 AC / DC power "
            "distribution unit over its HTTP interface: the 21 ports (17 DC "
            "behind eFuses, 4 AC) switched on, off, over or power-cycled, "
            "with each port's eFuse state, the banks and power sources, the "
            "mains input meter (voltage, current, power, energy, residual "
            "current), the overvoltage protection and plug-in temperature, "
            "humidity and air pressure sensors."
        ),
        "source_url": "https://wiki.gude-systems.com/EPC_HTTP_Interface",
        "tags": ["pdu", "power", "dc-power", "outlet-control", "metering",
                 "efuse", "rack", "environmental-sensor"],
        "verified": False,
        "simulated": True,
        "protocols": ["http"],
        "ports": [80, 443],
        "transport": "http",
        "discovery": {
            # The status read the driver itself makes, asking only for the
            # identity block ("misc"). A unit with HTTP password protection
            # on answers 401 and is found from the hints below instead; the
            # factory state has no passwords set (manual 3.2.3).
            "tcp_probe": {
                "port": 80,
                "send_ascii": "GET /status.json?components=16 HTTP/1.0\r\n\r\n",
                "expect_regex": "\"prodid\"\\s*:\\s*\"8291\"",
                "timeout_ms": 3000,
            },
            # Gude Systems GmbH's MA-L block (IEEE registry); the demo unit's
            # MAC and the wiki's MQTT topics both fall in it.
            "oui": ["00:19:32"],
            # The factory hostname ("def_hostname" in the identity block).
            "hostname": ["epc-8291"],
            "manufacturer_alias": ["gude", "gude systems", "gude systems gmbh"],
        },
        "compatible_models": [
            {
                "manufacturer": "GUDE",
                "models": ["Expert Power Control 8291-2"],
                "confidence": "untested",
                "notes": (
                    "17 switched DC ports (5 V, and 12 V or 24 V per bank) "
                    "behind eFuses, 4 switched AC ports. Its per-port DC "
                    "metering appears as meter children named after the "
                    "ports."
                ),
            },
            {
                "manufacturer": "GUDE",
                "models": ["Expert Power Control 8291-1"],
                "confidence": "untested",
                "notes": (
                    "The predecessor (discontinued): the same banks, ports "
                    "and interface without per-port DC metering."
                ),
            },
        ],
        "help": {
            "overview": (
                "The Expert Power Control 8291 powers a rack's AV gear from "
                "one unit: 5 V, 12 V and 24 V DC terminals and USB sockets "
                "for the devices that would otherwise need plug-in power "
                "adapters, and four AC outlets. Each of the 21 ports is a "
                "child here, so a button, macro or schedule can switch one "
                "or power-cycle a stuck device, and a tripped eFuse (an "
                "overload or a short circuit on a DC port) shows on that "
                "port. The banks, the power supply's outputs, the mains "
                "meter, the overvoltage protection and up to two plug-in "
                "climate sensors come through as live values you can alert "
                "on."
            ),
            "setup": (
                "1. Connect the unit to the network. It takes an address by "
                "DHCP; GUDE's GBL_Conf tool finds it.\n"
                "2. In its web interface, under Configuration > Ethernet > "
                "HTTP, note whether it serves HTTP, HTTPS or both. For "
                "HTTPS, turn on Use HTTPS here and set the port to 443.\n"
                "3. If password protection is on, enter admin or user and its "
                "password here. The user account can read and switch; "
                "Restart Controller may need admin. Any Authentication Mode "
                "works. With no passwords set on the unit (the factory "
                "state), leave the password blank.\n"
                "4. Ports, banks, power sources and sensors appear as "
                "children once connected. Port names are set in the unit's "
                "web interface, under Configuration > Power Ports."
            ),
        },
        "default_config": {
            "host": "",
            "port": 80,
            "ssl": False,
            "verify_ssl": False,
            "username": "admin",
            "password": "",
            "poll_interval": 5,
        },
        "config_schema": {
            "host": {
                "type": "string", "required": True, "label": "IP Address",
                "description": "The unit's IP address or hostname.",
            },
            "port": {
                "type": "integer", "default": 80, "label": "Port",
                "min": 1, "max": 65535,
                "description": "80 for HTTP, 443 for HTTPS, unless changed "
                               "on the unit's HTTP page.",
            },
            "ssl": {
                "type": "boolean", "default": False, "label": "Use HTTPS",
                "description": "Connect with HTTPS. Set the port to 443 "
                               "with it.",
            },
            "verify_ssl": {
                "type": "boolean", "default": False,
                "label": "Verify Certificate",
                "description": "Check the unit's HTTPS certificate. Leave "
                               "off for the certificate it creates itself.",
            },
            "username": {
                "type": "string", "default": "admin", "label": "Username",
                "description": "admin or user, or a RADIUS user. Used only "
                               "when a password is entered.",
            },
            "password": {
                "type": "string", "default": "", "secret": True,
                "label": "Password",
                "description": "Blank when the unit has no password set.",
            },
            "poll_interval": {
                "type": "integer", "default": 5, "min": 0,
                "label": "Poll Interval (sec)",
                "description": "How often to read the ports and meters. "
                               "0 disables polling.",
            },
        },
        "state_variables": {
            "model": {"type": "string", "label": "Model",
                      "cloud_priority": "low"},
            "firmware": {"type": "string", "label": "Firmware Version",
                         "cloud_priority": "low"},
            "hostname": {"type": "string", "label": "Hostname",
                         "cloud_priority": "low"},
            "mac_address": {"type": "string", "label": "MAC Address",
                            "cloud_priority": "low"},
            "uptime": {"type": "integer", "label": "Uptime", "unit": "s",
                       "min": 0, "cloud_priority": "low"},
            "port_count": {"type": "integer", "label": "Ports", "min": 0,
                           "cloud_priority": "low"},
            "voltage": {"type": "number", "label": "Mains Voltage",
                        "unit": "V", "min": 0, "max": 300, "step": 0.1,
                        "cloud_priority": "low"},
            "current": {"type": "number", "label": "Mains Current",
                        "unit": "A", "min": 0, "max": 16, "step": 0.01,
                        "cloud_priority": "low"},
            "frequency": {"type": "number", "label": "Mains Frequency",
                          "unit": "Hz", "min": 0, "max": 70, "step": 0.01,
                          "cloud_priority": "low"},
            "power": {"type": "number", "label": "Active Power",
                      "unit": "W", "min": 0, "max": 4000, "step": 1,
                      "cloud_priority": "low"},
            "apparent_power": {"type": "number", "label": "Apparent Power",
                               "unit": "VA", "min": 0, "step": 1,
                               "cloud_priority": "low"},
            "reactive_power": {"type": "number", "label": "Reactive Power",
                               "unit": "var", "step": 1,
                               "cloud_priority": "low"},
            "power_factor": {"type": "number", "label": "Power Factor",
                             "min": -1, "max": 1, "step": 0.001,
                             "cloud_priority": "low"},
            "residual_current": {"type": "number",
                                 "label": "Residual Current", "unit": "A",
                                 "min": 0, "step": 0.0001,
                                 "cloud_priority": "low"},
            "energy_total": {"type": "number", "label": "Energy",
                             "unit": "kWh", "min": 0, "step": 0.001,
                             "cloud_priority": "low"},
            "energy_resettable": {"type": "number",
                                  "label": "Energy (Resettable Counter)",
                                  "unit": "kWh", "min": 0, "step": 0.001,
                                  "cloud_priority": "low"},
            "internal_temperature": {"type": "number",
                                     "label": "Internal Temperature",
                                     "unit": "°C", "step": 0.1,
                                     "cloud_priority": "low"},
            "overvoltage_protection": {
                "type": "enum", "label": "Overvoltage Protection",
                "values": ["ok", "failed", "unknown"],
                "help": "failed: the surge protection no longer works and "
                        "only GUDE can restore it; the ports still work.",
                "cloud_priority": "high",
            },
            "efuse_fault": {
                "type": "boolean", "label": "Port Fault",
                "help": "True while any port reports a tripped eFuse or an "
                        "external voltage.",
                "cloud_priority": "high",
            },
            "last_error": {"type": "string", "label": "Last Error"},
        },
        "child_entity_types": {
            "port": {
                "label": "Port",
                "label_plural": "Ports",
                "id_format": {"type": "integer", "min": 1, "max": 21},
                "state_variables": {
                    "state": {
                        "type": "boolean", "label": "On",
                        "control": True, "cloud_priority": "high",
                    },
                    "fault": {
                        "type": "enum", "label": "Fault",
                        "values": ["none", "efuse_tripped",
                                   "external_voltage"],
                        "help": "efuse_tripped: an overload or short "
                                "circuit switched the port off; switching it "
                                "on resets the eFuse.",
                        "cloud_priority": "high",
                    },
                    "output_live": {
                        "type": "boolean", "label": "Output Live",
                        "help": "The port's output carries power.",
                        "cloud_priority": "low",
                    },
                    "name": {"type": "string", "label": "Name",
                             "cloud_priority": "low"},
                    "socket": {"type": "string", "label": "Socket",
                               "help": "The socket's mark on the unit: "
                                       "A1 to E4.",
                               "cloud_priority": "low"},
                    "bank": {"type": "string", "label": "Bank",
                             "cloud_priority": "low"},
                    "switch_count": {"type": "integer",
                                     "label": "Switch Count", "min": 0,
                                     "cloud_priority": "low"},
                    "timer_remaining": {
                        "type": "integer", "label": "Timer Remaining",
                        "unit": "s", "min": 0,
                        "help": "Seconds left on a power cycle or timed "
                                "switch in progress; 0 when none is.",
                        "cloud_priority": "low",
                    },
                },
                "summary_fields": ["name", "socket", "state", "fault"],
                "label_field": "name",
            },
            "bank": {
                "label": "Bank",
                "label_plural": "Banks",
                "id_format": {"type": "string", "max_length": 16},
                "state_variables": {
                    "name": {"type": "string", "label": "Name",
                             "cloud_priority": "low"},
                    "supply": {"type": "string", "label": "Supply",
                               "help": "The bank's voltage as the unit "
                                       "names it (5V, DC 12V, 230V).",
                               "cloud_priority": "low"},
                    "voltage": {"type": "number", "label": "Voltage",
                                "unit": "V", "min": 0, "max": 300,
                                "step": 0.01, "cloud_priority": "low"},
                    "current": {"type": "number", "label": "Current",
                                "unit": "A", "min": 0, "max": 16,
                                "step": 0.001, "cloud_priority": "low"},
                    "overvoltage_protection": {
                        "type": "enum", "label": "Overvoltage Protection",
                        "values": ["ok", "failed", "unknown", "none"],
                        "help": "none: this bank has no protection of its "
                                "own to report.",
                        "cloud_priority": "high",
                    },
                },
                "summary_fields": ["name", "supply", "voltage", "current"],
                "label_field": "name",
            },
            "power_source": {
                "label": "Power Source",
                "label_plural": "Power Sources",
                "id_format": {"type": "string", "max_length": 16},
                "state_variables": {
                    "name": {"type": "string", "label": "Name",
                             "cloud_priority": "low"},
                    "voltage": {"type": "number", "label": "Voltage",
                                "unit": "V", "min": 0, "max": 300,
                                "step": 0.01, "cloud_priority": "low"},
                    "current": {"type": "number", "label": "Current",
                                "unit": "A", "min": 0, "max": 16,
                                "step": 0.001, "cloud_priority": "low"},
                    "rated_current": {"type": "number",
                                      "label": "Rated Current", "unit": "A",
                                      "min": 0, "step": 0.1,
                                      "cloud_priority": "low"},
                    "fed_from": {"type": "string", "label": "Fed From",
                                 "help": "The source this one is fed from "
                                         "(the 24 V supply feeds the 5 V and "
                                         "12 V ones).",
                                 "cloud_priority": "low"},
                },
                "summary_fields": ["name", "voltage", "current"],
                "label_field": "name",
            },
            "sensor": {
                "label": "External Sensor",
                "label_plural": "External Sensors",
                "id_format": {"type": "integer", "min": 1, "max": 2},
                "state_variables": {
                    "name": {"type": "string", "label": "Name",
                             "cloud_priority": "low"},
                    "temperature": {"type": "number", "label": "Temperature",
                                    "unit": "°C", "min": -20, "max": 80,
                                    "step": 0.1, "cloud_priority": "low"},
                    "humidity": {"type": "number", "label": "Humidity",
                                 "unit": "%", "min": 0, "max": 100,
                                 "step": 0.1, "cloud_priority": "low"},
                    "dew_point": {"type": "number", "label": "Dew Point",
                                  "unit": "°C", "step": 0.1,
                                  "cloud_priority": "low"},
                    "dew_point_difference": {
                        "type": "number", "label": "Dew Point Distance",
                        "unit": "°C", "step": 0.1,
                        "help": "How far the temperature is above the dew "
                                "point.",
                        "cloud_priority": "low",
                    },
                    "pressure": {"type": "number", "label": "Air Pressure",
                                 "unit": "hPa", "min": 300, "max": 1100,
                                 "step": 0.1, "cloud_priority": "low"},
                },
                "summary_fields": ["name", "temperature", "humidity"],
                "label_field": "name",
            },
            "meter": {
                "label": "Meter",
                "label_plural": "Meters",
                "dynamic": True,
                "id_format": {"type": "string", "max_length": 64},
                # Each meter's readings are the fields the unit describes for
                # it, published at register_child(schema=...).
                "state_variables": {
                    "name": {"type": "string", "label": "Name",
                             "cloud_priority": "low"},
                },
                "summary_fields": ["name"],
                "label_field": "name",
            },
        },
        "quick_actions": ["port_on", "port_off", "port_power_cycle"],
        "actions": [
            {"id": "refresh", "kind": "command", "icon": "refresh-cw"},
            {
                "id": "restart_controller",
                "kind": "command",
                "icon": "server",
                "confirm": (
                    "Restart the PDU's controller. The ports keep their "
                    "power; the unit is off the network while it restarts. "
                    "Continue?"
                ),
            },
        ],
        "commands": {
            "refresh": {
                "label": "Refresh Status",
                "params": {},
                "help": "Reads every port, meter, sensor and name from the "
                        "unit now.",
            },
            "port_on": {
                "label": "Turn Port On",
                "params": {
                    "port": {"type": "child_id", "child_type": "port",
                             "required": True, "label": "Port"},
                },
                "sets": {"state": True},
                "help": "Switches the port on: its front panel light turns "
                        "on and the device on it gets power. A tripped "
                        "eFuse is reset.",
            },
            "port_off": {
                "label": "Turn Port Off",
                "params": {
                    "port": {"type": "child_id", "child_type": "port",
                             "required": True, "label": "Port"},
                },
                "sets": {"state": False},
                "help": "Switches the port off and the device on it loses "
                        "power.",
            },
            "port_toggle": {
                "label": "Toggle Port",
                "params": {
                    "port": {"type": "child_id", "child_type": "port",
                             "required": True, "label": "Port"},
                },
                "help": "Switches the port off if it is on, on if it is "
                        "off.",
            },
            "port_power_cycle": {
                "label": "Power Cycle Port",
                "params": {
                    "port": {"type": "child_id", "child_type": "port",
                             "required": True, "label": "Port"},
                },
                "help": "Switches a port that is on off, then on again "
                        "after the port's Reset Duration (set on the unit), "
                        "so the device on it restarts.",
            },
            "port_timed_switch": {
                "label": "Switch Port for a Time",
                "params": {
                    "port": {"type": "child_id", "child_type": "port",
                             "required": True, "label": "Port"},
                    "first": {"type": "enum", "values": ["on", "off"],
                              "required": True, "default": "off",
                              "label": "Switch To"},
                    "seconds": {"type": "integer", "min": 1, "max": 65535,
                                "required": True, "default": 10,
                                "label": "Wait (sec)"},
                    "then": {"type": "enum", "values": ["on", "off"],
                             "required": True, "default": "on",
                             "label": "Then Switch To"},
                },
                "help": "Switches the port to one state, waits, then "
                        "switches it to the other; the unit runs the timer "
                        "itself.",
            },
            "port_cancel_timer": {
                "label": "Cancel Port Timer",
                "params": {
                    "port": {"type": "child_id", "child_type": "port",
                             "required": True, "label": "Port"},
                },
                "help": "Stops a power cycle or timed switch in progress on "
                        "the port, which stays as it is now.",
            },
            "restart_controller": {
                "label": "Restart Controller",
                "params": {},
                # The controller is off the network while it restarts, so
                # without this the platform reports a fault for something we
                # asked for. NOT measured (no unit on the bench); generous on
                # purpose, as surgex_axess_elite_plus and apc_rack_pdu are.
                "restarts_device_for": 90,
                "help": "Restarts the PDU's controller. The ports keep their "
                        "power; the network interface is away while it "
                        "restarts.",
            },
        },
    }

    def __init__(self, device_id: str, config: dict[str, Any], state, events):
        self._auth_mode = "none"  # none | session | basic
        self._csrf: str | None = None
        self._basic: str | None = None
        self._login_gen = 0
        self._login_lock = asyncio.Lock()
        self._first_status: dict[str, Any] | None = None
        # The device's own sensor description, kept between full reads.
        self._layout: list[dict[str, Any]] = []
        self._layout_sensors: int | None = None
        self._need_full = True
        self._polls = 0
        self._meter_schemas: dict[str, dict[str, Any]] = {}
        self._meters_seen: set[str] = set()
        super().__init__(device_id, config, state, events)

    # ── Connect ──────────────────────────────────────────────────────────

    async def _post_connect(self) -> None:
        """Sign in, and read everything once: a refused credential fails the
        connect here, before the device is reported online."""
        self._layout = []
        self._layout_sensors = None
        self._need_full = True
        self._polls = 0
        await self._sign_in(self._login_gen)
        self._first_status = await self._status(FULL_COMPONENTS)

    async def _initial_sync(self) -> None:
        status, self._first_status = self._first_status, None
        if status is not None:
            self._apply(status, full=True)
            self._polls = 1  # this read was the first poll's

    async def refresh_children(self) -> dict[str, Any]:
        self._apply(await self._status(FULL_COMPONENTS), full=True)
        return {
            "ports": len(self.list_children("port")),
            "banks": len(self.list_children("bank")),
            "power_sources": len(self.list_children("power_source")),
            "meters": len(self.list_children("meter")),
        }

    # ── Poll ─────────────────────────────────────────────────────────────

    async def poll(self) -> None:
        full = self._need_full or self._polls % FULL_EVERY_POLLS == 0
        self._polls += 1
        status = await self._status(FULL_COMPONENTS if full else POLL_COMPONENTS)
        self._apply(status, full=full)
        if self._need_full and not full:
            # The layout moved under a values-only read (a sensor plugged in
            # or out): read the description now rather than a poll later.
            self._apply(await self._status(FULL_COMPONENTS), full=True)

    # ── Commands ─────────────────────────────────────────────────────────

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = params or {}
        if command == "refresh":
            self._apply(await self._status(FULL_COMPONENTS), full=True)
            return None
        if command == "restart_controller":
            try:
                await self._command({"cmd": CMD_RESTART_DEVICE}, read_back=False)
            except (ConnectionError, OSError, httpx.HTTPError) as exc:
                # A controller that restarts before its answer is out has
                # done what was asked.
                log.info("[%s] Restart sent; the unit dropped the request "
                         "as it restarted (%s).", self.device_id, exc)
            return None

        port = self._port_param(params)
        if command == "port_on":
            await self._switch(port, True)
        elif command == "port_off":
            await self._switch(port, False)
        elif command == "port_toggle":
            current = self._child_value("port", port, "state")
            if current is None:
                await self._command({"cmd": CMD_SWITCH, "p": port, "s": 2})
            else:
                await self._switch(port, not current, toggle=True)
        elif command == "port_power_cycle":
            if self._child_value("port", port, "state") is False:
                raise CommandRefusedError(
                    f"{self._port_name(port)} is off. A power cycle switches "
                    f"a port that is on off and back on; use Turn Port On."
                )
            await self._command({"cmd": CMD_RESET, "p": port})
        elif command == "port_timed_switch":
            first = 1 if str(params.get("first", "off")).lower() == "on" else 0
            then = 1 if str(params.get("then", "on")).lower() == "on" else 0
            seconds = int(params.get("seconds", 10))
            await self._command(
                {"cmd": CMD_BATCH, "p": port, "a1": first, "s": seconds, "a2": then}
            )
        elif command == "port_cancel_timer":
            await self._command({"cmd": CMD_CANCEL_BATCH, "p": port})
        else:
            raise ValueError(f"Unknown command: {command}")
        return None

    def _port_param(self, params: dict[str, Any]) -> int:
        try:
            port = int(params.get("port"))
        except (TypeError, ValueError):
            raise CommandRefusedError("Choose a port.") from None
        known = self.list_children("port")
        if known and port not in known:
            raise CommandRefusedError(f"The unit has no port {port}.")
        return port

    async def _switch(self, port: int, on: bool, toggle: bool = False) -> None:
        """Switch, and read back until the unit reports it. A port switched
        while others are still switching waits for the unit's gap between
        ports, so give it a few seconds before calling it refused."""
        args = {"cmd": CMD_SWITCH, "p": port, "s": 2 if toggle else int(on)}
        status = await self._command(args)
        deadline = time.monotonic() + SWITCH_SETTLE_S
        while True:
            outputs = status.get("outputs") if isinstance(status, dict) else None
            reported = None
            if isinstance(outputs, list) and 0 < port <= len(outputs):
                entry = outputs[port - 1]
                if isinstance(entry, dict) and "state" in entry:
                    reported = bool(entry.get("state"))
            if reported is on:
                return
            fault = self._child_value("port", port, "fault")
            if on and fault in FAULT_SENTENCE:
                self._refuse(f"{self._port_name(port)} stays off: "
                             f"{FAULT_SENTENCE[fault]}.")
            if time.monotonic() >= deadline:
                word = "on" if on else "off"
                self._refuse(f"{self._port_name(port)} still reads "
                             f"{'off' if on else 'on'} {SWITCH_SETTLE_S:.0f} s "
                             f"after it was switched {word}.")
            await asyncio.sleep(SWITCH_RECHECK_S)
            status = await self._status(POLL_COMPONENTS)
            self._apply(status, full=False)

    def _refuse(self, sentence: str) -> None:
        self.set_state("last_error", sentence)
        raise CommandRefusedError(sentence)

    async def _command(
        self, args: dict[str, Any], read_back: bool = True
    ) -> dict[str, Any]:
        """Send one CGI command; its reply carries the poll's components, so
        the state it leaves is applied at once."""
        status = await self._status(
            POLL_COMPONENTS if read_back else C_MISC, args
        )
        if read_back:
            self._apply(status, full=False)
        return status

    # ── HTTP ─────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        if self._auth_mode == "basic" and self._basic:
            return {"Authorization": self._basic}
        if self._auth_mode == "session" and self._csrf:
            return {"sessionidX": self._csrf}
        return {}

    def _credentials(self) -> tuple[str, str]:
        username = str(self.config.get("username") or "").strip() or "admin"
        return username, str(self.config.get("password") or "")

    async def _sign_in(self, seen_gen: int) -> None:
        """Sign in again unless another request already has since seen_gen.

        A session comes from login.json (manual 4.4); its cookie rides every
        later request in the HTTP client's own jar. A unit whose firmware
        has no login.json answers 404 and gets HTTP Basic instead."""
        async with self._login_lock:
            if self._login_gen != seen_gen:
                return
            self._login_gen += 1
            self._csrf = None
            self._basic = None
            username, password = self._credentials()
            if not password:
                self._auth_mode = "none"
                return
            response = await self.transport.request(
                "POST", LOGIN_PATH,
                form_data={"username": username, "password": password},
            )
            self._check_https_redirect(response)
            body = self._json(response)
            code = response.status_code
            if code in (404, 405) or (code == 200 and not isinstance(body, dict)):
                # No session sign-in here: firmware from before it, which
                # takes HTTP Basic (the wiki's interface page).
                token = base64.b64encode(
                    f"{username}:{password}".encode()
                ).decode()
                self._auth_mode = "basic"
                self._basic = f"Basic {token}"
                return
            if code == 200 and isinstance(body, dict) and \
                    _integer(body.get("login")) == 1:
                self._auth_mode = "session"
                csrf = body.get("sessionidX")
                self._csrf = str(csrf) if csrf else None
                return
            # Refused. A unit with no password set may refuse a sign-in it
            # has no use for, so ask once without one before calling the
            # password wrong.
            self._auth_mode = "none"
            probe = await self.transport.request(
                "GET", STATUS_PATH, params={"components": str(C_MISC)}
            )
            self._check_https_redirect(probe)
            if probe.status_code == 200 and isinstance(self._json(probe), dict):
                log.info("[%s] The unit asks for no password; signing in "
                         "without one.", self.device_id)
                return
            raise ConnectionFaultError(
                f"The PDU refused the password for {username} "
                f"(HTTP {response.status_code}).",
                code="auth_failed",
            )

    async def _status(
        self, components: int, cmd: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """One status.json request, a command's CGI parameters with it.

        In a session a command goes as a POST, which Session mode requires
        for switching (manual 4.4); everything else is a GET. A refused
        session signs in again once."""
        params = {"components": str(components)}
        for key, value in (cmd or {}).items():
            params[key] = str(value)
        for attempt in (1, 2):
            seen_gen = self._login_gen
            if cmd and self._auth_mode == "session":
                response = await self.transport.request(
                    "POST", STATUS_PATH, form_data=params, headers=self._headers()
                )
            else:
                response = await self.transport.request(
                    "GET", STATUS_PATH, params=params, headers=self._headers()
                )
            self._check_https_redirect(response)
            code = response.status_code
            refused = code in (401, 403) or 300 <= code < 400
            if refused and self._auth_mode == "session" and attempt == 1:
                await self._sign_in(seen_gen)
                continue
            if refused:
                if cmd:
                    sentence = (
                        "The PDU refused this command for the signed-in "
                        f"account (HTTP {code})."
                    )
                    self.set_state("last_error", sentence)
                    raise CommandRefusedError(sentence)
                if self._auth_mode == "none" and not self._credentials()[1]:
                    raise ConnectionFaultError(
                        "The PDU asks for a username and password and none "
                        "is entered.", code="auth_failed",
                    )
                raise ConnectionFaultError(
                    f"The PDU refused the password for "
                    f"{self._credentials()[0]} (HTTP {code}).",
                    code="auth_failed",
                )
            if code != 200:
                if cmd:
                    sentence = f"The PDU answered the command with HTTP {code}."
                    self.set_state("last_error", sentence)
                    raise CommandRefusedError(sentence)
                raise ConnectionError(
                    f"The PDU answered a status read with HTTP {code}."
                )
            body = self._json(response)
            if not isinstance(body, dict):
                raise ConnectionError(
                    "The PDU's status reply is not the JSON the interface "
                    "sends."
                )
            return body
        raise ConnectionError("The PDU did not answer the status read.")

    @staticmethod
    def _check_https_redirect(response: Any) -> None:
        """A unit whose HTTP server is set to redirect to HTTPS (the
        console's HTTPS_REDIR) answers every plain-HTTP request with a
        redirect: a setting here, not a refused password."""
        if not 300 <= response.status_code < 400:
            return
        headers = getattr(response, "headers", None) or {}
        location = str(headers.get("location") or headers.get("Location") or "")
        if location.lower().startswith("https://"):
            raise ConnectionFaultError(
                "The PDU redirects HTTP to HTTPS, so it answers only with Use "
                "HTTPS on (port 443 unless its HTTP page sets another).",
                code="invalid_config",
            )

    @staticmethod
    def _json(response: Any) -> Any:
        data = getattr(response, "json_data", None)
        if data is not None:
            return data
        try:
            return json.loads(getattr(response, "text", "") or "")
        except ValueError:
            return None

    # ── Applying a status reply ──────────────────────────────────────────

    def _apply(self, status: dict[str, Any], full: bool) -> None:
        if full:
            self._apply_identity(status)
        descr = status.get("sensor_descr")
        fresh = isinstance(descr, list)
        if fresh:
            self._layout = [d for d in descr if isinstance(d, dict)]
            summary = status.get("port_summary")
            if isinstance(summary, dict):
                self._layout_sensors = _integer(summary.get("num_sensors"))
            self._need_full = False
        self._apply_ports(status)
        self._apply_banks(status)
        values = status.get("sensor_values")
        if isinstance(values, list):
            if self._layout_moved(status, values):
                self._need_full = True
                return
            self._apply_sensors(values, fresh)

    def _apply_identity(self, status: dict[str, Any]) -> None:
        misc = status.get("misc")
        if isinstance(misc, dict):
            for var, key in (("model", "product_name"),
                             ("firmware", "firm_v"),
                             ("hostname", "hostname")):
                if misc.get(key) is not None:
                    self.set_state(var, str(misc[key]))
            uptime = _integer(misc.get("uptime"))
            if uptime is not None:
                self.set_state("uptime", uptime)
        ethernet = status.get("ethernet")
        if isinstance(ethernet, dict) and ethernet.get("mac"):
            self.set_state("mac_address", str(ethernet["mac"]).lower())

    def _layout_moved(self, status: dict[str, Any], values: list[Any]) -> bool:
        """True when the readings no longer fit the description held: a
        sensor plugged in or out, or the first read of a connection."""
        if not self._layout:
            return True
        summary = status.get("port_summary")
        if isinstance(summary, dict) and self._layout_sensors is not None:
            if _integer(summary.get("num_sensors")) != self._layout_sensors:
                return True
        if len(values) != len(self._layout):
            return True
        for described, measured in zip(self._layout, values):
            if not isinstance(measured, dict):
                return True
            if described.get("type") != measured.get("type"):
                return True
        return False

    # Ports: outputs[] and hardware.banks[] -------------------------------

    def _apply_ports(self, status: dict[str, Any]) -> None:
        outputs = status.get("outputs")
        if not isinstance(outputs, list):
            return
        count = min(len(outputs), 21)
        self.set_state("port_count", count)
        sockets, banks = self._port_places(status)
        for index in range(count):
            entry = outputs[index]
            if not isinstance(entry, dict):
                continue
            port = index + 1
            if not self.is_child_registered("port", port):
                self.register_child("port", port, initial_state={
                    "fault": "none", "timer_remaining": 0,
                })
            updates: dict[str, Any] = {}
            if entry.get("name") is not None:
                updates["name"] = str(entry["name"])
            if "state" in entry:
                updates["state"] = bool(_integer(entry.get("state")))
            switches = _integer(entry.get("sw_cnt"))
            if switches is not None:
                updates["switch_count"] = switches
            batch = entry.get("batch")
            if isinstance(batch, list) and len(batch) > 1:
                remaining = _integer(batch[1])
                updates["timer_remaining"] = max(0, remaining or 0)
            if port in sockets:
                updates["socket"] = sockets[port]
            if port in banks:
                updates["bank"] = banks[port]
            if updates:
                self.set_child_state_batch("port", port, updates)

    def _port_places(
        self, status: dict[str, Any]
    ) -> tuple[dict[int, str], dict[int, str]]:
        """port -> socket mark (A1) and port -> bank id (A), from the banks."""
        sockets: dict[int, str] = {}
        banks: dict[int, str] = {}
        hardware = status.get("hardware")
        if not isinstance(hardware, dict):
            return sockets, banks
        for index, bank in enumerate(hardware.get("banks") or []):
            if not isinstance(bank, dict):
                continue
            bank_id = self._bank_id(bank, index)
            names = bank.get("port_names") or []
            for position, port in enumerate(bank.get("ports") or []):
                number = _integer(port)
                if number is None:
                    continue
                banks[number] = bank_id
                if position < len(names) and names[position]:
                    sockets[number] = str(names[position])
        return sockets, banks

    @staticmethod
    def _bank_id(bank: dict[str, Any], index: int) -> str:
        name = str(bank.get("name") or "")
        if name.lower().startswith("bank ") and name[5:].strip():
            return name[5:].strip()
        return chr(ord("A") + index) if index < 26 else str(index + 1)

    def _apply_banks(self, status: dict[str, Any]) -> None:
        hardware = status.get("hardware")
        if not isinstance(hardware, dict):
            return
        seen: set[str] = set()
        for index, bank in enumerate(hardware.get("banks") or []):
            if not isinstance(bank, dict):
                continue
            bank_id = self._bank_id(bank, index)
            if not self.is_child_registered("bank", bank_id):
                self.register_child("bank", bank_id)
            fuse = bank.get("fuse")
            ovp = "none" if fuse is None else OVP_STATE.get(_integer(fuse), "unknown")
            self.set_child_state_batch("bank", bank_id, {
                "name": str(bank.get("name") or f"Bank {bank_id}"),
                "overvoltage_protection": ovp,
            })
            seen.add(ovp)
        # The unit as a whole: the worst any bank with protection reports.
        for verdict in ("failed", "unknown", "ok"):
            if verdict in seen:
                self.set_state("overvoltage_protection", verdict)
                break

    # Sensors: sensor_descr + sensor_values ------------------------------

    def _apply_sensors(self, values: list[Any], fresh: bool) -> None:
        present_ext: set[int] = set()
        self._meters_seen: set[str] = set()
        faults = False
        for described, measured in zip(self._layout, values):
            kind = described.get("type")
            rows = measured.get("values")
            if not isinstance(rows, list):
                continue
            props = described.get("properties") or []
            if kind == T_LINE_METER:
                self._apply_line(described, rows)
            elif kind == T_SYSTEM:
                self._apply_system(described, rows)
            elif kind == T_BANK:
                faults = self._apply_bank_sensor(described, rows) or faults
            elif kind == T_POWER_SOURCE:
                self._apply_power_sources(described, rows)
            elif kind in T_EXT_SENSORS:
                present_ext |= self._apply_ext(described, rows)
            else:
                self._apply_meters(kind, described, props, rows)
        self.set_state("efuse_fault", faults)
        if fresh:
            # The description is the roster: a meter it no longer lists is gone.
            for meter_id in list(self.list_children("meter")):
                if meter_id not in self._meters_seen:
                    self.deregister_child("meter", meter_id)
                    self._meter_schemas.pop(meter_id, None)
        for sensor in range(1, EXT_SENSOR_PORTS + 1):
            if sensor in present_ext:
                continue
            if not self.is_child_registered("sensor", sensor):
                self.register_child(
                    "sensor", sensor, initial_state=self.child_fault("not_fitted")
                )
            else:
                self.set_child_state_batch(
                    "sensor", sensor, self.child_fault("not_fitted")
                )

    @staticmethod
    def _fields(block: dict[str, Any]) -> list[str]:
        return [_key(f.get("name")) for f in block.get("fields") or []
                if isinstance(f, dict)]

    def _apply_line(self, described: dict[str, Any], rows: list[Any]) -> None:
        names = self._fields(described)
        if not rows or not isinstance(rows[0], list):
            return
        for name, cell in zip(names, rows[0]):
            var = LINE_FIELDS.get(name)
            if var is not None:
                self.set_state(var, _number(_cell(cell)))

    def _apply_system(self, described: dict[str, Any], rows: list[Any]) -> None:
        groups = described.get("groups") or []
        if not groups or not rows or not isinstance(rows[0], list):
            return
        names = self._fields(groups[0]) if isinstance(groups[0], dict) else []
        group_rows = rows[0][0] if rows[0] and isinstance(rows[0][0], list) else []
        if not group_rows or not isinstance(group_rows[0], list):
            return
        for name, cell in zip(names, group_rows[0]):
            if name == "tcpu":
                self.set_state("internal_temperature", _number(_cell(cell)))

    def _apply_bank_sensor(
        self, described: dict[str, Any], rows: list[Any]
    ) -> bool:
        """Each bank: its ports' eFuse states and its supply's readings.
        Returns True when a port reports a fault."""
        groups = described.get("groups") or []
        props = described.get("properties") or []
        port_fields = self._fields(groups[0]) if len(groups) > 0 else []
        supply_fields = self._fields(groups[1]) if len(groups) > 1 else []
        any_fault = False
        for bank_index, (prop, row) in enumerate(zip(props, rows)):
            if not isinstance(prop, dict) or not isinstance(row, list):
                continue
            sub = prop.get("groups") or []
            ports = sub[0] if len(sub) > 0 and isinstance(sub[0], list) else []
            supplies = sub[1] if len(sub) > 1 and isinstance(sub[1], list) else []
            port_rows = row[0] if len(row) > 0 and isinstance(row[0], list) else []
            for port_prop, cells in zip(ports, port_rows):
                port = _integer(port_prop.get("id")) if isinstance(port_prop, dict) else None
                if port is None or not self.is_child_registered("port", port):
                    continue
                if not isinstance(cells, list):
                    continue
                reading = dict(zip(port_fields, (_cell(c) for c in cells)))
                updates: dict[str, Any] = {}
                if "state" in reading:
                    state = _integer(reading["state"])
                    fault = EFUSE_FAULT.get(state, "none")
                    updates["fault"] = fault
                    any_fault = any_fault or fault != "none"
                if "power" in reading:
                    updates["output_live"] = bool(_integer(reading["power"]))
                if updates:
                    self.set_child_state_batch("port", port, updates)
            bank_id = str(prop.get("id") or chr(ord("A") + bank_index))
            supply_rows = row[1] if len(row) > 1 and isinstance(row[1], list) else []
            if not supplies or not supply_rows or not isinstance(supply_rows[0], list):
                continue
            if not self.is_child_registered("bank", bank_id):
                self.register_child("bank", bank_id)
            reading = dict(zip(supply_fields, (_cell(c) for c in supply_rows[0])))
            updates = {"voltage": _number(reading.get("voltage")),
                       "current": _number(reading.get("current"))}
            supply = supplies[0] if isinstance(supplies[0], dict) else {}
            if supply.get("name"):
                updates["supply"] = str(supply["name"])
            self.set_child_state_batch("bank", bank_id, updates)
        return any_fault

    def _apply_power_sources(
        self, described: dict[str, Any], rows: list[Any]
    ) -> None:
        names = self._fields(described)
        props = described.get("properties") or []
        ids = [str(p.get("id") or "") for p in props if isinstance(p, dict)]
        for prop, cells in zip(props, rows):
            if not isinstance(prop, dict) or not isinstance(cells, list):
                continue
            source_id = str(prop.get("id") or "")
            if not source_id:
                continue
            if not self.is_child_registered("power_source", source_id):
                self.register_child("power_source", source_id)
            reading = dict(zip(names, (_cell(c) for c in cells)))
            fed_from = ""
            src = _integer(reading.get("src"))
            if src is not None and 0 <= src < len(ids):
                fed_from = ids[src]
            self.set_child_state_batch("power_source", source_id, {
                "name": str(prop.get("name") or source_id),
                "voltage": _number(reading.get("vout")),
                "current": _number(reading.get("iout")),
                "rated_current": _number(reading.get("imaxout")),
                "fed_from": fed_from,
            })

    def _apply_ext(self, described: dict[str, Any], rows: list[Any]) -> set[int]:
        names = self._fields(described)
        props = described.get("properties") or []
        present: set[int] = set()
        for index, (prop, cells) in enumerate(zip(props, rows)):
            if not isinstance(prop, dict) or not isinstance(cells, list):
                continue
            sensor = self._ext_port(prop, index)
            if sensor is None:
                continue
            if not self.is_child_registered("sensor", sensor):
                self.register_child("sensor", sensor)
            updates: dict[str, Any] = {"name": str(prop.get("name") or "")}
            for name, cell in zip(names, cells):
                var = EXT_FIELDS.get(name)
                if var is not None:
                    updates[var] = _number(_cell(cell))
            self.set_child_state_batch(
                "sensor", sensor, {**updates, **self.child_fault()}
            )
            present.add(sensor)
        return present

    @staticmethod
    def _ext_port(prop: dict[str, Any], index: int) -> int | None:
        """The connector a sensor is on: the id reads "1: 7106" (connector,
        then sensor), and real_id counts from 0."""
        head = str(prop.get("id") or "").split(":", 1)[0].strip()
        number = _integer(head) if head.isdigit() else None
        if number is None:
            real = _integer(prop.get("real_id"))
            number = real + 1 if real is not None else index + 1
        return number if 1 <= number <= EXT_SENSOR_PORTS else None

    # Anything else: meter children built from the device's description ----

    def _apply_meters(
        self, kind: Any, described: dict[str, Any], props: list[Any],
        rows: list[Any],
    ) -> None:
        if described.get("groups"):
            groups = described.get("groups") or []
            for prop, row in zip(props, rows):
                if not isinstance(prop, dict) or not isinstance(row, list):
                    continue
                sub_groups = prop.get("groups") or []
                for g_index, group in enumerate(groups):
                    if not isinstance(group, dict):
                        continue
                    subs = sub_groups[g_index] if g_index < len(sub_groups) else []
                    cells_list = row[g_index] if g_index < len(row) else []
                    for sub, cells in zip(subs or [], cells_list or []):
                        if not isinstance(sub, dict):
                            continue
                        meter_id = _slug(f"t{kind}_{prop.get('id')}_{sub.get('id')}")
                        name = " ".join(
                            str(x) for x in (prop.get("name"), sub.get("name"))
                            if x
                        )
                        self._meter(meter_id, name, group.get("fields") or [], cells)
        else:
            fields = described.get("fields") or []
            for prop, cells in zip(props, rows):
                if not isinstance(prop, dict):
                    continue
                meter_id = _slug(f"t{kind}_{prop.get('id')}")
                self._meter(meter_id, str(prop.get("name") or prop.get("id") or ""),
                            fields, cells)

    def _meter(
        self, meter_id: str, name: str, fields: list[Any], cells: Any
    ) -> None:
        if not isinstance(cells, list):
            return
        # A dynamic child's schema replaces the type's variables, so the name
        # is declared here as well.
        schema: dict[str, Any] = {
            "name": {"type": "string", "label": "Name", "cloud_priority": "low"},
        }
        props: list[str | None] = []
        for field in fields:
            if not isinstance(field, dict):
                props.append(None)
                continue
            prop = _slug(field.get("name"))
            if prop in ("name", "label", "online", "offline_reason",
                        "offline_detail") or prop in schema:
                prop = f"{prop}_value"
            unit = str(field.get("unit") or "")
            var: dict[str, Any] = {
                "type": "number" if (unit or "decPrecision" in field) else "string",
                "label": str(field.get("name") or prop),
                "cloud_priority": "low",
            }
            if unit:
                var["unit"] = unit
            schema[prop] = var
            props.append(prop)
        if len(schema) == 1:
            return
        self._meters_seen.add(meter_id)
        known = self._meter_schemas.get(meter_id)
        if known is not None and known != schema:
            self.deregister_child("meter", meter_id)
        if not self.is_child_registered("meter", meter_id):
            self.register_child("meter", meter_id, schema=schema)
            self._meter_schemas[meter_id] = schema
        updates: dict[str, Any] = {"name": name or meter_id}
        for prop, cell in zip(props, cells):
            if prop is None:
                continue
            value = _cell(cell)
            if schema[prop]["type"] == "number":
                updates[prop] = _number(value)
            else:
                updates[prop] = None if value is None else str(value)
        self.set_child_state_batch("meter", meter_id, updates)

    # ── Helpers ──────────────────────────────────────────────────────────

    def _child_value(self, child_type: str, local_id: Any, prop: str) -> Any:
        return self.get_child_state(child_type, local_id).get(prop)

    def _port_name(self, port: int) -> str:
        """Port 3 (A3), or Port 3 (A3) "Projector" once it has been named
        ("Power Port" is the unit's name for a port nobody has named)."""
        socket = self._child_value("port", port, "socket")
        name = self._child_value("port", port, "name")
        label = f"Port {port}" + (f" ({socket})" if socket else "")
        if name and name != "Power Port":
            label += f' "{name}"'
        return label
