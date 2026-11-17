"""Turtle AV Mineola XLR family. See each size's API Control Command chapter.

A status table numbers inputs and outputs independently. Commit both rosters only
after the network header closes the audio section. All four manuals print XLR22,
so the model token cannot select a channel count. The API documents queries
and replies. Poll for device-side edits. Dante subscriptions are separate.
"""

import asyncio
from collections import Counter, deque
from decimal import Decimal, InvalidOperation
import re

from openavc.drivers.base import BaseDriver

try:
    from openavc.drivers.base import CommandRefusedError
except (
    ImportError
):  # Older releases surface a command refusal as a normal command failure.
    CommandRefusedError = RuntimeError

SIZES = (2, 4, 8, 16)
NUMBER = r"[+-]?\d+(?:\.\d+)?"
LEVEL = r"[+-]?\d+\s*dB[uV]"
IDENTITY = re.compile(r"Status Info\s+(.{1,80}?)\s+Audio Bridge", re.I)
INPUT = re.compile(
    rf"(\d+)\s+(.+?)\s+(on|off)\s+({LEVEL})\s+({NUMBER})(?:dB)?\s+(on|off)", re.I
)
OUTPUT = re.compile(
    rf"(\d+)\s+(.+?)\s+({LEVEL})\s+(\d+)\s+(on|off)\s+(\d+)(?:ms)?", re.I
)
FIRMWARE = re.compile(r"Web\s+(\S+)\s+MCU\s+(\S+)\s+DEP\s+(\S+)", re.I)
POWER = re.compile(
    r"(on|off)\s+(standby|sleep)\s+(off|\d+)(?:\s*m\s*ins?)?\s+\d+", re.I
)
ON_OFF = {"on": True, "off": False}
FILTERS = ("Parametric", "Lowpass", "Highpass", "Low Shelf", "High Shelf")
BANKS = ("Flat", "Custom1", "Custom2")
INPUT_LEVELS = ("+24dBu", "+14dBu", "+4dBu", "0dBV", "-18dBV", "-35dBV")
OUTPUT_LEVELS = ("+20dBu", *INPUT_LEVELS[1:])
TIMEOUT = 2.5


def checked_number(raw, low, high, step=1):
    try:
        value = Decimal(str(raw))
        if not value.is_finite():
            raise ValueError("Choose a finite value.")
        if low <= value <= high and value % Decimal(str(step)) == 0:
            return int(value) if step == 1 else float(value)
    except (InvalidOperation, ValueError):
        pass
    raise ValueError(f"Choose a value from {low} to {high} in steps of {step}.")


def switch(raw):
    if isinstance(raw, bool):
        return raw
    values = {**ON_OFF, "true": True, "false": False}
    if str(raw).lower() not in values:
        raise ValueError("Choose on or off.")
    return values[str(raw).lower()]


def ref_level(raw, choices):
    value = raw.replace(" ", "")
    return next(
        (option for option in choices if option.casefold() == value.casefold()), value
    )


def input_row(match):
    _, name, phantom, sensitivity, gain, mute = match.groups()
    return dict(
        device_name=name,
        phantom=switch(phantom),
        sensitivity=ref_level(sensitivity, INPUT_LEVELS),
        gain=checked_number(gain, -12, 12, 0.1),
        mute=switch(mute),
    )


def output_row(match):
    _, name, reference, volume, mute, delay = match.groups()
    return dict(
        device_name=name,
        reference=ref_level(reference, OUTPUT_LEVELS),
        volume=checked_number(volume, 0, 100),
        mute=switch(mute),
        delay=checked_number(delay, 0, 50),
    )


def table_rows(lines, pattern, parse):
    rows = {}
    for line in lines:
        match = pattern.fullmatch(line)
        if not match or int(match[1]) in rows:
            raise ValueError("Incomplete or repeated Mineola channel row.")
        rows[int(match[1])] = parse(match)
    return rows


def system_fields(lines):
    values = dict.fromkeys(
        (
            "power",
            "standby_mode",
            "auto_standby",
            "firmware_web",
            "firmware_mcu",
            "firmware_dep",
        )
    )
    for line in lines:
        if match := FIRMWARE.fullmatch(line):
            values.update(
                zip(("firmware_web", "firmware_mcu", "firmware_dep"), match.groups())
            )
        if match := POWER.fullmatch(line):
            timer = 0 if match[3].lower() == "off" else int(match[3])
            values.update(
                power=switch(match[1]),
                standby_mode=match[2].title(),
                auto_standby=timer,
            )
    return values


def section(lines, name):
    return next(
        i for i, line in enumerate(lines) if re.match(rf"{name}\s+Name\s+", line, re.I)
    )


def parse_status(lines):
    inp, out = section(lines, "Input"), section(lines, "Output")
    inputs = table_rows(lines[inp + 1 : out], INPUT, input_row)
    outputs = table_rows(lines[out + 1 :], OUTPUT, output_row)
    count = len(inputs)
    if count not in SIZES:
        raise ValueError("Unrecognized Mineola channel count.")
    if not inputs.keys() == outputs.keys() == set(range(1, count + 1)):
        raise ValueError("Incomplete Mineola channel roster.")
    system = system_fields(lines[1:inp])
    return {**system, "model": IDENTITY.fullmatch(lines[0])[1], "channels": count}, {
        "input": inputs,
        "output": outputs,
    }


class StatusReply:
    def __init__(self):
        self.lines = []

    def __call__(self, line):
        if IDENTITY.fullmatch(line):
            self.lines = [line]
        elif not self.lines or not line:
            return None
        elif re.match(r"TCP/IP\s+Telnet\b", line, re.I):
            return parse_status(self.lines)
        elif len(self.lines) < 64:
            self.lines.append(line)
        else:
            raise ValueError("Mineola status table exceeded its expected size.")
        return None


BAND = re.compile(
    rf"(\d):\s*Type:\s*([1-5]),\s*Frequency:\s*({NUMBER})\s*Hz,\s*Value:\s*({NUMBER})\s*dB,\s*Q:\s*({NUMBER})\s+(on|off)",
    re.I,
)


def parse_band(match):
    n, typ, freq, gain, q, enabled = match.groups()
    return int(n), dict(
        filter_type=FILTERS[int(typ) - 1],
        frequency=checked_number(freq, 20, 20000, 0.1),
        gain=checked_number(gain, -15, 15, 0.1),
        q=checked_number(q, 0.02, 16, 0.01),
        enabled=switch(enabled),
    )


class EqualizerReply:
    def __init__(self, names):
        self.headers = {f"dante {name} eq".casefold() for name in names}
        self.text = None
        self.bands = {}

    def __call__(self, line):
        if line.casefold() in self.headers:
            self.text, self.bands = "", {}
            return None
        if self.text is None or not line:
            return None
        self.text = (self.text + " " + line).strip()
        if len(self.text) > 256:
            raise ValueError("Unreadable Mineola EQ band.")
        if match := BAND.fullmatch(self.text):
            self._add(match)
        return self.bands if len(self.bands) == 8 else None

    def _add(self, match):
        index, values = parse_band(match)
        if index in self.bands or not 1 <= index <= 8:
            raise ValueError("Unexpected EQ band number.")
        self.bands[index] = values
        self.text = ""


def match_reply(pattern, convert):
    compiled = re.compile(pattern, re.I)

    def parse(line):
        match = compiled.fullmatch(line)
        return convert(match[1]) if match else None

    return parse


def bounded_volume(raw):
    return checked_number(raw, 0, 100)


MASTER = {
    "master_volume": ("vol", match_reply(r"Master volume:\s*(\d+)", bounded_volume)),
    "master_mute": ("mute", match_reply(r"Master mute:\s*(on|off)", switch)),
    "member_mask": (
        "member",
        match_reply(r"(?:Master (?:output )?member:\s*)?([01]{1,16})", str),
    ),
}
TOKENS = {
    "gain": "gain",
    "sensitivity": "sensitivity",
    "phantom": "phantom power",
    "mute": "mute",
    "volume": "vol",
    "reference": "gain",
    "delay": "delay",
    "eq_preset": "eq preset",
}
CHOICES = {
    "sensitivity": INPUT_LEVELS,
    "reference": OUTPUT_LEVELS[:5],
    "eq_preset": BANKS,
}
RANGES = {"gain": (-12, 12, 0.1), "volume": (0, 100, 1), "delay": (0, 50, 1)}


def as_wire(prop, raw):
    if prop in CHOICES:
        return str(CHOICES[prop].index(raw) + 1), raw
    if prop in RANGES:
        value = checked_number(raw, *RANGES[prop])
        return f"{value:g}", value
    value = switch(raw)
    return ("on" if value else "off"), value


def band_id(kind, number, band):
    return f"{kind}-{number}-{band}"


def verify(actual, expected):
    if actual != expected:
        raise CommandRefusedError(
            "The bridge did not confirm the requested setting. See its current readback."
        )


class TurtleMineolaDriver(BaseDriver):
    DRIVER_INFO = {
        "id": "turtle_mineola",
        "name": "Turtle AV Mineola Dante Audio Bridges",
        "manufacturer": "TurtleAV",
        "category": "audio",
        "version": "1.0.0",
        "author": "OpenAVC",
        "min_platform_version": "0.36.0",
        "description": "Analog input and output control for the four Mineola XLR bridges. Discovers channel names "
        "and counts, controls master and channel levels, and recalls scenes and eight-band EQ "
        "settings.",
        "source_url": "https://turtleav.com/download/262/mineola-2x2/28704/mineola-2x2-user-guide.pdf",
        "tags": ["dante", "audio", "dsp", "preamp", "equalizer"],
        "verified": False,
        "simulated": True,
        "ports": [8000],
        "compatible_models": [
            {
                "manufacturer": "TurtleAV",
                "models": [
                    "Mineola 2x2",
                    "Mineola 4x4",
                    "Mineola 8x8",
                    "Mineola 16x16",
                ],
                "confidence": "untested",
                "notes": "Each XLR model has a public TCP command chapter. All four guides show "
                "the same two-channel status example. Larger layouts are exercised in "
                "simulation. Hardware qualification pending.",
            }
        ],
        "transport": "tcp",
        "help": {
            "overview": "Bind faders and mute buttons to named analog channels. Select a scene to recall its "
            "saved audio settings. Read a channel’s EQ to see its eight bands and enable or edit "
            "a filter. Configure Dante subscriptions in Dante Controller.",
            "setup": "Connect the bridge to the control network and enter its IP address and TCP control "
            "port. The first complete status report supplies the input and output pickers. Projects "
            "can label the five scene slots to match the installation. The TCP guide provides scene "
            "numbers only.",
            "connection": "Channel status and master controls refresh after connection, after a write, and "
            "at the feedback interval. EQ details load on request. Previously opened EQ "
            "channels refresh in rotation. Missing or incomplete readings show as unknown.",
        },
        "default_config": {
            "host": "",
            "port": 8000,
            "poll_interval": 10,
            "channel_count": "2",
        },
        "config_schema": {
            "channel_count": {
                "type": "enum",
                "label": "Bridge Size for Offline Design",
                "default": "2",
                "values": [
                    {"value": "2", "label": "2×2"},
                    {"value": "4", "label": "4×4"},
                    {"value": "8", "label": "8×8"},
                    {"value": "16", "label": "16×16"},
                ],
                "learned_from": "channels",
                "help": "Choose the planned size for panel design and simulation. A connected bridge supplies the actual channel count.",
            },
            "host": {"type": "string", "label": "Bridge Address", "required": True},
            "port": {
                "type": "integer",
                "label": "TCP Control Port",
                "min": 1,
                "max": 65535,
                "default": 8000,
            },
            "poll_interval": {
                "type": "integer",
                "label": "Feedback Interval (Seconds)",
                "min": 3,
                "max": 300,
                "default": 10,
                "advanced": True,
            },
        },
        "discovery": {
            "manufacturer_alias": ["TurtleAV", "Turtle AV"],
            "tcp_probe": {
                "port": 8000,
                "send_ascii": "get status\r\n",
                "expect_regex": "(?i)Status "
                "Info\\s+(?:XLR[0-9]+|(?:TAV-)?MINEOLA[\\w-]*)\\s+Audio "
                "Bridge",
                "timeout_ms": 2200,
            },
        },
        "state_variables": {
            "model": {"type": "string", "label": "Reported Model"},
            "channels": {"type": "integer", "label": "Channels per Direction"},
            "firmware_web": {"type": "string", "label": "Web Firmware"},
            "firmware_mcu": {"type": "string", "label": "MCU Firmware"},
            "firmware_dep": {"type": "string", "label": "Dante Firmware"},
            "power": {"type": "boolean", "label": "Power"},
            "standby_mode": {"type": "string", "label": "Standby Mode"},
            "auto_standby": {"type": "integer", "label": "Auto Standby", "unit": "min"},
            "master_volume": {
                "type": "integer",
                "label": "Master Volume",
                "min": 0,
                "max": 100,
                "control": True,
            },
            "master_mute": {
                "type": "boolean",
                "label": "Master Mute",
                "control": True,
                "cloud_priority": "high",
            },
            "member_mask": {"type": "string", "label": "Reported Master Output Mask"},
            "last_recalled": {
                "type": "integer",
                "label": "Last Confirmed Scene Recall",
                "help": "Last recall acknowledged in this connection. The bridge "
                "does not report an active scene.",
            },
        },
        "child_entity_types": {
            "input": {
                "label": "Analog Input",
                "label_plural": "Analog Inputs",
                "instances": {
                    "count_from": "channel_count",
                    "count_from_state": "channels",
                    "label": "Input {id}",
                },
                "id_format": {"type": "integer", "min": 1, "max": 16},
                "state_variables": {
                    "device_name": {"type": "string", "label": "Device Channel Name"},
                    "mute": {
                        "type": "boolean",
                        "label": "Mute",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "eq_preset": {
                        "type": "string",
                        "label": "EQ Preset",
                        "control": True,
                    },
                    "gain": {
                        "type": "number",
                        "label": "Input Gain",
                        "min": -12,
                        "max": 12,
                        "unit": "dB",
                        "step": 0.1,
                        "control": True,
                    },
                    "sensitivity": {
                        "type": "string",
                        "label": "Input Sensitivity",
                        "control": True,
                    },
                    "phantom": {
                        "type": "boolean",
                        "label": "48 V Phantom Power",
                        "control": True,
                    },
                },
                "summary_fields": ["gain", "mute", "phantom"],
            },
            "output": {
                "label": "Analog Output",
                "label_plural": "Analog Outputs",
                "instances": {
                    "count_from": "channel_count",
                    "count_from_state": "channels",
                    "label": "Output {id}",
                },
                "id_format": {"type": "integer", "min": 1, "max": 16},
                "state_variables": {
                    "device_name": {"type": "string", "label": "Device Channel Name"},
                    "mute": {
                        "type": "boolean",
                        "label": "Mute",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "eq_preset": {
                        "type": "string",
                        "label": "EQ Preset",
                        "control": True,
                    },
                    "volume": {
                        "type": "integer",
                        "label": "Output Volume",
                        "min": 0,
                        "max": 100,
                        "control": True,
                    },
                    "reference": {
                        "type": "string",
                        "label": "Output Reference Level",
                        "control": True,
                    },
                    "delay": {
                        "type": "integer",
                        "label": "Output Delay",
                        "min": 0,
                        "max": 50,
                        "unit": "ms",
                        "control": True,
                    },
                    "master_member": {
                        "type": "boolean",
                        "label": "Included in Master Out",
                    },
                },
                "summary_fields": ["volume", "mute", "delay"],
            },
            "eq_band": {
                "label": "EQ Band",
                "label_plural": "EQ Bands",
                "id_format": {"type": "string"},
                "state_variables": {
                    "enabled": {
                        "type": "boolean",
                        "label": "Band Enabled",
                        "control": True,
                    },
                    "filter_type": {
                        "type": "string",
                        "label": "Filter Type",
                        "control": True,
                    },
                    "frequency": {
                        "type": "number",
                        "label": "Frequency",
                        "min": 20,
                        "max": 20000,
                        "unit": "Hz",
                        "step": 0.1,
                        "control": True,
                    },
                    "gain": {
                        "type": "number",
                        "label": "Filter Gain",
                        "min": -15,
                        "max": 15,
                        "unit": "dB",
                        "step": 0.1,
                        "control": True,
                    },
                    "q": {
                        "type": "number",
                        "label": "Q",
                        "min": 0.02,
                        "max": 16,
                        "step": 0.01,
                        "control": True,
                    },
                },
                "summary_fields": ["enabled", "frequency", "gain"],
            },
            "scene": {
                "label": "Scene",
                "label_plural": "Scenes",
                "instances": {"count": 5, "label": "Scene {id}"},
                "id_format": {"type": "integer", "min": 1, "max": 5},
                "state_variables": {},
                "summary_fields": [],
            },
        },
        "commands": {
            "refresh": {
                "label": "Refresh Bridge",
                "help": "Read the channel names, input gain and mute, output levels and delay, "
                "and master controls.",
                "params": {},
                "query_for": "channels",
            },
            "read_input": {
                "label": "Read Input Status",
                "help": "Read current audio settings for the selected channel.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "label": "Input",
                        "child_type": "input",
                        "required": True,
                    }
                },
                "query_for": "mute",
            },
            "read_input_eq": {
                "label": "Read Input EQ",
                "help": "Read the selected channel’s EQ preset and eight filters. The "
                "bands become available in the EQ Band picker.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "label": "Input",
                        "child_type": "input",
                        "required": True,
                    }
                },
                "query_for": "eq_preset",
            },
            "set_input_gain": {
                "label": "Set Input Gain",
                "help": "Change input gain on the selected input and check the bridge’s "
                "readback.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "label": "Input",
                        "child_type": "input",
                        "required": True,
                    },
                    "value": {
                        "type": "number",
                        "label": "Input Gain",
                        "min": -12,
                        "max": 12,
                        "required": True,
                    },
                },
                "sets": {"gain": "{value}"},
            },
            "set_input_sensitivity": {
                "label": "Set Input Sensitivity",
                "help": "Change input sensitivity on the selected input and check "
                "the bridge’s readback.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "label": "Input",
                        "child_type": "input",
                        "required": True,
                    },
                    "value": {
                        "type": "enum",
                        "label": "Input Sensitivity",
                        "values": [
                            "+24dBu",
                            "+14dBu",
                            "+4dBu",
                            "0dBV",
                            "-18dBV",
                            "-35dBV",
                        ],
                        "required": True,
                    },
                },
                "sets": {"sensitivity": "{value}"},
            },
            "set_input_phantom": {
                "label": "Set Input 48 V Phantom Power",
                "help": "Change 48 V phantom power on the selected input and check the "
                "bridge’s readback.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "label": "Input",
                        "child_type": "input",
                        "required": True,
                    },
                    "value": {
                        "type": "boolean",
                        "label": "48 V Phantom Power",
                        "required": True,
                    },
                },
                "sets": {"phantom": "{value}"},
                "confirm": "Change 48 V phantom power on this microphone input?",
            },
            "set_input_mute": {
                "label": "Set Input Mute",
                "help": "Change mute on the selected input and check the bridge’s "
                "readback.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "label": "Input",
                        "child_type": "input",
                        "required": True,
                    },
                    "value": {"type": "boolean", "label": "Mute", "required": True},
                },
                "sets": {"mute": "{value}"},
            },
            "set_input_eq_preset": {
                "label": "Set Input EQ Preset",
                "help": "Change eq preset on the selected input and check the "
                "bridge’s readback.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "label": "Input",
                        "child_type": "input",
                        "required": True,
                    },
                    "value": {
                        "type": "enum",
                        "label": "EQ Preset",
                        "values": ["Flat", "Custom1", "Custom2"],
                        "required": True,
                    },
                },
                "sets": {"eq_preset": "{value}"},
            },
            "read_output": {
                "label": "Read Output Status",
                "help": "Read current audio settings for the selected channel.",
                "params": {
                    "output": {
                        "type": "child_id",
                        "label": "Output",
                        "child_type": "output",
                        "required": True,
                    }
                },
                "query_for": "mute",
            },
            "read_output_eq": {
                "label": "Read Output EQ",
                "help": "Read the selected channel’s EQ preset and eight filters. The "
                "bands become available in the EQ Band picker.",
                "params": {
                    "output": {
                        "type": "child_id",
                        "label": "Output",
                        "child_type": "output",
                        "required": True,
                    }
                },
                "query_for": "eq_preset",
            },
            "set_output_volume": {
                "label": "Set Output Volume",
                "help": "Change output volume on the selected output and check the "
                "bridge’s readback.",
                "params": {
                    "output": {
                        "type": "child_id",
                        "label": "Output",
                        "child_type": "output",
                        "required": True,
                    },
                    "value": {
                        "type": "integer",
                        "label": "Output Volume",
                        "min": 0,
                        "max": 100,
                        "required": True,
                    },
                },
                "sets": {"volume": "{value}"},
            },
            "set_output_reference": {
                "label": "Set Output Reference Level",
                "help": "Change output reference level on the selected output and "
                "check the bridge’s readback.",
                "params": {
                    "output": {
                        "type": "child_id",
                        "label": "Output",
                        "child_type": "output",
                        "required": True,
                    },
                    "value": {
                        "type": "enum",
                        "label": "Output Reference Level",
                        "values": ["+20dBu", "+14dBu", "+4dBu", "0dBV", "-18dBV"],
                        "required": True,
                    },
                },
                "sets": {"reference": "{value}"},
            },
            "set_output_delay": {
                "label": "Set Output Delay",
                "help": "Change output delay on the selected output and check the "
                "bridge’s readback.",
                "params": {
                    "output": {
                        "type": "child_id",
                        "label": "Output",
                        "child_type": "output",
                        "required": True,
                    },
                    "value": {
                        "type": "integer",
                        "label": "Output Delay",
                        "min": 0,
                        "max": 50,
                        "required": True,
                    },
                },
                "sets": {"delay": "{value}"},
            },
            "set_output_mute": {
                "label": "Set Output Mute",
                "help": "Change mute on the selected output and check the bridge’s "
                "readback.",
                "params": {
                    "output": {
                        "type": "child_id",
                        "label": "Output",
                        "child_type": "output",
                        "required": True,
                    },
                    "value": {"type": "boolean", "label": "Mute", "required": True},
                },
                "sets": {"mute": "{value}"},
            },
            "set_output_eq_preset": {
                "label": "Set Output EQ Preset",
                "help": "Change eq preset on the selected output and check the "
                "bridge’s readback.",
                "params": {
                    "output": {
                        "type": "child_id",
                        "label": "Output",
                        "child_type": "output",
                        "required": True,
                    },
                    "value": {
                        "type": "enum",
                        "label": "EQ Preset",
                        "values": ["Flat", "Custom1", "Custom2"],
                        "required": True,
                    },
                },
                "sets": {"eq_preset": "{value}"},
            },
            "set_master_volume": {
                "label": "Set Master Volume",
                "help": "Change master volume for the outputs assigned to the master "
                "and refresh their feedback.",
                "params": {
                    "value": {
                        "type": "integer",
                        "label": "Master Volume",
                        "min": 0,
                        "max": 100,
                        "required": True,
                    }
                },
                "sets": {"master_volume": "{value}"},
            },
            "set_master_mute": {
                "label": "Set Master Mute",
                "help": "Change master mute for the outputs assigned to the master and "
                "refresh their feedback.",
                "params": {
                    "value": {
                        "type": "boolean",
                        "label": "Master Mute",
                        "required": True,
                    }
                },
                "sets": {"master_mute": "{value}"},
            },
            "set_master_members": {
                "label": "Set Master Output Group",
                "help": "Enter one digit per output in jack order. 1 includes that "
                "output in Master Out and 0 excludes it. The bridge must "
                "first report a mask matching its output count.",
                "params": {
                    "mask": {"type": "string", "label": "Output Mask", "required": True}
                },
                "sets": {"member_mask": "{mask}"},
            },
            "recall_scene": {
                "label": "Recall Scene",
                "help": "Load a previously saved scene and read the new channel and master settings.",
                "params": {
                    "scene": {
                        "type": "child_id",
                        "label": "Scene",
                        "child_type": "scene",
                        "required": True,
                    }
                },
            },
            "save_scene": {
                "label": "Save Scene",
                "help": "Save the bridge’s current settings in the selected slot.",
                "params": {
                    "scene": {
                        "type": "child_id",
                        "label": "Scene",
                        "child_type": "scene",
                        "required": True,
                    }
                },
                "confirm": "Overwrite this saved Mineola scene?",
            },
            "clear_scene": {
                "label": "Clear Scene",
                "help": "Erase the selected scene slot.",
                "params": {
                    "scene": {
                        "type": "child_id",
                        "label": "Scene",
                        "child_type": "scene",
                        "required": True,
                    }
                },
                "confirm": "Clear this saved Mineola scene?",
            },
            "set_band_enabled": {
                "label": "Set EQ Band Enabled",
                "help": "Enable or bypass the selected channel’s filter and read its "
                "settings.",
                "params": {
                    "eq_band": {
                        "type": "child_id",
                        "label": "Eq Band",
                        "child_type": "eq_band",
                        "required": True,
                    },
                    "value": {"type": "boolean", "label": "Enabled", "required": True},
                },
                "sets": {"enabled": "{value}"},
            },
            "configure_band": {
                "label": "Configure EQ Band",
                "help": "Apply the filter type, frequency, gain and Q to this band, then "
                "read the equalizer back.",
                "params": {
                    "eq_band": {
                        "type": "child_id",
                        "label": "Eq Band",
                        "child_type": "eq_band",
                        "required": True,
                    },
                    "filter_type": {
                        "type": "enum",
                        "label": "Filter Type",
                        "values": [
                            "Parametric",
                            "Lowpass",
                            "Highpass",
                            "Low Shelf",
                            "High Shelf",
                        ],
                        "required": True,
                    },
                    "frequency": {
                        "type": "number",
                        "label": "Frequency",
                        "min": 20,
                        "max": 20000,
                        "required": True,
                    },
                    "gain": {
                        "type": "number",
                        "label": "Filter Gain",
                        "min": -15,
                        "max": 15,
                        "required": True,
                    },
                    "q": {
                        "type": "number",
                        "label": "Q",
                        "min": 0.02,
                        "max": 16,
                        "required": True,
                    },
                },
                "sets": {
                    "filter_type": "{filter_type}",
                    "frequency": "{frequency}",
                    "gain": "{gain}",
                    "q": "{q}",
                },
            },
        },
        "quick_actions": [
            "refresh",
            "set_output_volume",
            "set_input_mute",
            "recall_scene",
        ],
    }

    def __init__(self, device_id, config, state, events):
        super().__init__(
            device_id, {**self.DRIVER_INFO["default_config"], **config}, state, events
        )
        self._lock = asyncio.Lock()
        self._pending = None
        self._roster = {}
        self._opened = deque()

    def _transport_kwargs(self, transport_type, kwargs):
        kwargs.update(delimiter=b"\n", frame_parser=None)
        return kwargs

    async def _send(self, line):
        await self.transport.send((line + "\r\n").encode("ascii"))

    async def _request(self, line, parse, timeout=TIMEOUT):
        future = asyncio.get_running_loop().create_future()
        self._pending = (parse, future)
        try:
            await self._send(line)
            return await asyncio.wait_for(future, timeout)
        finally:
            if self._pending and self._pending[1] is future:
                self._pending = None

    def on_data_received(self, data):
        if not self._pending:
            return
        parse, future = self._pending
        if future.done():
            return
        try:
            result = parse(data.decode("utf-8", errors="replace").strip())
        except (ValueError, StopIteration):
            future.set_exception(
                ValueError("Incomplete or invalid reply from the Mineola bridge.")
            )
            return
        if result is not None:
            future.set_result(result)

    def _empty_children(self, kind):
        fields = self.DRIVER_INFO["child_entity_types"][kind]["state_variables"]
        for ident in self.list_children(kind):
            self.set_child_state_batch(kind, ident, dict.fromkeys(fields))

    def _expire(self):
        self._roster = {}
        self.set_states(dict.fromkeys(self.DRIVER_INFO["state_variables"]))
        for kind in ("input", "output", "eq_band"):
            self._empty_children(kind)

    async def _close_session(self):
        if self._pending and not self._pending[1].done():
            self._pending[1].set_exception(
                ConnectionError("Mineola control connection closed.")
            )
        self._pending = None
        self._expire()
        await super()._close_session()

    def _channel(self, kind, ident):
        number = checked_number(ident, 1, 16)
        if number not in self._roster.get(kind, {}):
            raise CommandRefusedError(
                "Choose a channel from the bridge’s latest complete status report."
            )
        return number

    def _label(self, kind, number, name):
        # Preserve the label commissioned in the project. A device rename updates
        # the default label, and always updates the separate device_name reading.
        key = self.format_child_id(kind, number)
        entry = self._project_child_entities.get(kind, {}).get(key, {})
        return entry.get("label") or name

    def _publish_channels(self, kind, rows):
        counts = Counter(row["device_name"].casefold() for row in rows.values())
        for number, values in rows.items():
            self.register_child(kind, number)
            name = values["device_name"]
            if counts[name.casefold()] > 1:
                name = f"{name} ({kind.title()} {number})"
            label = self._label(kind, number, name)
            self.set_child_state_batch(kind, number, {**values, "label": label})
        for old in set(self.list_children(kind)) - rows.keys():
            self.deregister_child(kind, old)

    def _prune_eq(self):
        removed = [(kind, n) for kind, n in self._opened if n not in self._roster[kind]]
        for kind, n in removed:
            self._opened.remove((kind, n))
            for b in range(1, 9):
                key = band_id(kind, n, b)
                if self.is_child_registered("eq_band", key):
                    self.deregister_child("eq_band", key)

    async def _status(self):
        try:
            system, self._roster = await self._request("get status", StatusReply())
        except (ValueError, TimeoutError, ConnectionError):
            self._expire()
            raise
        self.set_states(system)
        for kind, rows in self._roster.items():
            self._publish_channels(kind, rows)
        self._prune_eq()

    async def _post_connect(self):
        try:
            await self._status()
        except (TimeoutError, ValueError) as exc:
            raise ConnectionError(
                "No complete Mineola input/output table on the configured TCP port."
            ) from exc
        for number in range(1, 6):
            self.register_child(
                "scene",
                number,
                {"label": self._label("scene", number, f"Scene {number}")},
            )

    async def _initial_sync(self):
        async with self._lock:
            await self._masters()

    def _publish_mask(self, mask):
        outputs = self._roster.get("output", {})
        valid = mask is not None and len(mask) == len(outputs)
        for number in outputs:
            self.set_child_state(
                "output",
                number,
                "master_member",
                mask[number - 1] == "1" if valid else None,
            )

    async def _master(self, prop):
        token, parser = MASTER[prop]
        try:
            value = await self._request(f"get master {token}", parser, 1.25)
        except (TimeoutError, ValueError):
            value = None
        self.set_state(prop, value)
        if prop == "member_mask":
            self._publish_mask(value)
        return value

    async def _masters(self):
        for prop in MASTER:
            await self._master(prop)

    async def _refresh(self):
        await self._status()
        await self._masters()

    async def poll(self):
        async with self._lock:
            await self._refresh()
            if self._opened:
                kind, n = self._opened[0]
                self._opened.rotate(-1)
                await self._eq(kind, n)

    async def refresh_children(self):
        await self.poll()
        return {kind: len(rows) for kind, rows in self._roster.items()}

    def _labeled_channels(self):
        for side, rows in self._roster.items():
            prefix = "IN" if side == "input" else "OUT"
            for number, row in rows.items():
                yield (side, number), (f"XLR {prefix}{number}", row["device_name"])

    def _eq_names(self, kind, n):
        # A renamed channel can match the fixed label of a different jack.
        labels = {}
        for owner, names in self._labeled_channels():
            for name in names:
                labels.setdefault(name.casefold(), set()).add(owner)
        return [key for key, owners in labels.items() if owners == {(kind, n)}]

    async def _optional(self, line, parser):
        try:
            return await self._request(line, parser, 1.25)
        except (ValueError, TimeoutError):
            return None

    def _publish_eq(self, kind, n, bands):
        empty = dict.fromkeys(
            self.DRIVER_INFO["child_entity_types"]["eq_band"]["state_variables"]
        )
        for band in range(1, 9):
            ident = band_id(kind, n, band)
            if bands:
                self.register_child("eq_band", ident)
                name = self._label(kind, n, self._roster[kind][n]["device_name"])
                label = f"{kind.title()} {n} · {name} · Band {band}"
                self.set_child_state_batch(
                    "eq_band",
                    ident,
                    {**bands[band], "label": self._label("eq_band", ident, label)},
                )
            elif self.is_child_registered("eq_band", ident):
                self.set_child_state_batch("eq_band", ident, empty)

    async def _eq(self, kind, n):
        names = self._eq_names(kind, n)
        if (kind, n) not in self._opened:
            self._opened.append((kind, n))
        prefix = "|".join(re.escape(name) for name in names)
        preset = await self._optional(
            f"get {kind} {n} eq preset",
            match_reply(
                rf"(?:{prefix})\s+PEQ:\s*(Flat|Custom1|Custom2)", lambda v: v.title()
            ),
        )
        self.set_child_state(kind, n, "eq_preset", preset)
        bands = await self._optional(
            f"get {kind} {n} eq setting", EqualizerReply(names)
        )
        self._publish_eq(kind, n, bands)
        return preset, bands

    def _clear_eq(self):
        for kind, n in self._opened:
            self.set_child_state(kind, n, "eq_preset", None)
        self._empty_children("eq_band")

    async def _channel_command(self, command, params):
        action, kind, *rest = command.split("_")
        n = self._channel(kind, params.get(kind))
        prop = "_".join(rest)
        if action == "read":
            return await self._read_channel(kind, n, prop)
        wire, expected = as_wire(prop, params.get("value"))
        await self._send(f"set {kind} {n} {TOKENS[prop]} {wire}")
        if prop == "eq_preset":
            actual, _ = await self._eq(kind, n)
        else:
            await self._status()
            actual = self._roster[kind].get(n, {}).get(prop)
        verify(actual, expected)

    async def _read_channel(self, kind, n, prop):
        if prop != "eq":
            await self._status()
            self._channel(kind, n)
            return
        preset, bands = await self._eq(kind, n)
        if preset is None or not bands:
            raise CommandRefusedError(
                "The bridge did not report all EQ details for this channel."
            )

    async def _set_master(self, command, params):
        prop = command.removeprefix("set_")
        if prop == "master_members":
            mask = params.get("mask", "")
            reported = await self._master("member_mask")
            width = len(self._roster.get("output", {}))
            current = reported or ""
            if len(current) != width or not re.fullmatch(
                rf"[01]{{{width}}}", str(mask)
            ):
                raise CommandRefusedError(
                    "The reported master mask and the new mask must match the output count."
                )
            wire, expected, prop = f"<{mask}>", mask, "member_mask"
        else:
            wire, expected = as_wire(prop.removeprefix("master_"), params.get("value"))
        await self._send(f"set master {MASTER[prop][0]} {wire}")
        await self._status()
        verify(await self._master(prop), expected)

    async def _scene(self, command, params):
        slot = checked_number(params.get("scene"), 1, 5)
        action = command.split("_")[0]
        ack = {
            "save": "Save to preset",
            "clear": "Clear preset",
            "recall": "Recall preset",
        }[action]
        try:
            await self._request(
                f"set preset {action} {slot}", match_reply(rf"{ack}\s+({slot})", int)
            )
        finally:
            if action == "recall" and self._roster:
                self.set_state("last_recalled", None)
                self._clear_eq()
                await self._refresh()
        if action == "recall":
            self.set_state("last_recalled", slot)

    def _band_target(self, params):
        target = str(params.get("eq_band", ""))
        if not self.is_child_registered("eq_band", target):
            raise CommandRefusedError(
                "Read the channel EQ first, then choose one of its eight bands."
            )
        kind, number, band = target.split("-")
        return kind, self._channel(kind, number), int(band)

    async def _set_band(self, command, params):
        kind, n, band = self._band_target(params)
        if command == "set_band_enabled":
            wire, value = as_wire("mute", params.get("value"))
            expected = {"enabled": value}
        else:
            typ = params.get("filter_type")
            freq = checked_number(params.get("frequency"), 20, 20000, 0.1)
            gain = checked_number(params.get("gain"), -15, 15, 0.1)
            q = checked_number(params.get("q"), 0.02, 16, 0.01)
            wire = f"typ {FILTERS.index(typ) + 1} frq {freq:g} val {gain:g} q {q:g}"
            expected = dict(filter_type=typ, frequency=freq, gain=gain, q=q)
        await self._send(f"set {kind} {n} eq {band} {wire}")
        _, bands = await self._eq(kind, n)
        values = (bands or {}).get(band, {})
        verify({key: values.get(key) for key in expected}, expected)

    async def _operate(self, command, params):
        if command == "refresh":
            return await self._refresh()
        if not self._roster:
            await self._status()
        if command.endswith("_scene"):
            return await self._scene(command, params)
        if command.startswith("set_master_"):
            return await self._set_master(command, params)
        if command in ("configure_band", "set_band_enabled"):
            return await self._set_band(command, params)
        return await self._channel_command(command, params)

    async def send_command(self, command, params=None):
        if command not in self.DRIVER_INFO["commands"]:
            raise CommandRefusedError("Unknown Mineola command.")
        async with self._lock:
            try:
                await self._operate(command, params or {})
            except (ValueError, TimeoutError) as exc:
                raise CommandRefusedError(
                    str(exc) or "The bridge did not confirm the command in time."
                ) from exc
        return True
