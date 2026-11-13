"""Sennheiser Spectera over HTTPS/SSCv2. See the manufacturer's Spectera OpenAPI 18.1 and 18.3.
A paired device's UID survives a name, slot or roster change. Output route mutes
join the output's link and policy to the paired source's command-button state.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any

from openavc.core.connection_fault import (
    CHILD_NOT_FITTED,
    CHILD_NOT_RESPONDING,
    CHILD_SERVICE_FAULT,
)
from openavc.drivers.base import BaseDriver, CommandRefusedError, ConnectionFaultError
from openavc.drivers.command_outcome import CommandOutcomeError
from openavc.utils.logger import get_logger

log = get_logger(__name__)

MOBILES = "/api/mts/paired/all"
OUTPUTS = "/api/audio/outputs"
RF = "/api/rf/channels"
ANTENNAS = "/api/rf/antennas"
SESSION = "/api/ssc/state/subscriptions"
USAGE = "/api/ssc/usagemark"
PENDING = "/api/audio/pendingactions"
IDENTIFY = "/api/device/identification"
COLLECTIONS = {
    MOBILES: ("mobile", "mtUid"),
    OUTPUTS: ("output", "outputId"),
    RF: ("rf", "rfChannelId"),
    ANTENNAS: ("antenna", "antennaPortId"),
}
PARENT = {
    "/api/device/state": {"station_state": "state"},
    IDENTIFY: {"identifying": "visual"},
    "/api/device/site": {"device_name": "deviceName"},
    "/api/firmware/update/state": {"firmware_version": "deviceVersion"},
    "/api/health/psu": {"psu_1": "psu1", "psu_2": "psu2"},
    "/api/health/tempstateoverall": {"temperature_state": "value"},
    "/api/audio/interface/aoip/status": {"aoip_muted": "mute"},
    "/api/audio/interface/madi1/status": {
        "madi1_input_muted": "inputStatus.mute",
        "madi1_output_muted": "outputStatus.mute",
    },
    "/api/audio/interface/madi2/status": {
        "madi2_input_muted": "inputStatus.mute",
        "madi2_output_muted": "outputStatus.mute",
    },
}
RESOURCES = (*COLLECTIONS, *PARENT)
DESTINATIONS = {"aoip": "aoIp", "madi1": "madi1", "madi2": "madi2"}


def field(row, path):
    value = row
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def measurement(value, minimum, maximum):
    if type(value) in (float, int) and minimum <= value <= maximum:
        return value
    return None


def mobile_state(row):
    connected = row.get("state") == "Connected"
    live = row if connected else {}
    iem = live if row.get("type") == "SEK" else {}
    volume = measurement(iem.get("headphoneVolume"), -100, 27.5)
    status = {
        "name": row.get("name") or f"{row['type']} {row['mtUid']}",
        "model": row["type"],
        "serial": row.get("serial"),
        "firmware_version": row.get("version"),
        "state": row.get("state"),
        "rf_channel": row.get("rfChannelId"),
        "mic_link_id": measurement(row.get("micAudiolinkId"), 0, 255),
        "iem_link_id": measurement(row.get("iemAudiolinkId"), 0, 255),
        "battery_percent": measurement(live.get("batteryFillLevel"), 0, 100),
        "battery_minutes": measurement(live.get("batteryRuntime"), 0, 65535),
        "battery_low": live.get("batteryLow"),
        "mic_active": live.get("micAudiolinkActive"),
        "iem_active": iem.get("iemAudiolinkActive"),
        "mic_quality": measurement(live.get("micLqi"), 1, 4),
        "iem_quality": measurement(iem.get("iemLqi"), 1, 4),
        "rssi": measurement(live.get("rssi"), -138, 30),
        "dominant_antenna": live.get("dominantAntenna")
        if live.get("dominantAntenna") in ("a", "b", "c", "d")
        else None,
        "interference": field(live, "interference.severity"),
        "identifying": live.get("identify"),
        "reverse_identify": live.get("reverseIdentify"),
        "gain_db": live.get("micPreampGain"),
        "headphone_db": volume,
        "headphone_muted": volume == -100 if volume is not None else None,
        "command_behavior": row.get("commandBehavior"),
        "command_state": live.get("commandState"),
    }
    if status["command_state"] not in ("Pressed", "Released", "Latched", "Unlatched"):
        status["command_state"] = None
    return status


def route_mute(source, disabled_mode, enabled_mode):
    if source.get("state") != "Connected":
        return None
    behavior = source.get("commandBehavior")
    if behavior == "Disabled":
        mode = disabled_mode
    elif behavior in ("Momentary", "Latching"):
        mode = enabled_mode
    else:
        return None
    if mode in ("On", "Off"):
        return mode == "Off"
    states = {
        "Momentary": {"Pressed": True, "Released": False},
        "Latching": {"Latched": True, "Unlatched": False},
    }
    active = states.get(behavior, {}).get(source.get("commandState"))
    if active is None or mode not in ("Mute", "Talk"):
        return None
    return active if mode == "Mute" else not active


def output_state(row, sources):
    link = measurement(row.get("micAudiolinkId"), 0, 255)
    matches = sources.get(link, [])
    source = matches[0] if len(matches) == 1 else {}
    status = {
        "assigned": None if row.get("micAudiolinkId") is None else link is not None,
        "mic_link_id": link,
        "source_uid": source.get("mtUid"),
        "source_name": source.get("name"),
    }
    for dest, wire in DESTINATIONS.items():
        enabled = row.get(wire + "EnableIfCommandIsEnabled")
        disabled = row.get(wire + "EnableIfCommandIsDisabled")
        status[dest + "_enabled_mode"] = enabled
        status[dest + "_disabled_mode"] = disabled
        status[dest + "_muted"] = route_mute(source, disabled, enabled)
    return status


def wireless_state(row):
    state = row.get("rfState")
    return {
        "requested_mute": state == "RfMuted"
        if state in ("RfActive", "RfMuted")
        else None,
        "restricted": row.get("rfRestrictionViolation"),
        "frequency_khz": row.get("frequency"),
        "power_mw": row.get("txPower"),
    }


def antenna_state(row):
    state = row.get("state")
    live = row if state and state != "Unconnected" else {}
    return {
        "state": state,
        "error": live.get("errorStateDetails"),
        "band": live.get("type"),
        "serial": live.get("serial"),
        "firmware_version": live.get("version"),
        "temperature": live.get("temperature"),
        "high_temperature": live.get("warningHighTemperature"),
        "packet_error": live.get("warningPacketError"),
        "identifying": live.get("identify"),
    }


def local_id(path, wire_id):
    if path in (OUTPUTS, RF):
        return wire_id + 1
    return wire_id


def indexed_rows(path, body):
    """Only a complete, well-formed collection can remove an existing child."""
    if not isinstance(body, list):
        raise ValueError("Spectera returned an invalid collection")
    result = {}
    for row in body:
        wire_id = field(row, COLLECTIONS[path][1])
        if path == ANTENNAS:
            valid = wire_id in ("a", "b", "c", "d")
        else:
            limit = {MOBILES: 4294967295, RF: 1, OUTPUTS: 31}[path]
            valid = type(wire_id) is int and 0 <= wire_id <= limit
        if (
            not valid
            or wire_id in result
            or (path == MOBILES and row.get("type") not in ("SEK", "SKM"))
        ):
            raise ValueError("Spectera returned an invalid or duplicate child identity")
        result[wire_id] = row
    return result


def command_value(value, schema):
    kind = schema["type"]
    if kind == "boolean":
        if str(value).lower() in ("true", "1", "on"):
            return True
        if str(value).lower() in ("false", "0", "off"):
            return False
    elif kind == "enum":
        if value in schema["values"]:
            return value
    elif kind in ("integer", "number") and not isinstance(value, bool):
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = math.nan
        if math.isfinite(number) and schema["min"] <= number <= schema["max"]:
            if kind == "integer" and number.is_integer():
                return int(number)
            if kind == "number" and (number * 2).is_integer():
                return number
    raise ValueError(f"Invalid {schema['label']} value")


class SennheiserSpecteraDriver(BaseDriver):
    DRIVER_INFO = {
        "id": "sennheiser_spectera",
        "name": "Sennheiser Spectera",
        "manufacturer": "Sennheiser",
        "category": "audio",
        "version": "1.0.0",
        "min_platform_version": "0.37.2",
        "author": "OpenAVC",
        "transport": "http",
        "description": "Discover paired SEK bodypacks and SKM handhelds on a Spectera Base Station. Follow batteries, RF "
        "health, microphone and IEM links, command-button output mutes and DAD antennas live.",
        "source_url": "https://docs.cloud.sennheiser.com/en-us/api-docs/spectera/open-api-specification-18-3.html",
        "protocols": ["sscv2"],
        "ports": [443],
        "tags": ["spectera", "wireless-microphone", "iem", "sscv2", "wmas"],
        "verified": False,
        "simulated": True,
        "compatible_models": [
            {
                "manufacturer": "Sennheiser",
                "models": ["Spectera Base Station", "Spectera Base Station ZMAN"],
                "confidence": "untested",
                "notes": "Spectera API 18.1 or 18.3, base firmware 1.4.0 or 1.5.x. Paired SEK and SKM "
                "devices and attached DAD antennas appear under the base station.",
            }
        ],
        "help": {
            "overview": "The paired device list follows each bodypack or handheld by its permanent radio ID. Each "
            "audio output shows the mute determined by its source's command button and its own AoIP and "
            "MADI routing settings. A missing or ambiguous source leaves the result unknown. "
            "Interface-wide mutes are reported separately.",
            "setup": "Commission the station and pair the radios in LinkDesk or the Spectera WebUI. Enter the "
            "station address and the device password configured there. OpenAVC uses the controlSennheiser "
            "account. Check the password in LinkDesk after a refusal because repeated failures can "
            "temporarily block access.",
            "connection": "HTTPS on port 443. Enable certificate verification when the station's certificate is "
            "trusted by OpenAVC. Subscriptions follow external changes. Every reconnect reads the "
            "complete roster and current settings. A periodic refresh also works while event delivery "
            "is unavailable.",
        },
        "discovery": {
            "tcp_probe": {
                "port": 443,
                "tls": True,
                "send_ascii": "GET /api/device/identity HTTP/1.1\r\n"
                "Host: spectera\r\n"
                "Connection: close\r\n"
                "\r\n",
                "expect_regex": '"product"\\s*:\\s*"Spectera-Base-Station"',
                "extract_manufacturer": "Sennheiser",
                "extract": {
                    "model": {"regex": '"articleName"\\s*:\\s*"([^"]+)"', "group": 1}
                },
            },
            "manufacturer_alias": ["sennheiser"],
        },
        "default_config": {
            "host": "",
            "port": 443,
            "ssl": True,
            "verify_ssl": True,
            "username": "controlSennheiser",
            "auth_type": "basic",
            "password": "",
            "timeout": 5,
            "verify_timeout": 0,
            "poll_interval": 30,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "label": "Base Station Address",
                "required": True,
            },
            "password": {
                "type": "string",
                "label": "Spectera Device Password",
                "secret": True,
                "required": True,
            },
            "port": {
                "type": "integer",
                "default": 443,
                "min": 1,
                "max": 65535,
                "advanced": True,
                "label": "HTTPS Port",
            },
            "verify_ssl": {
                "type": "boolean",
                "default": True,
                "advanced": True,
                "label": "Verify TLS Certificate",
                "help": "Turn this off only for a station whose device certificate you trust "
                "but OpenAVC cannot validate.",
            },
            "poll_interval": {
                "type": "integer",
                "default": 30,
                "min": 5,
                "max": 300,
                "advanced": True,
                "label": "Full Refresh Interval (seconds)",
                "help": "Safety refresh while events are quiet or unavailable.",
            },
        },
        "push": {
            "type": "sse",
            "path": "/api/ssc/state/subscriptions",
            "idle_timeout": 120,
            "session": {
                "header": "Content-Location",
                "event": "open",
                "key": "sessionUUID",
                "pattern": "([0-9a-fA-F]{8}-[0-9a-fA-F-]{27})$",
                "close_event": "close",
            },
        },
        "state_variables": {
            "model": {"type": "string", "label": "Base Station Model"},
            "serial": {"type": "string", "label": "Serial Number"},
            "api_version": {"type": "string", "label": "Spectera API Version"},
            "firmware_version": {"type": "string", "label": "Firmware Version"},
            "device_name": {"type": "string", "label": "Station Name"},
            "station_state": {
                "type": "enum",
                "label": "Operating State",
                "values": [
                    "Initializing",
                    "Normal",
                    "FactoryReset",
                    "Standby",
                    "Cooldown",
                    "Warmup",
                    "CriticalFailure",
                ],
                "cloud_priority": "high",
            },
            "identifying": {
                "type": "boolean",
                "label": "Station Identify",
                "control": True,
            },
            "paired_count": {"type": "integer", "label": "Paired Devices"},
            "connected_count": {"type": "integer", "label": "Connected Devices"},
            "events_active": {"type": "boolean", "label": "Live Subscription Active"},
            "psu_1": {
                "type": "enum",
                "label": "Power Supply 1",
                "values": ["connected", "unconnected", "disconnected"],
            },
            "psu_2": {
                "type": "enum",
                "label": "Power Supply 2",
                "values": ["connected", "unconnected", "disconnected"],
            },
            "temperature_state": {
                "type": "enum",
                "label": "Temperature",
                "values": ["normal", "low", "high", "critical"],
            },
            "aoip_muted": {
                "type": "boolean",
                "label": "AoIP Interface Muted",
                "cloud_priority": "high",
            },
            "madi1_input_muted": {
                "type": "boolean",
                "label": "MADI 1 Input Interface Muted",
                "cloud_priority": "high",
            },
            "madi1_output_muted": {
                "type": "boolean",
                "label": "MADI 1 Output Interface Muted",
                "cloud_priority": "high",
            },
            "madi2_input_muted": {
                "type": "boolean",
                "label": "MADI 2 Input Interface Muted",
                "cloud_priority": "high",
            },
            "madi2_output_muted": {
                "type": "boolean",
                "label": "MADI 2 Output Interface Muted",
                "cloud_priority": "high",
            },
            "microphone_choices": {
                "type": "string",
                "label": "Available Microphone Sources",
            },
        },
        "child_entity_types": {
            "mobile": {
                "label": "Paired Device",
                "label_plural": "Paired Devices",
                "id_format": {"type": "integer", "min": 0, "max": 4294967295},
                "state_variables": {
                    "name": {"type": "string", "label": "Device Name"},
                    "model": {"type": "string", "label": "Mobile Device Type"},
                    "serial": {"type": "string", "label": "Serial Number"},
                    "firmware_version": {"type": "string", "label": "Firmware Version"},
                    "state": {
                        "type": "enum",
                        "label": "Wireless Connection",
                        "values": [
                            "Connected",
                            "Disconnected",
                            "Syncing",
                            "Sleeping",
                            "Updating",
                        ],
                        "cloud_priority": "high",
                    },
                    "battery_percent": {
                        "type": "integer",
                        "label": "Battery Remaining",
                        "min": 0,
                        "max": 100,
                        "unit": "%",
                    },
                    "battery_minutes": {
                        "type": "integer",
                        "label": "Battery Runtime",
                        "min": 0,
                        "max": 65535,
                        "unit": "min",
                    },
                    "battery_low": {
                        "type": "boolean",
                        "label": "Low Battery",
                        "cloud_priority": "high",
                    },
                    "rf_channel": {
                        "type": "integer",
                        "label": "RF Channel (wire ID)",
                        "min": 0,
                        "max": 1,
                    },
                    "mic_link_id": {
                        "type": "integer",
                        "label": "Microphone Link ID",
                        "min": 0,
                        "max": 255,
                    },
                    "iem_link_id": {
                        "type": "integer",
                        "label": "IEM Link ID",
                        "min": 0,
                        "max": 255,
                    },
                    "mic_active": {
                        "type": "boolean",
                        "label": "Microphone Link Active",
                        "cloud_priority": "high",
                    },
                    "iem_active": {
                        "type": "boolean",
                        "label": "IEM Link Active",
                        "cloud_priority": "high",
                    },
                    "rssi": {
                        "type": "number",
                        "label": "Signal at Dominant Antenna",
                        "unit": "dBm",
                        "cloud_priority": "low",
                    },
                    "dominant_antenna": {"type": "string", "label": "Dominant Antenna"},
                    "interference": {
                        "type": "enum",
                        "label": "Interference",
                        "values": ["None", "Low", "Mid", "High"],
                    },
                    "identifying": {
                        "type": "boolean",
                        "label": "Identify Mobile Device",
                        "control": True,
                    },
                    "reverse_identify": {
                        "type": "boolean",
                        "label": "Identify Requested by Performer",
                    },
                    "gain_db": {
                        "type": "integer",
                        "label": "Mic Preamp Gain",
                        "min": -10,
                        "max": 42,
                        "unit": "dB",
                        "control": True,
                    },
                    "headphone_db": {
                        "type": "number",
                        "label": "SEK Headphone Volume",
                        "min": -100,
                        "max": 27.5,
                        "step": 0.5,
                        "unit": "dB",
                        "control": True,
                    },
                    "headphone_muted": {
                        "type": "boolean",
                        "label": "SEK Headphone Volume at Mute",
                    },
                    "command_behavior": {
                        "type": "enum",
                        "label": "Command Button Mode",
                        "values": ["Disabled", "Momentary", "Latching"],
                        "control": True,
                    },
                    "command_state": {
                        "type": "enum",
                        "label": "Command Button State",
                        "values": ["Released", "Pressed", "Unlatched", "Latched"],
                        "cloud_priority": "high",
                    },
                    "mic_quality": {
                        "type": "integer",
                        "label": "MIC Link Quality",
                        "min": 1,
                        "max": 4,
                        "help": "1 audio dropout, 2 error "
                        "concealed, 3 marginal, 4 stable. "
                        "Blank when the station reports no "
                        "measurement.",
                        "cloud_priority": "low",
                    },
                    "iem_quality": {
                        "type": "integer",
                        "label": "IEM Link Quality",
                        "min": 1,
                        "max": 4,
                        "help": "1 audio dropout, 2 error "
                        "concealed, 3 marginal, 4 stable. "
                        "Blank when the station reports no "
                        "measurement.",
                        "cloud_priority": "low",
                    },
                },
                "summary_fields": [
                    "state",
                    "battery_percent",
                    "mic_quality",
                    "iem_quality",
                ],
                "label_field": "name",
            },
            "output": {
                "label": "Audio Output",
                "label_plural": "Audio Outputs",
                "id_format": {"type": "integer", "min": 1, "max": 32},
                "state_variables": {
                    "mic_link_id": {
                        "type": "integer",
                        "label": "Microphone Link ID",
                        "min": 0,
                        "max": 255,
                    },
                    "source_uid": {"type": "integer", "label": "Paired Source ID"},
                    "source_name": {"type": "string", "label": "Paired Source Name"},
                    "aoip_muted": {
                        "type": "boolean",
                        "label": "AoIP Route Muted",
                        "help": "Mute from this output's command "
                        "routing policy and paired source. "
                        "Interface status and RF dropouts "
                        "are separate.",
                        "cloud_priority": "high",
                    },
                    "aoip_enabled_mode": {
                        "type": "enum",
                        "label": "AoIP With Command Enabled",
                        "values": ["On", "Off", "Talk", "Mute"],
                        "control": True,
                    },
                    "aoip_disabled_mode": {
                        "type": "enum",
                        "label": "AoIP With Command Disabled",
                        "values": ["On", "Off"],
                        "control": True,
                    },
                    "madi1_muted": {
                        "type": "boolean",
                        "label": "MADI 1 Route Muted",
                        "help": "Mute from this output's command "
                        "routing policy and paired source. "
                        "Interface status and RF dropouts "
                        "are separate.",
                        "cloud_priority": "high",
                    },
                    "madi1_enabled_mode": {
                        "type": "enum",
                        "label": "MADI 1 With Command Enabled",
                        "values": ["On", "Off", "Talk", "Mute"],
                        "control": True,
                    },
                    "madi1_disabled_mode": {
                        "type": "enum",
                        "label": "MADI 1 With Command Disabled",
                        "values": ["On", "Off"],
                        "control": True,
                    },
                    "madi2_muted": {
                        "type": "boolean",
                        "label": "MADI 2 Route Muted",
                        "help": "Mute from this output's command "
                        "routing policy and paired source. "
                        "Interface status and RF dropouts "
                        "are separate.",
                        "cloud_priority": "high",
                    },
                    "madi2_enabled_mode": {
                        "type": "enum",
                        "label": "MADI 2 With Command Enabled",
                        "values": ["On", "Off", "Talk", "Mute"],
                        "control": True,
                    },
                    "madi2_disabled_mode": {
                        "type": "enum",
                        "label": "MADI 2 With Command Disabled",
                        "values": ["On", "Off"],
                        "control": True,
                    },
                    "assigned": {
                        "type": "boolean",
                        "label": "Microphone Link Assigned",
                    },
                },
                "summary_fields": [
                    "source_name",
                    "aoip_muted",
                    "madi1_muted",
                    "madi2_muted",
                ],
                "instances": {"count": 32, "label": "Output {id}"},
            },
            "rf": {
                "label": "RF Channel",
                "label_plural": "RF Channels",
                "id_format": {"type": "integer", "min": 1, "max": 2},
                "state_variables": {
                    "requested_mute": {
                        "type": "boolean",
                        "label": "RF Mute Requested",
                        "control": True,
                        "help": "Muting the carrier disconnects "
                        "every mobile device on it. An "
                        "active request alone does not "
                        "prove transmission.",
                    },
                    "restricted": {
                        "type": "boolean",
                        "label": "Transmission Restricted",
                    },
                    "frequency_khz": {
                        "type": "integer",
                        "label": "Carrier Frequency",
                        "min": 10000,
                        "max": 10000000,
                        "unit": "kHz",
                    },
                    "power_mw": {
                        "type": "integer",
                        "label": "Transmit Power",
                        "unit": "mW",
                    },
                },
                "summary_fields": ["requested_mute", "restricted", "frequency_khz"],
                "instances": {"count": 2, "label": "RF {id}"},
            },
            "antenna": {
                "label": "Antenna Port",
                "label_plural": "DAD Antenna Ports",
                "id_format": {"type": "string", "max_length": 1},
                "state_variables": {
                    "state": {
                        "type": "enum",
                        "label": "Port State",
                        "values": [
                            "Unconnected",
                            "Connected",
                            "Updating",
                            "Initialized",
                            "Error",
                        ],
                    },
                    "error": {"type": "string", "label": "Error Detail"},
                    "band": {"type": "string", "label": "Antenna Band"},
                    "serial": {"type": "string", "label": "Serial Number"},
                    "firmware_version": {"type": "string", "label": "Firmware Version"},
                    "temperature": {
                        "type": "integer",
                        "label": "Temperature",
                        "unit": "°C",
                    },
                    "high_temperature": {
                        "type": "boolean",
                        "label": "High Temperature",
                        "cloud_priority": "high",
                    },
                    "packet_error": {
                        "type": "boolean",
                        "label": "Cable Packet Errors",
                        "cloud_priority": "high",
                    },
                    "identifying": {
                        "type": "boolean",
                        "label": "Identify Antenna",
                        "control": True,
                    },
                },
                "summary_fields": ["state", "band", "error"],
                "instances": {
                    "ids": ["a", "b", "c", "d"],
                    "label": "DAD {id}",
                    "presence": "reported",
                },
            },
        },
        "commands": {
            "refresh": {
                "label": "Refresh from Base Station",
                "query_for": "paired_count",
            },
            "set_station_identify": {
                "label": "Set Station Identify",
                "params": {
                    "value": {
                        "type": "boolean",
                        "label": "Station Identify",
                        "required": True,
                    }
                },
                "sets": {"identifying": "{value}"},
            },
            "read_station_identify": {
                "label": "Read Station Identify",
                "query_for": "identifying",
            },
            "set_mobile_identify": {
                "label": "Set Identify Mobile Device",
                "params": {
                    "mobile": {
                        "type": "child_id",
                        "child_type": "mobile",
                        "required": True,
                        "label": "Paired Device",
                    },
                    "value": {
                        "type": "boolean",
                        "label": "Identify Mobile Device",
                        "required": True,
                    },
                },
                "sets": {"identifying": "{value}"},
            },
            "read_mobile_identify": {
                "label": "Read Identify Mobile Device",
                "params": {
                    "mobile": {
                        "type": "child_id",
                        "child_type": "mobile",
                        "required": True,
                        "label": "Paired Device",
                    }
                },
                "query_for": "identifying",
            },
            "set_mobile_gain": {
                "label": "Set Mic Preamp Gain",
                "params": {
                    "mobile": {
                        "type": "child_id",
                        "child_type": "mobile",
                        "required": True,
                        "label": "Paired Device",
                    },
                    "value": {
                        "type": "integer",
                        "label": "Mic Preamp Gain",
                        "min": -10,
                        "max": 42,
                        "unit": "dB",
                        "required": True,
                    },
                },
                "sets": {"gain_db": "{value}"},
            },
            "read_mobile_gain": {
                "label": "Read Mic Preamp Gain",
                "params": {
                    "mobile": {
                        "type": "child_id",
                        "child_type": "mobile",
                        "required": True,
                        "label": "Paired Device",
                    }
                },
                "query_for": "gain_db",
            },
            "set_headphone_volume": {
                "label": "Set SEK Headphone Volume",
                "params": {
                    "mobile": {
                        "type": "child_id",
                        "child_type": "mobile",
                        "required": True,
                        "label": "Paired Device",
                    },
                    "value": {
                        "type": "number",
                        "label": "SEK Headphone Volume",
                        "min": -100,
                        "max": 27.5,
                        "unit": "dB",
                        "required": True,
                    },
                },
                "sets": {"headphone_db": "{value}"},
            },
            "read_headphone_volume": {
                "label": "Read SEK Headphone Volume",
                "params": {
                    "mobile": {
                        "type": "child_id",
                        "child_type": "mobile",
                        "required": True,
                        "label": "Paired Device",
                    }
                },
                "query_for": "headphone_db",
            },
            "set_command_behavior": {
                "label": "Set Command Button Mode",
                "params": {
                    "mobile": {
                        "type": "child_id",
                        "child_type": "mobile",
                        "required": True,
                        "label": "Paired Device",
                    },
                    "value": {
                        "type": "enum",
                        "label": "Command Button Mode",
                        "values": ["Disabled", "Momentary", "Latching"],
                        "required": True,
                    },
                },
                "sets": {"command_behavior": "{value}"},
            },
            "read_command_behavior": {
                "label": "Read Command Button Mode",
                "params": {
                    "mobile": {
                        "type": "child_id",
                        "child_type": "mobile",
                        "required": True,
                        "label": "Paired Device",
                    }
                },
                "query_for": "command_behavior",
            },
            "set_rf_mute": {
                "label": "Set RF Mute Requested",
                "params": {
                    "rf": {
                        "type": "child_id",
                        "child_type": "rf",
                        "required": True,
                        "label": "RF Channel",
                    },
                    "value": {
                        "type": "boolean",
                        "label": "RF Mute Requested",
                        "required": True,
                    },
                },
                "sets": {"requested_mute": "{value}"},
                "confirm": "Changing carrier mute affects every mobile device using this RF "
                "channel.",
            },
            "read_rf_mute": {
                "label": "Read RF Mute Requested",
                "params": {
                    "rf": {
                        "type": "child_id",
                        "child_type": "rf",
                        "required": True,
                        "label": "RF Channel",
                    }
                },
                "query_for": "requested_mute",
            },
            "set_antenna_identify": {
                "label": "Set Identify Antenna",
                "params": {
                    "antenna": {
                        "type": "child_id",
                        "child_type": "antenna",
                        "required": True,
                        "label": "Antenna Port",
                    },
                    "value": {
                        "type": "boolean",
                        "label": "Identify Antenna",
                        "required": True,
                    },
                },
                "sets": {"identifying": "{value}"},
            },
            "read_antenna_identify": {
                "label": "Read Identify Antenna",
                "params": {
                    "antenna": {
                        "type": "child_id",
                        "child_type": "antenna",
                        "required": True,
                        "label": "Antenna Port",
                    }
                },
                "query_for": "identifying",
            },
            "set_aoip_enabled_mode": {
                "label": "Set AoIP With Command Enabled",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Audio Output",
                    },
                    "value": {
                        "type": "enum",
                        "label": "AoIP With Command Enabled",
                        "values": ["On", "Off", "Talk", "Mute"],
                        "required": True,
                    },
                },
                "sets": {"aoip_enabled_mode": "{value}"},
            },
            "read_aoip_enabled_mode": {
                "label": "Read AoIP With Command Enabled",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Audio Output",
                    }
                },
                "query_for": "aoip_enabled_mode",
            },
            "set_aoip_disabled_mode": {
                "label": "Set AoIP With Command Disabled",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Audio Output",
                    },
                    "value": {
                        "type": "enum",
                        "label": "AoIP With Command Disabled",
                        "values": ["On", "Off"],
                        "required": True,
                    },
                },
                "sets": {"aoip_disabled_mode": "{value}"},
            },
            "read_aoip_disabled_mode": {
                "label": "Read AoIP With Command Disabled",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Audio Output",
                    }
                },
                "query_for": "aoip_disabled_mode",
            },
            "read_aoip_muted": {
                "label": "Read AoIP Route Muted",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Audio Output",
                    }
                },
                "query_for": "aoip_muted",
            },
            "set_madi1_enabled_mode": {
                "label": "Set MADI 1 With Command Enabled",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Audio Output",
                    },
                    "value": {
                        "type": "enum",
                        "label": "MADI 1 With Command Enabled",
                        "values": ["On", "Off", "Talk", "Mute"],
                        "required": True,
                    },
                },
                "sets": {"madi1_enabled_mode": "{value}"},
            },
            "read_madi1_enabled_mode": {
                "label": "Read MADI 1 With Command Enabled",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Audio Output",
                    }
                },
                "query_for": "madi1_enabled_mode",
            },
            "set_madi1_disabled_mode": {
                "label": "Set MADI 1 With Command Disabled",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Audio Output",
                    },
                    "value": {
                        "type": "enum",
                        "label": "MADI 1 With Command Disabled",
                        "values": ["On", "Off"],
                        "required": True,
                    },
                },
                "sets": {"madi1_disabled_mode": "{value}"},
            },
            "read_madi1_disabled_mode": {
                "label": "Read MADI 1 With Command Disabled",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Audio Output",
                    }
                },
                "query_for": "madi1_disabled_mode",
            },
            "read_madi1_muted": {
                "label": "Read MADI 1 Route Muted",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Audio Output",
                    }
                },
                "query_for": "madi1_muted",
            },
            "set_madi2_enabled_mode": {
                "label": "Set MADI 2 With Command Enabled",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Audio Output",
                    },
                    "value": {
                        "type": "enum",
                        "label": "MADI 2 With Command Enabled",
                        "values": ["On", "Off", "Talk", "Mute"],
                        "required": True,
                    },
                },
                "sets": {"madi2_enabled_mode": "{value}"},
            },
            "read_madi2_enabled_mode": {
                "label": "Read MADI 2 With Command Enabled",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Audio Output",
                    }
                },
                "query_for": "madi2_enabled_mode",
            },
            "set_madi2_disabled_mode": {
                "label": "Set MADI 2 With Command Disabled",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Audio Output",
                    },
                    "value": {
                        "type": "enum",
                        "label": "MADI 2 With Command Disabled",
                        "values": ["On", "Off"],
                        "required": True,
                    },
                },
                "sets": {"madi2_disabled_mode": "{value}"},
            },
            "read_madi2_disabled_mode": {
                "label": "Read MADI 2 With Command Disabled",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Audio Output",
                    }
                },
                "query_for": "madi2_disabled_mode",
            },
            "read_madi2_muted": {
                "label": "Read MADI 2 Route Muted",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Audio Output",
                    }
                },
                "query_for": "madi2_muted",
            },
            "read_battery_percent": {
                "label": "Read Battery Remaining",
                "params": {
                    "mobile": {
                        "type": "child_id",
                        "child_type": "mobile",
                        "required": True,
                        "label": "Paired Device",
                    }
                },
                "query_for": "battery_percent",
            },
            "read_battery_minutes": {
                "label": "Read Battery Runtime",
                "params": {
                    "mobile": {
                        "type": "child_id",
                        "child_type": "mobile",
                        "required": True,
                        "label": "Paired Device",
                    }
                },
                "query_for": "battery_minutes",
            },
            "read_mic_quality": {
                "label": "Read MIC Link Quality",
                "params": {
                    "mobile": {
                        "type": "child_id",
                        "child_type": "mobile",
                        "required": True,
                        "label": "Paired Device",
                    }
                },
                "query_for": "mic_quality",
            },
            "read_iem_quality": {
                "label": "Read IEM Link Quality",
                "params": {
                    "mobile": {
                        "type": "child_id",
                        "child_type": "mobile",
                        "required": True,
                        "label": "Paired Device",
                    }
                },
                "query_for": "iem_quality",
            },
            "read_state": {
                "label": "Read Wireless Connection",
                "params": {
                    "mobile": {
                        "type": "child_id",
                        "child_type": "mobile",
                        "required": True,
                        "label": "Paired Device",
                    }
                },
                "query_for": "state",
            },
            "read_headphone_muted": {
                "label": "Read SEK Headphone Volume at Mute",
                "params": {
                    "mobile": {
                        "type": "child_id",
                        "child_type": "mobile",
                        "required": True,
                        "label": "Paired Device",
                    }
                },
                "query_for": "headphone_muted",
            },
            "read_mic_active": {
                "label": "Read Microphone Link Active",
                "params": {
                    "mobile": {
                        "type": "child_id",
                        "child_type": "mobile",
                        "required": True,
                        "label": "Paired Device",
                    }
                },
                "query_for": "mic_active",
            },
            "read_iem_active": {
                "label": "Read IEM Link Active",
                "params": {
                    "mobile": {
                        "type": "child_id",
                        "child_type": "mobile",
                        "required": True,
                        "label": "Paired Device",
                    }
                },
                "query_for": "iem_active",
            },
            "route_microphone": {
                "label": "Route Paired Microphone to Output",
                "help": "Route an existing microphone link to one base-station output. The "
                "source must have a unique microphone link assigned in LinkDesk.",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "label": "Audio Output",
                        "required": True,
                    },
                    "mobile": {
                        "type": "integer",
                        "options_state": "microphone_choices",
                        "min": 0,
                        "max": 4294967295,
                        "label": "Paired Microphone",
                        "required": True,
                    },
                },
                "sets": {"source_uid": "{mobile}"},
            },
            "clear_output": {
                "label": "Clear Microphone from Output",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                    }
                },
                "sets": {"assigned": False},
                "help": "Remove this output's microphone link assignment. The wireless link and "
                "its other destinations keep their assignments.",
            },
            "read_output_source": {
                "label": "Read Output Microphone",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Audio Output",
                    }
                },
                "query_for": "source_uid",
            },
        },
        "routing": {
            "destination_child_type": "output",
            "source_child_type": "mobile",
            "command": "route_microphone",
            "destination_param": "output",
            "source_param": "mobile",
            "planes": [{"label": "Microphone Audio", "route_property": "source_uid"}],
        },
        "actions": [
            {
                "id": "refresh",
                "kind": "command",
                "label": "Refresh Paired Devices",
                "command": "refresh",
            },
            {
                "id": "open_web_ui",
                "kind": "link",
                "label": "Open Spectera WebUI",
                "url": "https://{host}:{port}/",
                "icon": "external-link",
            },
        ],
    }

    def __init__(self, device_id, config, state, events):
        super().__init__(
            device_id, {**self.DRIVER_INFO["default_config"], **config}, state, events
        )
        self._lock = asyncio.Lock()
        self._rows = {path: {} for path in COLLECTIONS}
        self._dirty = set()
        self._refresh_task = None
        self._closing = False
        self._auth_refused = False
        self._writer_id = "OpenAVC-" + uuid.uuid4().hex

    async def _pre_connect(self):
        self._closing = False
        self._auth_refused = False
        if not self.config.get("password"):
            raise ConnectionFaultError(
                "Enter the Spectera device password from LinkDesk or the WebUI.",
                code="auth_failed",
            )
        self.config.update(ssl=True, auth_type="basic", username="controlSennheiser")

    async def _request(self, method, path, body=None):
        if self.transport is None:
            raise ConnectionError("The Spectera connection has closed.")
        auth_message = "Check the Spectera device password and account access in LinkDesk or the WebUI."
        if self._auth_refused:
            raise ConnectionFaultError(auth_message, code="auth_failed")
        reply = await self.transport.request(method, path, json_body=body)
        if reply.status_code in (401, 403):
            self._auth_refused = True
            if self.connected:
                self._force_disconnect("auth_failed", auth_message)
            raise ConnectionFaultError(auth_message, code="auth_failed")
        if reply.status_code in (400, 404, 405, 409, 422):
            raise CommandRefusedError(
                f"Spectera refused {method} {path} (HTTP {reply.status_code})."
            )
        if not reply.ok:
            raise ConnectionError(
                f"Spectera returned HTTP {reply.status_code} for {path}."
            )
        if method != "GET":
            return None
        return (
            reply.json_data if reply.json_data is not None else json.loads(reply.text)
        )

    async def _post_connect(self):
        identity = await self._request("GET", "/api/device/identity")
        if field(identity, "product") != "Spectera-Base-Station":
            raise ConnectionFaultError(
                "This address did not identify as a Spectera Base Station.",
                code="invalid_config",
            )
        version = field(await self._request("GET", "/api/ssc/version"), "schema")
        match = re.fullmatch(r"18\.(\d+)", str(version))
        if not match or int(match[1]) < 1:
            raise ConnectionFaultError(
                "This driver requires Spectera API 18.1 or later in the 18.x family.",
                code="invalid_config",
            )
        self.set_states(
            {
                "model": field(identity, "variant.articleName") or identity["product"],
                "serial": identity.get("serial"),
                "api_version": version,
            }
        )
        # This first protected read happens before the platform opens the event stream.
        await self._read_paths([MOBILES])

    async def _initial_sync(self):
        await self.poll()

    async def _read_paths(self, paths):
        """Serialize GETs with writes and push refreshes. Publish related resources together."""
        async with self._lock:
            await self._read_locked(paths)

    async def _read_locked(self, paths):
        if self._closing:
            return
        changes = {}
        try:
            for path in paths:
                try:
                    body = await self._request("GET", path)
                    if path in COLLECTIONS:
                        changes[path] = indexed_rows(path, body)
                    elif isinstance(body, dict):
                        changes[path] = body
                    else:
                        raise ValueError("Spectera returned an invalid resource")
                except Exception:
                    self._expire(path)
                    raise
        finally:
            if not self._closing:
                self._apply(changes)

    def _expire(self, path):
        if path in PARENT:
            self.set_states(dict.fromkeys(PARENT[path]))
            return
        kind = COLLECTIONS[path][0]
        self._rows[path].clear()
        for ident in self.list_children(kind):
            self._clear_child(kind, ident)
        if path == MOBILES:
            self.set_states(
                {
                    "paired_count": None,
                    "connected_count": None,
                    "microphone_choices": "[]",
                }
            )
            self._publish_outputs()

    def _clear_child(self, kind, ident):
        keys = self.DRIVER_INFO["child_entity_types"][kind]["state_variables"]
        transient = {
            key: None
            for key in keys
            if key not in ("name", "model", "serial", "firmware_version")
        }
        self.set_child_state_batch(
            kind,
            ident,
            {
                **transient,
                "online": None,
                "offline_reason": None,
                "offline_detail": None,
            },
        )

    def _apply(self, changes):
        for path, body in changes.items():
            if path in PARENT:
                self.set_states(
                    {key: field(body, wire) for key, wire in PARENT[path].items()}
                )
            else:
                self._rows[path] = body
        for path in (MOBILES, RF, ANTENNAS):
            if path in changes:
                self._publish_children(path)
        if OUTPUTS in changes or MOBILES in changes:
            self._publish_outputs()

    def _publish_children(self, path):
        rows = self._rows[path]
        kind = COLLECTIONS[path][0]
        updates = []
        for wire_id, row in rows.items():
            ident = local_id(path, wire_id)
            if path == MOBILES:
                props = mobile_state(row)
                status = props["state"]
                if status == "Connected":
                    presence = self.child_fault()
                elif status is None:
                    presence = {
                        "online": None,
                        "offline_reason": None,
                        "offline_detail": None,
                    }
                else:
                    presence = self.child_fault(
                        CHILD_NOT_RESPONDING,
                        f"Spectera reports this paired device as {status}.",
                    )
            elif path == ANTENNAS:
                props = antenna_state(row)
                status = props["state"]
                if status == "Unconnected":
                    presence = self.child_fault(CHILD_NOT_FITTED)
                elif status == "Error":
                    presence = self.child_fault(
                        CHILD_SERVICE_FAULT,
                        f"DAD antenna error: {props['error'] or 'unspecified'}.",
                    )
                elif status is None:
                    presence = {
                        "online": None,
                        "offline_reason": None,
                        "offline_detail": None,
                    }
                else:
                    presence = self.child_fault()
            else:
                props = wireless_state(row)
                presence = self.child_fault()
            props.update(presence)
            self.register_child(kind, ident, initial_state=props)
            updates.append((kind, ident, props))
        self.set_children_state_batch(updates)
        seen = {local_id(path, wire_id) for wire_id in rows}
        for missing in set(self.list_children(kind)) - seen:
            if path == MOBILES:
                self.deregister_child(kind, missing)
            else:
                self._clear_child(kind, missing)
        if path == MOBILES:
            links = {}
            for row in rows.values():
                link = measurement(row.get("micAudiolinkId"), 0, 255)
                if link is not None:
                    links[link] = links.get(link, 0) + 1
            choices = [
                {"value": uid, "label": row.get("name") or f"{row['type']} {uid}"}
                for uid, row in rows.items()
                if links.get(row.get("micAudiolinkId")) == 1
            ]
            self.set_state("microphone_choices", json.dumps(choices))
            states = [row.get("state") for row in rows.values()]
            self.set_states(
                {
                    "paired_count": len(rows),
                    "connected_count": None
                    if None in states
                    else states.count("Connected"),
                }
            )

    def _publish_outputs(self):
        sources = {}
        for row in self._rows[MOBILES].values():
            link = measurement(row.get("micAudiolinkId"), 0, 255)
            if link is not None:
                sources.setdefault(link, []).append(row)
        updates = []
        outputs = self._rows[OUTPUTS]
        for wire_id, row in outputs.items():
            ident = local_id(OUTPUTS, wire_id)
            props = {**output_state(row, sources), **self.child_fault()}
            self.register_child("output", ident, initial_state=props)
            updates.append(("output", ident, props))
        self.set_children_state_batch(updates)
        for ident in self.list_children("output"):
            if ident - 1 not in outputs:
                self._clear_child("output", ident)

    async def poll(self):
        await self._read_paths(RESOURCES)

    async def refresh_children(self):
        await self._read_paths(COLLECTIONS)
        return {kind: len(self.list_children(kind)) for kind, _ in COLLECTIONS.values()}

    async def _on_push_session(self, session_id):
        await super()._on_push_session(session_id)
        self.set_state("events_active", False)
        if self._closing:
            return
        if not session_id:
            # The HTTP control connection can outlive its stream. Clear live readings
            # until an authoritative GET or a newly armed stream restores them.
            async with self._lock:
                self._expire(MOBILES)
                self._expire(OUTPUTS)
                for path in PARENT:
                    self._expire(path)
                self._expire(ANTENNAS)
                self._expire(RF)
            return
        # The snapshot remains essential even if the station refuses a subscription list.
        try:
            await self._request("PUT", f"{SESSION}/{session_id}", list(RESOURCES))
        except CommandRefusedError:
            await self.poll()
            raise
        # Replaying existing children alone cannot reveal an unpair during the outage.
        await self.poll()
        self.set_state("events_active", True)

    async def _push_unregister(self):
        if self.push_session and self.transport is not None:
            try:
                await self._request("DELETE", f"{SESSION}/{self.push_session}")
            except Exception:
                log.debug("Spectera event session already closed", exc_info=True)

    async def _handle_push_event(self, data: bytes):
        try:
            event = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(event, dict) or self._closing:
            return
        # A creation announcement can be {}, deletion null, and updates can be partial.
        # Read affected resources once per burst; queued notifications cannot overwrite a later GET.
        for path in event:
            normalized = path if path.startswith("/api/") else "/api" + path
            for resource in RESOURCES:
                if normalized == resource or (
                    resource in COLLECTIONS and normalized.startswith(resource + "/")
                ):
                    self._dirty.add(resource)
                    break
        if self._dirty and (self._refresh_task is None or self._refresh_task.done()):
            self._refresh_task = asyncio.create_task(self._drain_changes())

    async def _drain_changes(self):
        while self._dirty and not self._closing:
            await asyncio.sleep(0.35)
            paths, self._dirty = self._dirty, set()
            try:
                await self._read_paths(sorted(paths))
            except Exception:
                log.warning(
                    "Spectera live refresh failed; the next poll reconciles the resource.",
                    exc_info=True,
                )

    async def _close_session(self):
        self._closing = True
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            await asyncio.gather(self._refresh_task, return_exceptions=True)
            self._refresh_task = None
        self._dirty.clear()
        for records in self._rows.values():
            records.clear()
        for kind, _ in COLLECTIONS.values():
            for ident in self.list_children(kind):
                self._clear_child(kind, ident)
        transient = {key: None for path in PARENT.values() for key in path}
        self.set_states(
            {
                **transient,
                "paired_count": None,
                "connected_count": None,
                "microphone_choices": "[]",
                "events_active": False,
            }
        )

    def _target(self, params, spec):
        for name, param in spec.get("params", {}).items():
            if param.get("type") == "child_id":
                raw = params.get(name)
                ident = raw if name == "antenna" else int(str(raw))
                if not self.is_child_registered(name, ident):
                    raise ValueError(f"Select a discovered {name}.")
                path = {
                    "mobile": MOBILES,
                    "output": OUTPUTS,
                    "rf": RF,
                    "antenna": ANTENNAS,
                }[name]
                wire_id = ident - 1 if name in ("output", "rf") else ident
                return path, wire_id, name, ident
        return IDENTIFY, None, None, None

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = params or {}
        spec = self.DRIVER_INFO["commands"].get(command)
        if spec is None:
            raise ValueError(f"Unknown Spectera command {command}")
        if command == "refresh":
            await self.poll()
            return self.get_state("paired_count")
        if command in ("route_microphone", "clear_output"):
            return await self._route_output(command, params)
        path, wire_id, kind, ident = self._target(params, spec)
        reads = (MOBILES, OUTPUTS) if path == OUTPUTS else (path,)
        prop = spec.get("query_for")
        async with self._lock:
            if prop:
                await self._read_locked(reads)
                return (
                    self.get_child_state(kind, ident).get(prop)
                    if kind
                    else self.get_state(prop)
                )
            prop = next(iter(spec["sets"]))
            value = command_value(params.get("value"), spec["params"]["value"])
            # Re-read mobile identity before writing because its type is mandatory on every PUT.
            if kind == "mobile":
                await self._read_locked([MOBILES])
            target, body = self._write_body(path, wire_id, prop, value)
            await self._write_and_read(target, body, reads)
            reported = (
                self.get_child_state(kind, ident).get(prop)
                if kind
                else self.get_state(prop)
            )
            if reported != value:
                raise CommandOutcomeError("uncertain")
            return reported

    async def _route_output(self, command, params):
        output = int(str(params.get("output")))
        uid = int(str(params["mobile"])) if command == "route_microphone" else None
        async with self._lock:
            await self._read_locked((MOBILES, OUTPUTS))
            if output - 1 not in self._rows[OUTPUTS]:
                raise ValueError("Select a reported audio output.")
            link = -1
            if uid is not None:
                row = self._rows[MOBILES].get(uid, {})
                link = measurement(row.get("micAudiolinkId"), 0, 255)
                if (
                    link is None
                    or sum(
                        r.get("micAudiolinkId") == link
                        for r in self._rows[MOBILES].values()
                    )
                    != 1
                ):
                    raise CommandRefusedError(
                        "The selected microphone needs a unique audio link assigned in LinkDesk."
                    )
            target = f"{OUTPUTS}/{output - 1}"
            await self._write_and_read(
                target,
                {"outputId": output - 1, "micAudiolinkId": link},
                (MOBILES, OUTPUTS),
            )
            current = self._rows[OUTPUTS].get(output - 1, {})
            reported = self.get_child_state("output", output).get("source_uid")
            if current.get("micAudiolinkId") != link or reported != uid:
                raise CommandOutcomeError("uncertain")
            return reported

    @asynccontextmanager
    async def _write_access(self):
        # SSCv2's usage mark is advisory. A second read catches a competing claim.
        owner = field(await self._request("GET", USAGE), "usedBy")
        if owner not in ("", self._writer_id):
            raise CommandRefusedError(
                "Another client has exclusive access, or Spectera could not confirm write access."
            )
        await self._request("PUT", USAGE, {"usedBy": self._writer_id, "timer": 15})
        try:
            if field(await self._request("GET", USAGE), "usedBy") != self._writer_id:
                raise CommandRefusedError(
                    "Another client acquired Spectera write access."
                )
            yield
        finally:
            # A lost connection releases its own claim by the device-side timer.
            try:
                if (
                    field(await self._request("GET", USAGE), "usedBy")
                    == self._writer_id
                ):
                    await self._request("PUT", USAGE, {"usedBy": "", "timer": 5})
            except Exception:
                log.debug(
                    "Spectera write mark will expire on the device.", exc_info=True
                )

    async def _audio_settled(self):
        async with asyncio.timeout(4):
            while True:
                count = field(await self._request("GET", PENDING), "numPendingActions")
                if type(count) is not int or count < 0:
                    raise ValueError(
                        "Spectera did not report the number of pending audio actions."
                    )
                if count == 0:
                    return
                await asyncio.sleep(0.15)

    async def _write_and_read(self, target, body, reads):
        async with self._write_access():
            try:
                await self._request("PUT", target, body)
            except (CommandRefusedError, ConnectionFaultError):
                raise
            except Exception as exc:
                raise CommandOutcomeError("uncertain") from exc
            try:
                if target.startswith(OUTPUTS + "/"):
                    await self._audio_settled()
                await self._read_locked(reads)
            except Exception as exc:
                raise CommandOutcomeError("uncertain") from exc

    def _write_body(self, path, wire_id, prop, value):
        if path == IDENTIFY:
            return path, {"visual": value}
        row = self._rows[path].get(wire_id)
        if row is None:
            raise CommandRefusedError(
                "The station no longer reports this device or port."
            )
        body = {COLLECTIONS[path][1]: wire_id}
        if path == MOBILES:
            if row.get("state") != "Connected":
                raise CommandRefusedError("The paired device is not connected.")
            body["type"] = row["type"]
            key = {
                "identifying": "identify",
                "gain_db": "micPreampGain",
                "headphone_db": "headphoneVolume",
                "command_behavior": "commandBehavior",
            }[prop]
            if prop == "gain_db" and row["type"] == "SEK" and value < -6:
                raise ValueError("SEK gain starts at -6 dB.")
            if prop == "headphone_db":
                if row["type"] != "SEK":
                    raise ValueError("Headphone volume requires an SEK bodypack.")
                if (
                    not row.get("headphoneVolumeMin", -100)
                    <= value
                    <= row.get("headphoneVolumeMax", 27.5)
                ):
                    raise ValueError(
                        "Volume is outside the performer's configured headphone limits."
                    )
        elif path == OUTPUTS:
            dest, mode, _ = prop.split("_")
            key = DESTINATIONS[dest] + (
                "EnableIfCommandIsEnabled"
                if mode == "enabled"
                else "EnableIfCommandIsDisabled"
            )
        elif path == RF:
            key, value = "rfState", "RfMuted" if value else "RfActive"
        else:
            key = "identify"
        body[key] = value
        return f"{path}/{wire_id}", body
