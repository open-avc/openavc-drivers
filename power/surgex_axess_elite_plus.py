"""SurgeX Axess ELITE+ power conditioner driver (SNMP, ESP Axess Elite MIB).

The Axess ELITE+ (SX-AEP-159, SX-AEP-209, SX-AEP-1011i, SX-AEP-1611i) is a
rack power conditioner with nine or eleven individually switched outlets, two
auxiliary relays, outlet groups, stored sequences, triggers and system-level
metering. SurgeX publishes its SNMP MIB (``axesselite_v10.mib``, Electronic
Systems Protection enterprise 40079) on the product page, and the user manual
says the agent takes SNMP V1 through V3 with a table of community strings, so
this driver is a set of OIDs read off that MIB.

**Why SNMP, of the four ways in.** The manual names a REST API ("full protocol
details are available at ametekesp.com") that is not published there; a CLI
over SSH whose sign-in is a private key downloaded from the unit and run with
``ssh -T`` (no terminal, no user name given), where OpenAVC's SSH transport
always asks for a terminal; and the same CLI on RS-232 with a login whose
prompts the manual does not show. The MIB is the one interface whose every
object, type, range and scale is in a public SurgeX document.

**Why Python, not YAML.** ``snmp`` is a Python-only transport: a request is a
list of OIDs, not a send string. The rosters (outlets, groups, sequences,
triggers, AutoPing hosts) are walked off the unit, because groups, sequences
and triggers are whatever the site set up in the web interface.

**Polling, deliberately.** The unit sends traps (outlet state changes, trigger
and clear, a sequence run, a failed sign-in) to receivers set in its web
interface, and the platform has no listener for a unicast trap port (the
``snmp_v2c`` row of the pending-push tracker in the driver roadmap). Nothing
here is lost between polls: an outlet changes only when commanded and stays
changed, a trigger's state holds while its condition does, and the input fault
reads back as the most recent one detected. Traps would only make a change
show sooner.

Source:
  Axess ELITE+ MIB v1.0 (axesselite_v10.mib, 2021-03-26), the objects' own
  SYNTAX and DESCRIPTION clauses —
  https://www.ametekesp.com/-/media/ametekesp/products/surgex/axess-elite-plus/axesselite_v10.mib
  Axess ELITE+ User Manual B01-00014 Rev A (2025): the SNMP setup page, the
  outlet initial states, and the CLI screenshots whose readings settle the
  per-outlet scales the MIB leaves unstated —
  https://www.ametekesp.com/-/media/ametekesp/products/surgex/axess-elite-plus/b01-00014_rev-a_axess-elite-plus_user-manual.pdf
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openavc.drivers.base import BaseDriver
from openavc.transport.snmp import SnmpError
from openavc.utils.logger import get_logger

try:  # OpenAVC 0.37.1 and later: the device's reason reaches Send Command.
    from openavc.drivers.base import CommandRefusedError
except ImportError:  # 0.37.0 and earlier refuse the same way, untyped.
    CommandRefusedError = RuntimeError

log = get_logger(__name__)

# One request's worth of varbinds: keeps a reply inside one datagram on any
# path, the same number apc_rack_pdu and snmp_v2c settled on.
VARBINDS_PER_REQUEST = 16

# Ceiling on a column walk. The MIB indexes every table with at most 255
# rows; the limit stops an agent that never leaves the subtree.
WALK_LIMIT = 256

# Names, initial states, reboot times and the rosters themselves change only
# when someone edits the unit's web interface, so they are read at connect,
# on Refresh, and every this-many polls (once a minute at the default 10 s).
SLOW_EVERY_POLLS = 6

# The MIB-II system group. Not in SurgeX's MIB; read when the agent has it so
# the card shows its description and uptime, blank when it does not.
SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"

# axesselite MODULE-IDENTITY ::= { espSurgex 1000 }, espSurgex ::= { enterprises 40079 }.
AXESSELITE = "1.3.6.1.4.1.40079.1000"
DEVICE = f"{AXESSELITE}.1"  # scalars: DEVICE.<n>.0
OUTLETS = f"{AXESSELITE}.2.1"  # outletsEntry: OUTLETS.<column>.<index>
GROUPS = f"{AXESSELITE}.3.1"  # outletGroupsEntry
SEQUENCES = f"{AXESSELITE}.4.1"  # sequenceEntry
TRIGGERS = f"{AXESSELITE}.5.1"  # triggerEntry
PING_HOSTS = f"{AXESSELITE}.6.1"  # pingHostsEntry

# Scalars written by commands (device 21-24).
ENERGY_USAGE_RESET = f"{DEVICE}.21.0"
REBOOT_DEVICE = f"{DEVICE}.23.0"
TEMPERATURE_UNIT = f"{DEVICE}.24.0"
YES = 2  # energyUsageReset / rebootDevice: no(1), yes(2)

# ── Enum vocabularies, each read off the object's SYNTAX clause ─────────

# inputState
INPUT_STATE = {
    0: "no_fault",
    1: "no_ground",
    2: "reverse_polarity",
    3: "over_voltage_line_neutral",
    4: "under_voltage_line_neutral",
    5: "over_voltage_neutral_ground",
    6: "under_voltage_neutral_ground",
    7: "overload",
    8: "over_temperature",
    9: "under_temperature",
    10: "over_humidity",
    11: "under_humidity",
    12: "over_frequency",
    13: "under_frequency",
}
# powerFactorIndicator
POWER_FACTOR_TYPE = {0: "lagging", 1: "unity", 2: "leading"}
# temperatureUnit
TEMPERATURE_UNITS = {1: "F", 2: "C"}
# outletStatus and outletGroupStatus: on(1), off(2), rebooting(3). The same
# numbers are what a SET sends to switch.
SWITCH_STATUS = {1: "on", 2: "off", 3: "rebooting"}
SWITCH_VALUE = {"on": 1, "off": 2, "reboot": 3}
# outletInitialState. 2 is not used by the MIB.
INITIAL_STATE = {
    0: "always_on",
    1: "always_off",
    3: "on",
    4: "off",
    5: "last",
    6: "reboot_only",
}
# sequenceStatus
SEQUENCE_STATUS = {0: "none", 1: "on", 2: "off"}
SEQUENCE_RUN = 1  # writing on(1) commands the sequence (trap sequenceRun)
# triggerEnabled no(0)/yes(1); triggerState waiting(0)/triggered(1)
YES_NO = {0: False, 1: True}

# The outlet initial states the manual says ignore a command: Always On
# ignores power off and reboot, Reboot Only ignores power off (B01-00014 Rev A,
# Outlet Configuration). Always Off never passes power.
IGNORES = {
    "off": ("always_on", "reboot_only"),
    "reboot": ("always_on", "always_off"),
    "on": ("always_off",),
}
INITIAL_STATE_WORDS = {
    "always_on": "Always On",
    "always_off": "Always Off",
    "reboot_only": "Reboot Only",
}


@dataclass(frozen=True)
class Column:
    """One MIB column (or scalar) as this driver publishes it.

    ``scale`` converts the MIB's integer to the declared unit; ``enum`` maps
    an enumerated integer to the name a panel shows; ``text`` keeps a
    DisplayString as text.
    """

    prop: str
    number: int
    scale: float = 1.0
    enum: dict[int, Any] | None = None
    text: bool = False


# Device scalars. Every one is read each poll (two requests).
SCALARS = (
    Column("device_name", 1, text=True),
    Column("firmware", 2, text=True),
    Column("nominal_voltage", 3),
    Column("manufacturer", 4, text=True),
    Column("model", 5, text=True),
    Column("device_time", 6, text=True),
    Column("input_state", 7, enum=INPUT_STATE),
    Column("reboot_time", 8),
    Column("device_index", 9),
    Column("voltage_ln", 10, scale=0.001),  # "(*0.001)"
    Column("voltage_ng", 11, scale=0.001),  # "(*0.001)"
    Column("current_a", 12, scale=0.01),  # "(*0.01)"
    Column("power_w", 13),  # "Watts"
    Column("power_factor", 14, scale=0.001),  # "(* 0.001)"
    Column("power_factor_type", 15, enum=POWER_FACTOR_TYPE),
    Column("crest_factor", 16, scale=0.001),  # "(* 0.001)"
    Column("_temperature", 17, scale=0.1),  # "(* 0.1)", in temperatureUnit
    Column("frequency_hz", 18, scale=0.001),  # "(* 0.001)"
    Column("energy_kwh", 19, scale=0.001),  # "Energy in Wh"
    Column("energy_since", 20, text=True),
    Column("temperature_unit", 24, enum=TEMPERATURE_UNITS),
    Column("nominal_frequency", 25),
    Column("voltage_limit", 26),
    Column("device_type", 27, text=True),
)

# Outlet columns. The MIB states no unit or scale for the five metering
# columns ("The display IoutRms of the outlet"). The user manual's CLI
# screenshots read the same quantities with the decimals of the device-level
# objects whose scale the MIB does state (Outlet1_IoutRms 0.11 and
# TotalCurrent 1.03 at *0.01; Outlet1_PowerFactor 0.039 and PowerFactor 0.071
# at *0.001; Outlet1_CrestFactor 3.594 and TotalCrestFactor 2.475 at *0.001;
# Outlet1_ActivePower 1 and TotalPower 16 in whole Watts), so those are the
# scales used. Peak current is taken at the RMS current's scale. All five are
# the first thing to check against a unit.
OUTLET_LIVE = (
    Column("status", 4, enum=SWITCH_STATUS),
    Column("current_a", 8, scale=0.01),
    Column("power_w", 9),
    Column("power_factor", 10, scale=0.001),
    Column("crest_factor", 11, scale=0.001),
    Column("peak_current_a", 12, scale=0.01),
)
OUTLET_SLOW = (
    Column("outlet_id", 2, text=True),
    Column("name", 3, text=True),
    Column("initial_state", 5, enum=INITIAL_STATE),
    Column("reboot_time", 7),
)
GROUP_LIVE = (Column("status", 4, enum=SWITCH_STATUS),)
GROUP_SLOW = (
    Column("group_id", 2, text=True),
    Column("name", 3, text=True),
)
SEQUENCE_LIVE = (Column("status", 4, enum=SEQUENCE_STATUS),)
SEQUENCE_SLOW = (
    Column("sequence_id", 2, text=True),
    Column("name", 3, text=True),
)
TRIGGER_LIVE = (
    Column("enabled", 5, enum=YES_NO),
    Column("triggered", 6, enum=YES_NO),
)
TRIGGER_SLOW = (
    Column("trigger_id", 2, text=True),
    Column("name", 3, text=True),
    Column("trigger_type", 4, text=True),
)
PING_LIVE = (
    Column("successes", 3),
    Column("failures", 4),
    Column("last_success", 5, text=True),
    Column("last_failure", 6, text=True),
)
PING_SLOW = (Column("address", 2, text=True),)


@dataclass(frozen=True)
class Table:
    """A MIB table published as one child type.

    ``roster`` is the column walked to learn which rows exist: one the table
    always fills, never the index column (an agent may leave an index column
    unreadable, which is what SMIv2 asks of it).
    """

    child_type: str
    entry: str
    roster: int
    live: tuple[Column, ...]
    slow: tuple[Column, ...]


TABLES = (
    Table("outlet", OUTLETS, 4, OUTLET_LIVE, OUTLET_SLOW),
    Table("group", GROUPS, 4, GROUP_LIVE, GROUP_SLOW),
    Table("sequence", SEQUENCES, 4, SEQUENCE_LIVE, SEQUENCE_SLOW),
    Table("trigger", TRIGGERS, 6, TRIGGER_LIVE, TRIGGER_SLOW),
    Table("ping_host", PING_HOSTS, 2, PING_LIVE, PING_SLOW),
)
TABLE_BY_TYPE = {table.child_type: table for table in TABLES}


class SurgeXAxessElitePlusDriver(BaseDriver):
    """SurgeX Axess ELITE+ over SNMP (ESP Axess Elite MIB)."""

    DRIVER_INFO = {
        "id": "surgex_axess_elite_plus",
        "name": "SurgeX Axess ELITE+",
        "manufacturer": "SurgeX",
        "category": "power",
        "version": "1.0.0",
        "author": "OpenAVC",
        # transport: snmp needs 0.34.0; confirm on Reset Energy Meter 0.36.0.
        "min_platform_version": "0.36.0",
        "description": (
            "Controls SurgeX Axess ELITE+ power conditioners over SNMP. "
            "Outlets, outlet groups, stored sequences, triggers and AutoPing "
            "hosts are children read off the unit, with outlet and group "
            "on, off and reboot, sequence runs, line voltage, current, power, "
            "frequency, energy, temperature and the input fault the unit "
            "last detected."
        ),
        "source_url": (
            "https://www.ametekesp.com/-/media/ametekesp/products/surgex/"
            "axess-elite-plus/axesselite_v10.mib"
        ),
        "tags": ["power-conditioner", "pdu", "power", "outlet-control",
                 "rack", "snmp", "metering", "sequencing"],
        "verified": False,
        "simulated": True,
        "protocols": ["snmp"],
        "ports": [161],
        "transport": "snmp",
        "discovery": {
            # 40079 is Electronic Systems Protection, Inc. (SurgeX's maker) in
            # the IANA enterprise registry, and the MIB roots at it. Whether
            # the agent's sysObjectID sits under it is not documented, so this
            # narrows a scanned SNMP host only if it does.
            "snmp_pen": 40079,
            # IEEE MA-L AC-A6-67 is Electronic Systems Protection, Inc.; the
            # manual's Network Setup screenshot shows a unit at AC:A6:67:44:00:08.
            "oui": ["ac:a6:67"],
            "manufacturer_alias": [
                "surgex", "electronic systems protection", "ametek esp",
            ],
        },
        "compatible_models": [
            {
                "manufacturer": "SurgeX",
                "models": ["SX-AEP-159", "SX-AEP-209"],
                "confidence": "untested",
                "notes": (
                    "Axess ELITE+ 120 V: nine switched NEMA outlets (the "
                    "SX-AEP-209 has two of them 5-20R). The outlet roster is "
                    "read off the unit."
                ),
            },
            {
                "manufacturer": "SurgeX",
                "models": ["SX-AEP-1011i", "SX-AEP-1611i"],
                "confidence": "untested",
                "notes": (
                    "Axess ELITE+ 240 V: eleven switched IEC C13 outlets."
                ),
            },
        ],
        "help": {
            "overview": (
                "The Axess ELITE+ is the SurgeX power conditioner with "
                "switched outlets. Each outlet is a child here, so a button, "
                "macro or schedule can switch one, reboot a stuck device, or "
                "run a power sequence stored on the unit. Outlet groups and "
                "sequences made in the unit's web interface appear "
                "automatically. Line voltage, current, power, frequency, "
                "energy, temperature, trigger states and the last input fault "
                "come through as live values you can alert on."
            ),
            "setup": (
                "1. Give the Axess ELITE+ an IP address and open its web "
                "interface.\n"
                "2. Under Setup > Network Advanced > SNMP, enable SNMP on port "
                "161 and add an SNMP V1 community with Read/Write access. A "
                "second community with Read access only can be the Read "
                "Community here. Set Source to this OpenAVC server's address "
                "so nothing else can switch outlets.\n"
                "3. Enter the unit's IP address and the community names here, "
                "then run Test Connection. It reads the unit's model and "
                "outlet list back, and writes nothing.\n"
                "4. Outlets, groups, sequences and triggers appear as children "
                "once connected. After changing them in the web interface, "
                "use Refresh from Device, or wait a minute."
            ),
        },
        "default_config": {
            "host": "",
            "port": 161,
            "community": "public",
            "write_community": "private",
            "timeout": 2.0,
            "retries": 1,
            "poll_interval": 10,
        },
        "config_schema": {
            "host": {"type": "string", "required": True,
                     "label": "IP Address",
                     "description": "The Axess ELITE+'s IP address or "
                                    "hostname."},
            "port": {"type": "integer", "default": 161,
                     "label": "Port", "min": 1, "max": 65535,
                     "description": "The SNMP port set on the unit's SNMP "
                                    "page. 161 unless it was changed."},
            "community": {
                "type": "string", "default": "public", "secret": True,
                "label": "Read Community",
                "description": "An SNMP community on the unit with Read or "
                               "Read/Write access.",
            },
            "write_community": {
                "type": "string", "default": "private", "secret": True,
                "label": "Write Community",
                "description": "An SNMP community on the unit with "
                               "Read/Write access, which switching outlets "
                               "needs. Leave blank to write with the read "
                               "community.",
            },
            "timeout": {"type": "number", "default": 2.0,
                        "label": "Timeout (sec)", "min": 0.5, "max": 30},
            "retries": {
                "type": "integer", "default": 1, "label": "Retries",
                "min": 0, "max": 5,
                "description": "Extra attempts when a request goes "
                               "unanswered. SNMP runs over UDP, which drops "
                               "datagrams without saying so.",
            },
            "poll_interval": {
                "type": "integer", "default": 10,
                "label": "Poll Interval (sec)", "min": 0,
                "description": "How often to read outlet states and "
                               "readings. 0 disables polling.",
            },
        },
        "state_variables": {
            "model": {"type": "string", "label": "Model"},
            "device_name": {"type": "string", "label": "Unit Name"},
            "firmware": {"type": "string", "label": "Firmware Version"},
            "manufacturer": {"type": "string", "label": "Manufacturer",
                             "cloud_priority": "low"},
            "device_type": {"type": "string", "label": "Device Type",
                            "cloud_priority": "low"},
            "device_time": {"type": "string", "label": "Unit Time",
                            "cloud_priority": "low"},
            "device_index": {
                "type": "integer", "label": "Device ID", "min": 0, "max": 255,
                "cloud_priority": "low",
            },
            "input_state": {
                "type": "enum", "label": "Input Fault",
                "values": [
                    "no_fault", "no_ground", "reverse_polarity",
                    "over_voltage_line_neutral", "under_voltage_line_neutral",
                    "over_voltage_neutral_ground",
                    "under_voltage_neutral_ground", "overload",
                    "over_temperature", "under_temperature", "over_humidity",
                    "under_humidity", "over_frequency", "under_frequency",
                ],
                "cloud_priority": "high",
            },
            "input_fault": {
                "type": "boolean", "label": "Input Fault Present",
                "cloud_priority": "high",
            },
            "voltage_ln": {
                "type": "number", "label": "Line Voltage (L-N)", "unit": "V",
                "min": 0, "max": 300, "step": 0.1, "cloud_priority": "high",
            },
            "voltage_ng": {
                "type": "number", "label": "Neutral-Ground Voltage",
                "unit": "V", "min": 0, "max": 300, "step": 0.01,
                "cloud_priority": "low",
            },
            "current_a": {
                "type": "number", "label": "Total Current", "unit": "A",
                "min": 0, "max": 20, "step": 0.01, "cloud_priority": "high",
            },
            "power_w": {
                "type": "integer", "label": "Total Power", "unit": "W",
                "min": 0, "cloud_priority": "high",
            },
            "power_factor": {
                "type": "number", "label": "Power Factor",
                "min": -1, "max": 1, "step": 0.001, "cloud_priority": "low",
            },
            "power_factor_type": {
                "type": "enum", "label": "Power Factor Type",
                "values": ["lagging", "unity", "leading"],
                "cloud_priority": "low",
            },
            "crest_factor": {
                "type": "number", "label": "Voltage Crest Factor",
                "min": 0, "max": 2, "step": 0.001, "cloud_priority": "low",
            },
            "frequency_hz": {
                "type": "number", "label": "Line Frequency", "unit": "Hz",
                "min": 0, "max": 70, "step": 0.01, "cloud_priority": "low",
            },
            "energy_kwh": {
                "type": "number", "label": "Energy Used", "unit": "kWh",
                "min": 0, "step": 0.001, "cloud_priority": "low",
            },
            "energy_since": {"type": "string", "label": "Energy Counted Since",
                             "cloud_priority": "low"},
            "internal_temperature_c": {
                "type": "number", "label": "Internal Temperature",
                "unit": "°C", "min": -40, "max": 125, "step": 0.1,
                "cloud_priority": "high",
            },
            "internal_temperature_f": {
                "type": "number", "label": "Internal Temperature (F)",
                "unit": "°F", "min": -40, "max": 257, "step": 0.1,
                "cloud_priority": "low",
            },
            "temperature_unit": {
                "type": "enum", "label": "Temperature Unit",
                "values": ["F", "C"], "cloud_priority": "low",
            },
            "nominal_voltage": {
                "type": "integer", "label": "Nominal Voltage", "unit": "V",
                "min": 0, "max": 255, "cloud_priority": "low",
            },
            "voltage_limit": {
                "type": "integer", "label": "Voltage Limit", "unit": "V",
                "min": 0, "max": 255, "cloud_priority": "low",
            },
            "nominal_frequency": {
                "type": "integer", "label": "Nominal Frequency", "unit": "Hz",
                "min": 0, "max": 60, "cloud_priority": "low",
            },
            "reboot_time": {
                "type": "integer", "label": "Reboot Time", "unit": "s",
                "min": 0, "cloud_priority": "low",
            },
            "outlet_count": {"type": "integer", "label": "Outlets", "min": 0},
            "sys_descr": {"type": "string", "label": "System Description",
                          "cloud_priority": "low"},
            "sys_name": {"type": "string", "label": "System Name",
                         "cloud_priority": "low"},
            "sys_uptime": {"type": "integer", "label": "Uptime", "unit": "s",
                           "min": 0, "cloud_priority": "low"},
        },
        "child_entity_types": {
            "outlet": {
                "label": "Outlet",
                "label_plural": "Outlets",
                "id_format": {"type": "integer", "min": 1, "max": 24},
                "state_variables": {
                    "state": {
                        "type": "boolean", "label": "Powered",
                        "control": True, "cloud_priority": "high",
                    },
                    "status": {
                        "type": "enum", "label": "Status",
                        "values": ["on", "off", "rebooting"],
                        "cloud_priority": "high",
                    },
                    "name": {"type": "string", "label": "Name",
                             "cloud_priority": "low"},
                    "outlet_id": {"type": "string", "label": "Outlet ID",
                                  "cloud_priority": "low"},
                    "initial_state": {
                        "type": "enum", "label": "Initial State",
                        "values": ["always_on", "always_off", "on", "off",
                                   "last", "reboot_only"],
                        "cloud_priority": "low",
                    },
                    "reboot_time": {
                        "type": "integer", "label": "Reboot Time",
                        "unit": "s", "min": 0, "cloud_priority": "low",
                    },
                    "current_a": {
                        "type": "number", "label": "Current", "unit": "A",
                        "min": 0, "max": 16, "step": 0.01,
                        "cloud_priority": "low",
                    },
                    "peak_current_a": {
                        "type": "number", "label": "Peak Current", "unit": "A",
                        "min": 0, "step": 0.01, "cloud_priority": "low",
                    },
                    "power_w": {
                        "type": "integer", "label": "Power", "unit": "W",
                        "min": 0, "cloud_priority": "low",
                    },
                    "power_factor": {
                        "type": "number", "label": "Power Factor",
                        "min": 0, "max": 1, "step": 0.001,
                        "cloud_priority": "low",
                    },
                    "crest_factor": {
                        "type": "number", "label": "Crest Factor",
                        "min": 0, "step": 0.001, "cloud_priority": "low",
                    },
                },
                "summary_fields": ["name", "state", "power_w"],
                "label_field": "name",
            },
            "group": {
                "label": "Outlet Group",
                "label_plural": "Outlet Groups",
                "id_format": {"type": "integer", "min": 1, "max": 255},
                "state_variables": {
                    "state": {
                        "type": "boolean", "label": "Powered",
                        "control": True, "cloud_priority": "high",
                    },
                    "status": {
                        "type": "enum", "label": "Status",
                        "values": ["on", "off", "rebooting"],
                        "cloud_priority": "high",
                    },
                    "name": {"type": "string", "label": "Name",
                             "cloud_priority": "low"},
                    "group_id": {"type": "string", "label": "Group ID",
                                 "cloud_priority": "low"},
                },
                "summary_fields": ["name", "status"],
                "label_field": "name",
            },
            "sequence": {
                "label": "Sequence",
                "label_plural": "Sequences",
                "id_format": {"type": "integer", "min": 0, "max": 255},
                "state_variables": {
                    "status": {
                        "type": "enum", "label": "Status",
                        "values": ["none", "on", "off"],
                        "cloud_priority": "high",
                    },
                    "name": {"type": "string", "label": "Name",
                             "cloud_priority": "low"},
                    "sequence_id": {"type": "string", "label": "Sequence ID",
                                    "cloud_priority": "low"},
                },
                "summary_fields": ["name", "status"],
                "label_field": "name",
            },
            "trigger": {
                "label": "Trigger",
                "label_plural": "Triggers",
                "id_format": {"type": "integer", "min": 0, "max": 255},
                "state_variables": {
                    "triggered": {"type": "boolean", "label": "Triggered",
                                  "cloud_priority": "high"},
                    "enabled": {"type": "boolean", "label": "Enabled",
                                "cloud_priority": "low"},
                    "name": {"type": "string", "label": "Name",
                             "cloud_priority": "low"},
                    "trigger_id": {"type": "string", "label": "Trigger ID",
                                   "cloud_priority": "low"},
                    "trigger_type": {"type": "string", "label": "Type",
                                     "cloud_priority": "low"},
                },
                "summary_fields": ["name", "enabled", "triggered"],
                "label_field": "name",
            },
            "ping_host": {
                "label": "AutoPing Host",
                "label_plural": "AutoPing Hosts",
                "id_format": {"type": "integer", "min": 0, "max": 255},
                "state_variables": {
                    "address": {"type": "string", "label": "Address",
                                "cloud_priority": "low"},
                    "successes": {"type": "integer", "label": "Pings Answered",
                                  "min": 0, "cloud_priority": "low"},
                    "failures": {"type": "integer", "label": "Pings Missed",
                                 "min": 0, "cloud_priority": "high"},
                    "last_success": {"type": "string",
                                     "label": "Last Answered",
                                     "cloud_priority": "low"},
                    "last_failure": {"type": "string", "label": "Last Missed",
                                     "cloud_priority": "low"},
                },
                "summary_fields": ["address", "failures", "last_failure"],
                "label_field": "address",
            },
        },
        "device_settings": {
            "temperature_unit": {
                "type": "enum",
                "label": "Temperature Unit",
                "help": "The unit the Axess ELITE+ shows temperatures in on "
                        "its own web interface. OpenAVC reports both.",
                "values": [
                    {"value": "F", "label": "Fahrenheit"},
                    {"value": "C", "label": "Celsius"},
                ],
                "state_key": "temperature_unit",
                "default": "F",
                "setup": False,
            },
        },
        "quick_actions": [
            "outlet_on", "outlet_off", "outlet_reboot", "run_sequence",
        ],
        "actions": [
            {
                "id": "test_connection",
                "kind": "setup",
                "label": "Test Connection",
                "icon": "search",
                "availability": "always",
                "confirm": (
                    "Reads the Axess ELITE+'s model, firmware and outlet "
                    "list. Nothing on the unit changes and no outlet is "
                    "switched."
                ),
            },
            {"id": "refresh", "kind": "command", "icon": "refresh-cw"},
            {
                "id": "reboot_controller",
                "kind": "command",
                "icon": "server",
                "confirm": (
                    "Restart the Axess ELITE+'s controller. The outlets keep "
                    "their power; only the network interface goes away while "
                    "it restarts. Continue?"
                ),
            },
        ],
        "commands": {
            "refresh": {
                "label": "Refresh Status",
                "params": {},
                "help": "Reads every value, name and list from the unit now.",
            },
            "outlet_on": {
                "label": "Turn Outlet On",
                "params": {
                    "outlet": {"type": "child_id", "child_type": "outlet",
                               "required": True, "label": "Outlet"},
                },
                "sets": {"state": True, "status": "on"},
                "help": "Switches the outlet on and its green outlet light "
                        "comes on.",
            },
            "outlet_off": {
                "label": "Turn Outlet Off",
                "params": {
                    "outlet": {"type": "child_id", "child_type": "outlet",
                               "required": True, "label": "Outlet"},
                },
                "sets": {"state": False, "status": "off"},
                "help": "Switches the outlet off. An outlet set to Always On "
                        "or Reboot Only on the unit stays on.",
            },
            "outlet_reboot": {
                "label": "Reboot Outlet",
                "params": {
                    "outlet": {"type": "child_id", "child_type": "outlet",
                               "required": True, "label": "Outlet"},
                },
                "help": "Switches the outlet off for its Reboot Time, then "
                        "on again. An outlet set to Always On stays on.",
            },
            "group_on": {
                "label": "Turn Group On",
                "params": {
                    "group": {"type": "child_id", "child_type": "group",
                              "required": True, "label": "Outlet Group"},
                },
                "sets": {"state": True, "status": "on"},
                "help": "Switches every outlet in the group on.",
            },
            "group_off": {
                "label": "Turn Group Off",
                "params": {
                    "group": {"type": "child_id", "child_type": "group",
                              "required": True, "label": "Outlet Group"},
                },
                "sets": {"state": False, "status": "off"},
                "help": "Switches every outlet in the group off.",
            },
            "group_reboot": {
                "label": "Reboot Group",
                "params": {
                    "group": {"type": "child_id", "child_type": "group",
                              "required": True, "label": "Outlet Group"},
                },
                "help": "Switches every outlet in the group off for its "
                        "Reboot Time, then on again.",
            },
            "run_sequence": {
                "label": "Run Sequence",
                "params": {
                    "sequence": {"type": "child_id", "child_type": "sequence",
                                 "required": True, "label": "Sequence"},
                },
                "help": "Runs a sequence stored on the unit: its outlets "
                        "switch one after another, with the delays set in the "
                        "unit's web interface.",
            },
            "reset_energy": {
                "label": "Reset Energy Meter",
                "confirm": "Zeroes the unit's energy count. The recorded "
                           "total cannot be recovered.",
                "params": {},
                "help": "Zeroes the Energy Used count and starts it again "
                        "from now.",
            },
            "reboot_controller": {
                "label": "Restart Controller",
                "params": {},
                # The controller is off the network while it restarts, so
                # without this the platform reports a fault for something we
                # asked for. NOT measured (no unit on the bench); generous on
                # purpose, the same as apc_rack_pdu's card restart.
                "restarts_device_for": 90,
                "help": "Restarts the unit's controller. The outlets keep "
                        "their power; the network interface is away while it "
                        "restarts.",
            },
        },
    }

    # Liveness. SNMP rides UDP, where a send always "succeeds", so an awaited
    # read is the only thing that notices the unit has gone.
    HEALTH_INTERVAL_S = 30.0
    HEALTH_TIMEOUT_S = 10.0
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = "The Axess ELITE+ stopped answering SNMP requests."

    def __init__(self, device_id: str, config: dict[str, Any], state, events):
        # child type -> {local id: OID row suffix}. The walk is the authority
        # on which rows exist; the local id is the row's index.
        self._rows: dict[str, dict[int, str]] = {}
        self._polls = 0
        super().__init__(device_id, config, state, events)

    # ── Connect ──────────────────────────────────────────────────────────

    async def _initial_sync(self) -> None:
        """Read the rosters and a first full reading before the first poll."""
        try:
            await self._read_system_group()
            await self._reconcile_children()
            await self._read_scalars()
            await self._read_children(slow=True)
        except (ConnectionError, SnmpError) as e:
            # Left connected: the first poll reads again, and a unit still
            # silent then goes offline as no_response.
            log.warning("[%s] Initial sync failed: %s", self.device_id, e)

    async def _read_system_group(self) -> None:
        answered = await self._get([SYS_DESCR, SYS_NAME, SYS_UPTIME])
        updates: dict[str, Any] = {}
        for name, oid in (("sys_descr", SYS_DESCR), ("sys_name", SYS_NAME)):
            varbind = answered.get(oid)
            if varbind is not None and not varbind.is_exception:
                updates[name] = str(varbind.value)
        uptime = answered.get(SYS_UPTIME)
        if uptime is not None and not uptime.is_exception:
            try:
                # TimeTicks are hundredths of a second.
                updates["sys_uptime"] = int(uptime.value) // 100
            except (TypeError, ValueError):
                pass
        if updates:
            self.set_states(updates)

    # ── Children ─────────────────────────────────────────────────────────

    async def _reconcile_children(self) -> None:
        """Walk each table's roster column and register a child per row.

        A row that is gone (a group or sequence deleted in the web interface)
        is deregistered rather than left showing its last reading.
        """
        for table in TABLES:
            rows = await self._walk_rows(f"{table.entry}.{table.roster}")
            for local_id in rows:
                self.register_child(table.child_type, local_id,
                                    initial_state={"online": True})
            known = self._rows.get(table.child_type, {})
            for stale in set(known) - set(rows):
                self.deregister_child(table.child_type, stale)
            self._rows[table.child_type] = rows
        self.set_state("outlet_count", len(self._rows.get("outlet", {})))

    async def _walk_rows(self, column_oid: str) -> dict[int, str]:
        """Row suffixes under a column, keyed by local child id.

        Every table in this MIB has one integer index, so a suffix that is not
        a single integer is skipped rather than coerced.
        """
        # Silence is the transport's typed no_response fault, which is left to
        # reach the platform's offline path.
        try:
            rows = await self.transport.walk(column_oid, limit=WALK_LIMIT)
        except SnmpError as e:
            log.warning("[%s] SNMP walk of %s refused: %s",
                        self.device_id, column_oid, e)
            return dict(self._rows_for_column(column_oid))
        prefix = column_oid + "."
        found: dict[int, str] = {}
        for varbind in rows:
            suffix = varbind.oid[len(prefix):]
            if suffix.isdigit():
                found[int(suffix)] = suffix
        return found

    def _rows_for_column(self, column_oid: str) -> dict[int, str]:
        """The rows already known for the table a column belongs to, so a
        refused walk keeps the roster instead of emptying it."""
        for table in TABLES:
            if column_oid.startswith(table.entry + "."):
                return self._rows.get(table.child_type, {})
        return {}

    async def refresh_children(self) -> dict[str, Any]:
        """The IDE's Refresh from Device button."""
        await self._reconcile_children()
        await self._read_children(slow=True)
        return {
            child_type: len(rows)
            for child_type, rows in self._rows.items() if rows
        }

    # ── Reads ────────────────────────────────────────────────────────────

    async def poll(self) -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        self._polls += 1
        slow = (
            self._polls % SLOW_EVERY_POLLS == 0
            or not self._rows.get("outlet")
        )
        if slow:
            # Also how a unit that did not answer at connect gets its rosters.
            await self._read_system_group()
            await self._reconcile_children()
        await self._read_scalars()
        await self._read_children(slow=slow)

    async def _read_scalars(self) -> None:
        by_oid = {f"{DEVICE}.{column.number}.0": column for column in SCALARS}
        updates: dict[str, Any] = {}
        for chunk in _chunks(list(by_oid), VARBINDS_PER_REQUEST):
            for oid, varbind in (await self._get(chunk)).items():
                column = by_oid.get(oid)
                if column is None or varbind.is_exception:
                    continue
                value = _decode(column, varbind.value)
                if value is not None:
                    updates[column.prop] = value
        reading = updates.pop("_temperature", None)
        unit = updates.get("temperature_unit")
        if reading is not None and unit in ("F", "C"):
            # "Internal Temperature: Degrees F or C, based on setting
            # currently in use". Both are published so an alert does not
            # change meaning when someone changes the unit's display setting.
            if unit == "C":
                celsius, fahrenheit = reading, reading * 9 / 5 + 32
            else:
                celsius, fahrenheit = (reading - 32) * 5 / 9, reading
            updates["internal_temperature_c"] = round(celsius, 1)
            updates["internal_temperature_f"] = round(fahrenheit, 1)
        if "input_state" in updates:
            updates["input_fault"] = updates["input_state"] != "no_fault"
        if updates:
            self.set_states(updates)

    async def _read_children(self, *, slow: bool) -> None:
        wanted: list[str] = []
        owner: dict[str, tuple[str, int, Column]] = {}
        for table in TABLES:
            columns = table.live + (table.slow if slow else ())
            for local_id, suffix in self._rows.get(table.child_type, {}).items():
                for column in columns:
                    oid = f"{table.entry}.{column.number}.{suffix}"
                    wanted.append(oid)
                    owner[oid] = (table.child_type, local_id, column)

        per_child: dict[tuple[str, int], dict[str, Any]] = {}
        for chunk in _chunks(wanted, VARBINDS_PER_REQUEST):
            for oid, varbind in (await self._get(chunk)).items():
                entry = owner.get(oid)
                if entry is None or varbind.is_exception:
                    continue
                child_type, local_id, column = entry
                value = _decode(column, varbind.value)
                if value is None:
                    continue
                props = per_child.setdefault((child_type, local_id), {})
                props[column.prop] = value
                if column.prop == "status" and child_type in ("outlet", "group"):
                    # Powered only while on: a rebooting outlet is off for
                    # its Reboot Time.
                    props["state"] = value == "on"
        if per_child:
            self.set_children_state_batch(
                [(ct, cid, props) for (ct, cid), props in per_child.items()]
            )

    async def _get(self, oids: list[str]) -> dict[str, Any]:
        """One GET.

        Silence raises the transport's typed ``no_response`` fault (a
        ``ConnectionError``), which reaches the platform's offline path. An
        ``SnmpError`` is the agent refusing the request (a community that may
        not read one of these OIDs): a log line and a stale value, not an
        offline unit.
        """
        if not oids:
            return {}
        try:
            return await self.transport.get(oids)
        except SnmpError as e:
            log.warning("[%s] SNMP read refused: %s", self.device_id, e)
            return {}

    # ── Writes ───────────────────────────────────────────────────────────

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None,
    ) -> Any:
        params = params or {}
        if command == "refresh":
            await self._read_system_group()
            await self._reconcile_children()
            await self._read_scalars()
            await self._read_children(slow=True)
            return True

        switch = {
            "outlet_on": ("outlet", "on"), "outlet_off": ("outlet", "off"),
            "outlet_reboot": ("outlet", "reboot"),
            "group_on": ("group", "on"), "group_off": ("group", "off"),
            "group_reboot": ("group", "reboot"),
        }.get(command)
        if switch is not None:
            child_type, action = switch
            local_id, suffix = self._row(child_type, params)
            oid = f"{TABLE_BY_TYPE[child_type].entry}.4.{suffix}"
            await self._set(oid, SWITCH_VALUE[action])
            # The SET echoes what we sent; the status column is what the unit
            # did, and an outlet held by its initial state answers the SET
            # and stays as it was.
            status = await self._read_status(child_type, local_id, suffix)
            if child_type == "outlet":
                self._check_held(local_id, action, status)
            return True

        if command == "run_sequence":
            local_id, suffix = self._row("sequence", params)
            await self._set(f"{SEQUENCES}.4.{suffix}", SEQUENCE_RUN)
            await self._read_status("sequence", local_id, suffix)
            return True

        if command == "reset_energy":
            await self._set(ENERGY_USAGE_RESET, YES)
            await self._read_scalars()
            return True

        if command == "reboot_controller":
            await self._set(REBOOT_DEVICE, YES)
            return True

        raise ValueError(f"Unknown command: {command}")

    def _row(self, child_type: str, params: dict[str, Any]) -> tuple[int, str]:
        raw = params.get(child_type, params.get("child_id"))
        if raw is None:
            raise ValueError(f"This command needs a {child_type}")
        try:
            local_id = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"Unknown {child_type}: {raw}") from None
        rows = self._rows.get(child_type, {})
        if local_id not in rows:
            label = child_type.replace("_", " ")
            raise ValueError(
                f"The Axess ELITE+ has no {label} {local_id}"
                + ("" if rows else " (its list has not been read yet)")
            )
        return local_id, rows[local_id]

    async def _read_status(
        self, child_type: str, local_id: int, suffix: str,
    ) -> str | None:
        table = TABLE_BY_TYPE[child_type]
        column = table.live[0]
        oid = f"{table.entry}.{column.number}.{suffix}"
        varbind = (await self._get([oid])).get(oid)
        if varbind is None or varbind.is_exception:
            return None
        value = _decode(column, varbind.value)
        if value is None:
            return None
        props: dict[str, Any] = {"status": value}
        if child_type in ("outlet", "group"):
            props["state"] = value == "on"
        self.set_child_state_batch(child_type, local_id, props)
        return value

    def _check_held(self, local_id: int, action: str, status: str | None) -> None:
        """Say so when the unit kept an outlet as it was because of its
        Initial State, rather than reporting a switch that did not happen.

        Only for the states the manual says ignore the command, and only once
        the status read back shows it did not change, so an outlet whose
        Initial State was edited since the last read is not refused.
        """
        outlet = self.get_child_state("outlet", local_id)
        initial = outlet.get("initial_state")
        if status is None or initial not in IGNORES[action]:
            return
        if status == {"on": "on", "off": "off", "reboot": "rebooting"}[action]:
            return
        name = outlet.get("name") or f"Outlet {local_id}"
        what = {"on": "power-on", "off": "power-off", "reboot": "reboot"}[action]
        raise CommandRefusedError(
            f"{name} is set to {INITIAL_STATE_WORDS[initial]} on the Axess "
            f"ELITE+, which ignores {what} commands."
        )

    async def _set(self, oid: str, value: int) -> None:
        try:
            await self.transport.set([(oid, "integer", value)])
        except SnmpError as e:
            raise CommandRefusedError(
                f"The Axess ELITE+ refused the change: {e}"
            ) from e
        except ConnectionError as e:
            # The transport's no-reply fault. Reads answering while a write
            # goes unanswered is a write community the unit does not have.
            raise CommandRefusedError(
                "The Axess ELITE+ did not answer the change. It ignores a "
                "community it does not recognise without replying, so a "
                "Write Community that is not set up on the unit looks like "
                "this."
            ) from e

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if key != "temperature_unit":
            raise ValueError(f"{key} is not a setting of this driver")
        number = {"F": 1, "C": 2}.get(str(value).upper())
        if number is None:
            raise ValueError("Temperature Unit is F or C")
        await self._set(TEMPERATURE_UNIT, number)
        await self._read_scalars()
        return self.get_state("temperature_unit")

    # ── Test Connection ──────────────────────────────────────────────────

    async def run_setup_action(
        self, action_id: str, params: dict[str, Any], progress: Any,
    ) -> dict[str, Any]:
        """Prove the address, the read community and that this is an ELITE+.

        A wrong community is answered with silence, which looks exactly like
        an unreachable unit, so the failure sentence names both.
        """
        if action_id != "test_connection":
            raise ValueError(f"Unknown setup action: {action_id}")
        model_oid = f"{DEVICE}.5.0"
        firmware_oid = f"{DEVICE}.2.0"
        try:
            answered = await self.transport.get([model_oid, firmware_oid])
        except ConnectionError:
            return {
                "success": False,
                "message": (
                    "No answer. Check the IP address, that SNMP is enabled "
                    "on the unit's SNMP page, and that the Read Community "
                    "is one listed there. The unit ignores a community it "
                    "does not recognise, which looks the same as an "
                    "unreachable unit."
                ),
            }
        except SnmpError as e:
            return {"success": False, "message": str(e)}
        model = answered.get(model_oid)
        if model is None or model.is_exception:
            return {
                "success": False,
                "message": (
                    "Reached an SNMP device, but it does not answer the "
                    "Axess ELITE+ model number. This driver needs an Axess "
                    "ELITE+."
                ),
            }
        firmware = answered.get(firmware_oid)
        await self._reconcile_children()
        outlets = len(self._rows.get("outlet", {}))
        message = f"Connected to {model.value}"
        if firmware is not None and not firmware.is_exception:
            message += f", firmware {firmware.value}"
        message += f". Found {outlets} outlet{'s' if outlets != 1 else ''}"
        extras = [
            f"{len(self._rows[t])} {t.replace('_', ' ')}"
            f"{'s' if len(self._rows[t]) != 1 else ''}"
            for t in ("group", "sequence", "trigger")
            if self._rows.get(t)
        ]
        if extras:
            message += ", " + ", ".join(extras)
        message += (
            ". Switching an outlet also needs the Write Community to have "
            "Read/Write access; this test writes nothing."
        )
        return {"success": True, "message": message}

    # ── Liveness ─────────────────────────────────────────────────────────

    async def _liveness_probe(self) -> None:
        """Read the unit's model number, a fixed object every ELITE+ has.

        Any answer counts: an agent that refuses it still answered.
        """
        try:
            await self.transport.get(f"{DEVICE}.5.0")
        except SnmpError:
            return


def _decode(column: Column, raw: Any) -> Any:
    """Turn one varbind value into what the state variable is declared as."""
    if column.text:
        if isinstance(raw, bytes):
            return raw.decode("utf-8", "replace").strip()
        return str(raw).strip()
    try:
        number = int(raw)
    except (TypeError, ValueError):
        return None
    if column.enum is not None:
        return column.enum.get(number)
    if column.scale == 1.0:
        return number
    return round(number * column.scale, 4)


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


DRIVER_CLASS = SurgeXAxessElitePlusDriver
