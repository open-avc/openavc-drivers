"""Allen & Heath AHM audio matrices over the rear Network socket.

Python assembles MIDI running status, interleaved NRPN messages and variable-length
SysEx source lists on one stream. A TLS login precedes the MIDI exchange.
"""

from __future__ import annotations

import asyncio
import json

from openavc.core.device_traffic import get_traffic_recorder
from openavc.drivers.base import BaseDriver, CommandRefusedError, ConnectionFaultError

HEADER = bytes.fromhex("F0 00 00 1A 50 12 01 00")
KINDS = ("input", "zone", "group", "room")
COLOURS = ("Off", "Red", "Green", "Yellow", "Blue", "Magenta", "Cyan", "White")
PARAMS = {"level": 0x17, "trim": 0x18, "gain": 0x19, "pad": 0x1A, "phantom": 0x1B}
PROPS = {number: name for name, number in PARAMS.items()}
SWITCHES = {"muted", "pad", "phantom"}
MISSING = object()
PLAYERS = ("Mono 1", "Mono 2", "Stereo")
MODEL_SIZE = {"AHM-16": 16, "AHM-32": 32, "AHM-64": 64}


def sysex(*data):
    return HEADER + bytes(data) + b"\xf7"


def bounded(value, low, high):
    if isinstance(value, bool) or not str(value).isdecimal():
        raise CommandRefusedError(f"Choose a whole number from {low} to {high}.")
    number = int(value)
    if not low <= number <= high:
        raise CommandRefusedError(f"Choose a value from {low} to {high}.")
    return number


def switch(value):
    if isinstance(value, bool):
        return value
    if str(value).lower() in ("1", "true", "on"):
        return True
    if str(value).lower() in ("0", "false", "off"):
        return False
    raise CommandRefusedError("Choose on or off.")


def colour(code):
    return COLOURS[code] if code < len(COLOURS) else None


def name_bytes(data):
    return data.rstrip(b"\x00").decode("ascii", "replace").strip()


def get_channel(n, ch, prop):
    if prop == "muted":
        return sysex(n, 1, 9, ch)
    return sysex(n, 1, 11, PARAMS[prop], ch)


def set_channel(n, ch, prop, value):
    if prop == "muted":
        return bytes((0x90 + n, ch, 127 if value else 63, 0x90 + n, ch, 0))
    raw = (127 if value else 0) if prop in SWITCHES else value
    return bytes((0xB0 + n, 99, ch, 0xB0 + n, 98, PARAMS[prop], 0xB0 + n, 6, raw))


def printable_name(data):
    return all(32 <= code < 127 for code in data)


def selector_labels(data, count):
    colours = data[:count]
    names = data[count:].split(b"\x00")
    if max(colours, default=0) > 7:
        return None
    if len(names) != count + 1 or names[-1]:
        return None
    if not all(printable_name(item) for item in names[:-1]):
        return None
    return [(name_bytes(item), colour(code)) for item, code in zip(names, colours)]


def source_list(data):
    """The counted snapshot and the single-source change share opcode 08."""
    if len(data) < 2:
        return None
    count, selected = data[:2]
    if count > 20 or len(data) < 2 + count * 2:
        return None
    entries = selector_labels(data[2:], count)
    return None if entries is None else (selected, entries)


def login_bytes(config):
    try:
        profile = bounded(config.get("profile", 0), 0, 31)
        password = (config.get("password") or "").encode("ascii")
        if b"\x00" in password:
            raise ValueError
    except (ValueError, CommandRefusedError) as exc:
        raise ConnectionFaultError(
            "Use profile 0–31 and an ASCII password without a null character.",
            code="invalid_config",
        ) from exc
    return bytes((profile,)) + (password or b"\x00")


def favorite_presets(rows):
    choices = {}
    for row in rows:
        number = bounded(row["number"], 1, 500)
        choices[number] = row.get("name") or f"Preset {number}"
    return json.dumps([{"value": n, "label": label} for n, label in choices.items()])


def selector_change(payload):
    return len(payload) >= 2 and payload[0] < 20 and payload[1] < 8


class MidiStream:
    """One MIDI stream. Realtime bytes may interrupt any other message."""

    def __init__(self):
        self.status = 0
        self.data = bytearray()

    def feed(self, chunk):
        for byte in chunk:
            frame = self._byte(byte)
            if frame is not None:
                yield frame

    def _byte(self, byte):
        if byte >= 0xF8:
            return None
        if byte >= 0x80:
            return self._status(byte)
        if not self.status:
            return None
        self.data.append(byte)
        if self.status == 0xF0:
            if len(self.data) > 4096:
                self._status(0xF7)
            return None
        size = 1 if self.status >> 4 in (12, 13) else 2
        if len(self.data) == size:
            frame = bytes((self.status,)) + self.data
            self.data.clear()
            return frame
        return None

    def _status(self, byte):
        frame = None
        if byte == 0xF7 and self.status == 0xF0:
            frame = b"\xf0" + self.data + b"\xf7"
        self.data.clear()
        self.status = byte if byte <= 0xF0 else 0
        return frame


class AllenHeathAHM(BaseDriver):
    DRIVER_INFO = {
        "id": "allenheath_ahm",
        "name": "Allen & Heath AHM",
        "manufacturer": "Allen & Heath",
        "category": "audio",
        "version": "1.0.0",
        "min_platform_version": "0.37.2",
        "author": "OpenAVC",
        "source_url": "https://www.allen-heath.com/content/uploads/2024/10/AHM-TCP-Protocol-V1.5.pdf",
        "description": "Control an AHM-16, AHM-32 or AHM-64 audio matrix by zone, channel and source name. Includes room "
        "combining, presets and selectable audio sends.",
        "verified": False,
        "simulated": True,
        "compatible_models": [
            {
                "manufacturer": "Allen & Heath",
                "models": ["AHM-16", "AHM-32", "AHM-64"],
                "confidence": "untested",
                "notes": "Based on TCP/IP Protocol V1.5 and checked in simulation. Physical verification "
                "pending.",
            }
        ],
        "transport": "tcp",
        "ports": [51327, 51325],
        "protocols": ["midi-over-tcp"],
        "tags": ["dsp", "audio-matrix", "zone-control", "room-combining"],
        "discovery": {
            "tcp_probe": {
                "port": 51325,
                "send_hex": "F0 00 00 1A 50 12 01 00 00 09 00 F7",
                "expect_hex": "F0 00 00 1A 50 12 01 00 00 0A 00",
                "timeout_ms": 1500,
            },
            "port_open": [51325, 51327],
            "oui": ["00:04:c4"],
            "manufacturer_alias": ["Allen & Heath", "Audiotonix"],
        },
        "default_config": {
            "host": "",
            "port": 51327,
            "ssl": True,
            "verify_ssl": True,
            "model": "AHM-16",
            "profile": 0,
            "poll_interval": 0,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "label": "AHM network address",
                "required": True,
            },
            "model": {
                "type": "enum",
                "label": "Processor model",
                "values": ["AHM-16", "AHM-32", "AHM-64"],
                "default": "AHM-16",
                "description": "Choose the model printed on the processor. This sets its input and "
                "zone count.",
            },
            "ssl": {
                "type": "boolean",
                "label": "Secure connection (TLS)",
                "default": True,
                "description": "Match External Control security in AHM System Manager. The driver uses "
                "51327 for SSL and 51325 for No Security.",
            },
            "verify_ssl": {
                "type": "boolean",
                "label": "Verify the AHM certificate",
                "default": True,
                "description": "Use a trusted certificate and its matching host name. Turn off "
                "only for a known processor with a private certificate.",
            },
            "port": {
                "type": "integer",
                "label": "TCP port",
                "min": 1,
                "max": 65535,
                "default": 51327,
                "description": "Leave at the default to follow the security setting. Enter another "
                "port for a forwarded connection.",
            },
            "profile": {
                "type": "integer",
                "label": "Login profile number",
                "min": 0,
                "max": 31,
                "default": 0,
                "description": "Admin is 0. For User 1 enter 1. Used only with SSL.",
            },
            "password": {
                "type": "string",
                "label": "Profile password",
                "secret": True,
                "description": "Leave empty for a profile without a password. Use a profile with "
                "an ASCII password.",
            },
            "sends": {
                "type": "table",
                "label": "Named audio sends",
                "row_label": "send",
                "columns": {
                    "source_type": {
                        "type": "enum",
                        "label": "From",
                        "values": ["Input", "Zone"],
                        "required": True,
                    },
                    "source": {
                        "type": "integer",
                        "label": "Source number",
                        "min": 1,
                        "max": 64,
                        "required": True,
                    },
                    "zone": {
                        "type": "integer",
                        "label": "Destination zone",
                        "min": 1,
                        "max": 64,
                        "required": True,
                    },
                    "name": {"type": "string", "label": "Display name"},
                },
                "description": "Add the input-to-zone or zone-to-zone crosspoints you want to "
                "control. The numbers match System Manager.",
            },
            "presets": {
                "type": "table",
                "label": "Favorite presets",
                "row_label": "preset",
                "columns": {
                    "number": {
                        "type": "integer",
                        "label": "Preset number",
                        "min": 1,
                        "max": 500,
                        "required": True,
                    },
                    "name": {
                        "type": "string",
                        "label": "Display name",
                        "required": True,
                    },
                },
                "description": "Give commonly used preset numbers names for the Recall preset "
                "picker. Any preset from 1 to 500 can also be entered.",
            },
        },
        "help": {
            "overview": "Control the channels and zones designed in AHM System Manager. A zone always controls that "
            "zone. A combiner room follows the primary room when spaces are combined.",
            "setup": "Choose the processor model and enter its Network socket address. Match the External Control "
            "mode in System Manager. For SSL, enter the profile number and password. Refresh controls "
            "after changing the AHM project. Add favorite presets or named audio sends only if you use "
            "them.",
            "connection": "Bind a zone fader to Fader position and a mute button to Muted. Select a named zone "
            "source lists each source with its zone. A source binding keeps its zone and slot number "
            "if the label changes. Preset, combiner source and divider indicators start unknown until "
            "the processor reports a change. A fader position is a native step, with 0 at silence and "
            "127 at +10 dB.",
        },
        "state_variables": {
            "last_preset": {
                "type": "integer",
                "label": "Last observed preset",
                "min": 1,
                "max": 500,
                "help": "Unknown until a recall is received on this connection.",
            },
            "preset_choices": {"type": "string", "label": "Favorite preset choices"},
        },
        "child_entity_types": {
            "zone": {
                "label": "Zone",
                "label_plural": "Zones",
                "id_format": {"type": "integer", "min": 1, "max": 64},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Zone name"},
                    "level": {
                        "type": "integer",
                        "label": "Fader position",
                        "min": 0,
                        "max": 127,
                        "step": 1,
                        "control": True,
                        "help": "All 128 AHM fader positions. 0 is "
                        "silence, 105 is nominal and 127 is "
                        "maximum.",
                    },
                    "muted": {
                        "type": "boolean",
                        "label": "Muted",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "colour": {
                        "type": "string",
                        "label": "Channel colour",
                        "cloud_priority": "low",
                    },
                    "source": {
                        "type": "integer",
                        "label": "Selected source number",
                        "min": 1,
                        "max": 20,
                        "control": True,
                    },
                    "source_name": {"type": "string", "label": "Selected source name"},
                    "source_colour": {
                        "type": "string",
                        "label": "Selected source colour",
                    },
                },
                "summary_fields": ["muted", "level", "source_name"],
            },
            "input": {
                "label": "Input",
                "label_plural": "Inputs",
                "id_format": {"type": "integer", "min": 1, "max": 64},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Input name"},
                    "level": {
                        "type": "integer",
                        "label": "Fader position",
                        "min": 0,
                        "max": 127,
                        "step": 1,
                        "control": True,
                        "help": "All 128 AHM fader positions. 0 is "
                        "silence, 105 is nominal and 127 is "
                        "maximum.",
                    },
                    "muted": {
                        "type": "boolean",
                        "label": "Muted",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "colour": {
                        "type": "string",
                        "label": "Channel colour",
                        "cloud_priority": "low",
                    },
                    "trim": {
                        "type": "integer",
                        "label": "Digital trim position",
                        "min": 0,
                        "max": 127,
                        "step": 1,
                    },
                    "gain": {
                        "type": "integer",
                        "label": "Preamp gain position",
                        "min": 0,
                        "max": 127,
                        "step": 1,
                    },
                    "pad": {"type": "boolean", "label": "Preamp pad"},
                    "phantom": {"type": "boolean", "label": "48 V phantom power"},
                },
                "summary_fields": ["muted", "level"],
            },
            "group": {
                "label": "Control group",
                "label_plural": "Control groups",
                "id_format": {"type": "integer", "min": 1, "max": 32},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Control group name"},
                    "level": {
                        "type": "integer",
                        "label": "Fader position",
                        "min": 0,
                        "max": 127,
                        "step": 1,
                        "control": True,
                        "help": "All 128 AHM fader positions. 0 is "
                        "silence, 105 is nominal and 127 is "
                        "maximum.",
                    },
                    "muted": {
                        "type": "boolean",
                        "label": "Muted",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "colour": {
                        "type": "string",
                        "label": "Channel colour",
                        "cloud_priority": "low",
                    },
                },
                "summary_fields": ["muted", "level"],
            },
            "room": {
                "label": "Combiner room",
                "label_plural": "Combiner rooms",
                "id_format": {"type": "integer", "min": 1, "max": 16},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Combiner room name"},
                    "level": {
                        "type": "integer",
                        "label": "Fader position",
                        "min": 0,
                        "max": 127,
                        "step": 1,
                        "control": True,
                        "help": "All 128 AHM fader positions. 0 is "
                        "silence, 105 is nominal and 127 is "
                        "maximum.",
                    },
                    "muted": {
                        "type": "boolean",
                        "label": "Muted",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "colour": {
                        "type": "string",
                        "label": "Channel colour",
                        "cloud_priority": "low",
                    },
                    "source": {
                        "type": "integer",
                        "label": "Selected source number",
                        "min": 1,
                        "max": 20,
                        "control": True,
                    },
                    "source_name": {"type": "string", "label": "Selected source name"},
                    "source_colour": {
                        "type": "string",
                        "label": "Selected source colour",
                    },
                },
                "summary_fields": ["muted", "level", "source_name"],
            },
            "send": {
                "label": "Audio send",
                "label_plural": "Audio sends",
                "id_format": {"type": "string", "max_length": 32},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Audio send name"},
                    "level": {
                        "type": "integer",
                        "label": "Fader position",
                        "min": 0,
                        "max": 127,
                        "step": 1,
                        "control": True,
                        "help": "All 128 AHM fader positions. 0 is "
                        "silence, 105 is nominal and 127 is "
                        "maximum.",
                    },
                    "muted": {
                        "type": "boolean",
                        "label": "Muted",
                        "control": True,
                        "cloud_priority": "high",
                    },
                },
                "summary_fields": ["muted", "level"],
            },
            "zone_source": {
                "label": "Zone source",
                "label_plural": "Zone sources",
                "id_format": {"type": "string", "max_length": 32},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Zone source name"},
                    "selected": {
                        "type": "boolean",
                        "label": "Selected in zone",
                        "control": True,
                    },
                    "colour": {"type": "string", "label": "Source colour"},
                },
                "summary_fields": ["selected"],
            },
            "divider": {
                "label": "Room divider",
                "label_plural": "Room dividers",
                "id_format": {"type": "string", "max_length": 32},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Room divider name"},
                    "combined": {
                        "type": "boolean",
                        "label": "Rooms combined",
                        "control": True,
                    },
                },
                "summary_fields": ["combined"],
            },
        },
        "commands": {
            "set_zone_level": {
                "label": "Set zone level",
                "help": "Set the selected zone fader position and read its actual value back.",
                "params": {
                    "zone": {
                        "type": "child_id",
                        "child_type": "zone",
                        "label": "Zone",
                        "required": True,
                    },
                    "level": {
                        "type": "integer",
                        "label": "Fader position",
                        "min": 0,
                        "max": 127,
                        "required": True,
                    },
                },
                "sets": {"level": "{level}"},
            },
            "read_zone_level": {
                "label": "Read zone level",
                "help": "Read the current fader position from the processor.",
                "params": {
                    "zone": {
                        "type": "child_id",
                        "child_type": "zone",
                        "label": "Zone",
                        "required": True,
                    }
                },
                "query_for": "level",
            },
            "set_zone_mute": {
                "label": "Set zone mute",
                "help": "Set the selected zone mute and read its actual value back.",
                "params": {
                    "zone": {
                        "type": "child_id",
                        "child_type": "zone",
                        "label": "Zone",
                        "required": True,
                    },
                    "muted": {"type": "boolean", "label": "Muted", "required": True},
                },
                "sets": {"muted": "{muted}"},
            },
            "read_zone_mute": {
                "label": "Read zone mute",
                "help": "Read the current mute from the processor.",
                "params": {
                    "zone": {
                        "type": "child_id",
                        "child_type": "zone",
                        "label": "Zone",
                        "required": True,
                    }
                },
                "query_for": "muted",
            },
            "set_group_level": {
                "label": "Set control group level",
                "help": "Set the selected control group fader position and read its actual "
                "value back.",
                "params": {
                    "group": {
                        "type": "child_id",
                        "child_type": "group",
                        "label": "Control group",
                        "required": True,
                    },
                    "level": {
                        "type": "integer",
                        "label": "Fader position",
                        "min": 0,
                        "max": 127,
                        "required": True,
                    },
                },
                "sets": {"level": "{level}"},
            },
            "read_group_level": {
                "label": "Read control group level",
                "help": "Read the current fader position from the processor.",
                "params": {
                    "group": {
                        "type": "child_id",
                        "child_type": "group",
                        "label": "Control group",
                        "required": True,
                    }
                },
                "query_for": "level",
            },
            "set_group_mute": {
                "label": "Set control group mute",
                "help": "Set the selected control group mute and read its actual value back.",
                "params": {
                    "group": {
                        "type": "child_id",
                        "child_type": "group",
                        "label": "Control group",
                        "required": True,
                    },
                    "muted": {"type": "boolean", "label": "Muted", "required": True},
                },
                "sets": {"muted": "{muted}"},
            },
            "read_group_mute": {
                "label": "Read control group mute",
                "help": "Read the current mute from the processor.",
                "params": {
                    "group": {
                        "type": "child_id",
                        "child_type": "group",
                        "label": "Control group",
                        "required": True,
                    }
                },
                "query_for": "muted",
            },
            "set_input_level": {
                "label": "Set input level",
                "help": "Set the selected input fader position and read its actual value back.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "label": "Input",
                        "required": True,
                    },
                    "level": {
                        "type": "integer",
                        "label": "Fader position",
                        "min": 0,
                        "max": 127,
                        "required": True,
                    },
                },
                "sets": {"level": "{level}"},
            },
            "read_input_level": {
                "label": "Read input level",
                "help": "Read the current fader position from the processor.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "label": "Input",
                        "required": True,
                    }
                },
                "query_for": "level",
            },
            "set_input_mute": {
                "label": "Set input mute",
                "help": "Set the selected input mute and read its actual value back.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "label": "Input",
                        "required": True,
                    },
                    "muted": {"type": "boolean", "label": "Muted", "required": True},
                },
                "sets": {"muted": "{muted}"},
            },
            "read_input_mute": {
                "label": "Read input mute",
                "help": "Read the current mute from the processor.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "label": "Input",
                        "required": True,
                    }
                },
                "query_for": "muted",
            },
            "set_room_level": {
                "label": "Set combiner room level",
                "help": "Set the selected combiner room fader position and read its actual value "
                "back.",
                "params": {
                    "room": {
                        "type": "child_id",
                        "child_type": "room",
                        "label": "Combiner room",
                        "required": True,
                    },
                    "level": {
                        "type": "integer",
                        "label": "Fader position",
                        "min": 0,
                        "max": 127,
                        "required": True,
                    },
                },
                "sets": {"level": "{level}"},
            },
            "read_room_level": {
                "label": "Read combiner room level",
                "help": "Read the current fader position from the processor.",
                "params": {
                    "room": {
                        "type": "child_id",
                        "child_type": "room",
                        "label": "Combiner room",
                        "required": True,
                    }
                },
                "query_for": "level",
            },
            "set_room_mute": {
                "label": "Set combiner room mute",
                "help": "Set the selected combiner room mute and read its actual value back.",
                "params": {
                    "room": {
                        "type": "child_id",
                        "child_type": "room",
                        "label": "Combiner room",
                        "required": True,
                    },
                    "muted": {"type": "boolean", "label": "Muted", "required": True},
                },
                "sets": {"muted": "{muted}"},
            },
            "read_room_mute": {
                "label": "Read combiner room mute",
                "help": "Read the current mute from the processor.",
                "params": {
                    "room": {
                        "type": "child_id",
                        "child_type": "room",
                        "label": "Combiner room",
                        "required": True,
                    }
                },
                "query_for": "muted",
            },
            "set_send_level": {
                "label": "Set audio send level",
                "help": "Set the selected audio send fader position and read its actual value "
                "back.",
                "params": {
                    "send": {
                        "type": "child_id",
                        "child_type": "send",
                        "label": "Audio send",
                        "required": True,
                    },
                    "level": {
                        "type": "integer",
                        "label": "Fader position",
                        "min": 0,
                        "max": 127,
                        "required": True,
                    },
                },
                "sets": {"level": "{level}"},
            },
            "read_send_level": {
                "label": "Read audio send level",
                "help": "Read the current fader position from the processor.",
                "params": {
                    "send": {
                        "type": "child_id",
                        "child_type": "send",
                        "label": "Audio send",
                        "required": True,
                    }
                },
                "query_for": "level",
            },
            "set_send_mute": {
                "label": "Set audio send mute",
                "help": "Set the selected audio send mute and read its actual value back.",
                "params": {
                    "send": {
                        "type": "child_id",
                        "child_type": "send",
                        "label": "Audio send",
                        "required": True,
                    },
                    "muted": {"type": "boolean", "label": "Muted", "required": True},
                },
                "sets": {"muted": "{muted}"},
            },
            "read_send_mute": {
                "label": "Read audio send mute",
                "help": "Read the current mute from the processor.",
                "params": {
                    "send": {
                        "type": "child_id",
                        "child_type": "send",
                        "label": "Audio send",
                        "required": True,
                    }
                },
                "query_for": "muted",
            },
            "set_input_trim": {
                "label": "Set digital trim position",
                "help": "Change digital trim position for the selected input and read it back.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "label": "Input",
                        "required": True,
                    },
                    "trim": {
                        "type": "integer",
                        "label": "Digital trim position",
                        "min": 0,
                        "max": 127,
                        "required": True,
                    },
                },
                "sets": {"trim": "{trim}"},
            },
            "read_input_trim": {
                "label": "Read digital trim position",
                "help": "Read the current digital trim position for this input.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "label": "Input",
                        "required": True,
                    }
                },
                "query_for": "trim",
            },
            "set_input_gain": {
                "label": "Set preamp gain position",
                "help": "Change preamp gain position for the selected input and read it back.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "label": "Input",
                        "required": True,
                    },
                    "gain": {
                        "type": "integer",
                        "label": "Preamp gain position",
                        "min": 0,
                        "max": 127,
                        "required": True,
                    },
                },
                "sets": {"gain": "{gain}"},
            },
            "read_input_gain": {
                "label": "Read preamp gain position",
                "help": "Read the current preamp gain position for this input.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "label": "Input",
                        "required": True,
                    }
                },
                "query_for": "gain",
            },
            "set_input_pad": {
                "label": "Set preamp pad",
                "help": "Change preamp pad for the selected input and read it back.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "label": "Input",
                        "required": True,
                    },
                    "pad": {"type": "boolean", "label": "Preamp pad", "required": True},
                },
                "sets": {"pad": "{pad}"},
            },
            "read_input_pad": {
                "label": "Read preamp pad",
                "help": "Read the current preamp pad for this input.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "label": "Input",
                        "required": True,
                    }
                },
                "query_for": "pad",
            },
            "set_input_phantom": {
                "label": "Set 48 v phantom power",
                "help": "Change 48 v phantom power for the selected input and read it back.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "label": "Input",
                        "required": True,
                    },
                    "phantom": {
                        "type": "boolean",
                        "label": "48 V phantom power",
                        "required": True,
                    },
                },
                "sets": {"phantom": "{phantom}"},
                "confirm": "Changes 48 V phantom power on the patched microphone input.",
            },
            "read_input_phantom": {
                "label": "Read 48 v phantom power",
                "help": "Read the current 48 v phantom power for this input.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "label": "Input",
                        "required": True,
                    }
                },
                "query_for": "phantom",
            },
            "lower_send_level": {
                "label": "Lower audio send level one step",
                "help": "Move the selected audio send fader by one AHM step and read its new position.",
                "params": {
                    "send": {
                        "type": "child_id",
                        "label": "Audio send",
                        "child_type": "send",
                        "required": True,
                    }
                },
            },
            "raise_send_level": {
                "label": "Raise audio send level one step",
                "help": "Move the selected audio send fader by one AHM step and read its new position.",
                "params": {
                    "send": {
                        "type": "child_id",
                        "label": "Audio send",
                        "child_type": "send",
                        "required": True,
                    }
                },
            },
            "lower_group_level": {
                "label": "Lower control group level one step",
                "help": "Move the selected control group fader by one AHM step and read its new position.",
                "params": {
                    "group": {
                        "type": "child_id",
                        "label": "Control group",
                        "child_type": "group",
                        "required": True,
                    }
                },
            },
            "raise_group_level": {
                "label": "Raise control group level one step",
                "help": "Move the selected control group fader by one AHM step and read its new position.",
                "params": {
                    "group": {
                        "type": "child_id",
                        "label": "Control group",
                        "child_type": "group",
                        "required": True,
                    }
                },
            },
            "lower_input_level": {
                "label": "Lower input level one step",
                "help": "Move the selected input fader by one AHM step and read its new position.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "label": "Input",
                        "child_type": "input",
                        "required": True,
                    }
                },
            },
            "raise_input_level": {
                "label": "Raise input level one step",
                "help": "Move the selected input fader by one AHM step and read its new position.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "label": "Input",
                        "child_type": "input",
                        "required": True,
                    }
                },
            },
            "lower_room_level": {
                "label": "Lower combiner room level one step",
                "help": "Move the selected combiner room fader by one AHM step and read its new position.",
                "params": {
                    "room": {
                        "type": "child_id",
                        "label": "Combiner room",
                        "child_type": "room",
                        "required": True,
                    }
                },
            },
            "raise_room_level": {
                "label": "Raise combiner room level one step",
                "help": "Move the selected combiner room fader by one AHM step and read its new position.",
                "params": {
                    "room": {
                        "type": "child_id",
                        "label": "Combiner room",
                        "child_type": "room",
                        "required": True,
                    }
                },
            },
            "lower_zone_level": {
                "label": "Lower zone level one step",
                "help": "Move the selected zone fader by one AHM step and read its new position.",
                "params": {
                    "zone": {
                        "type": "child_id",
                        "label": "Zone",
                        "child_type": "zone",
                        "required": True,
                    }
                },
            },
            "raise_zone_level": {
                "label": "Raise zone level one step",
                "help": "Move the selected zone fader by one AHM step and read its new position.",
                "params": {
                    "zone": {
                        "type": "child_id",
                        "label": "Zone",
                        "child_type": "zone",
                        "required": True,
                    }
                },
            },
            "select_zone_source": {
                "label": "Select a named zone source",
                "help": "Choose the zone and source together using their names from the AHM "
                "project.",
                "params": {
                    "zone_source": {
                        "type": "child_id",
                        "child_type": "zone_source",
                        "label": "Zone source",
                        "required": True,
                    }
                },
                "sets": {"selected": True},
            },
            "set_zone_source": {
                "label": "Select zone source by number",
                "help": "Choose a zone and the source position in its selector.",
                "params": {
                    "zone": {
                        "type": "child_id",
                        "child_type": "zone",
                        "label": "Zone",
                        "required": True,
                    },
                    "source": {
                        "type": "integer",
                        "label": "Source number",
                        "min": 1,
                        "max": 20,
                        "required": True,
                    },
                },
                "sets": {"source": "{source}"},
            },
            "read_zone_source": {
                "label": "Read zone source",
                "help": "Refresh this zone’s source choices and current selection.",
                "params": {
                    "zone": {
                        "type": "child_id",
                        "child_type": "zone",
                        "label": "Zone",
                        "required": True,
                    }
                },
                "query_for": "source",
            },
            "set_room_source": {
                "label": "Select combiner room source",
                "help": "Select a source position for the room following the current room "
                "combination. Feedback comes from the processor.",
                "params": {
                    "room": {
                        "type": "child_id",
                        "child_type": "room",
                        "label": "Combiner room",
                        "required": True,
                    },
                    "source": {
                        "type": "integer",
                        "label": "Source number",
                        "min": 1,
                        "max": 20,
                        "required": True,
                    },
                },
                "sets": {"source": "{source}"},
            },
            "set_divider": {
                "label": "Set room divider",
                "help": "Combine or divide two rooms assigned in System Manager. Status updates "
                "when the processor reports this pair.",
                "params": {
                    "room_a": {
                        "type": "child_id",
                        "child_type": "room",
                        "label": "First room",
                        "required": True,
                    },
                    "room_b": {
                        "type": "child_id",
                        "child_type": "room",
                        "label": "Second room",
                        "required": True,
                    },
                    "combined": {
                        "type": "boolean",
                        "label": "Combine rooms",
                        "required": True,
                    },
                },
            },
            "recall_preset": {
                "label": "Recall preset",
                "help": "Recall a saved processor preset. The last recalled number updates when "
                "the AHM reports a recall.",
                "params": {
                    "preset": {
                        "type": "integer",
                        "label": "Preset",
                        "min": 1,
                        "max": 500,
                        "required": True,
                        "options_state": "preset_choices",
                    }
                },
                "sets": {"last_preset": "{preset}"},
            },
            "play_track": {
                "label": "Play audio track",
                "help": "Start the chosen playback track on the AHM. Playback status is unavailable.",
                "params": {
                    "player": {
                        "type": "enum",
                        "label": "Playback channel",
                        "values": ["Mono 1", "Mono 2", "Stereo"],
                        "required": True,
                    },
                    "track": {
                        "type": "integer",
                        "label": "Track number",
                        "min": 1,
                        "max": 128,
                        "required": True,
                    },
                },
            },
            "refresh": {
                "label": "Refresh controls",
                "help": "Read channel names, faders, mutes, configured sends and zone source choices "
                "again after a project change.",
                "params": {},
            },
        },
        "quick_actions": [
            "select_zone_source",
            "set_zone_level",
            "set_zone_mute",
            "recall_preset",
            "refresh",
        ],
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stream = MidiStream()
        self._auth = None
        self._ack = bytearray()
        self._pending_key = None
        self._pending_value = MISSING
        self._fence = None
        self._commands = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        self._rebuild = None
        self._addresses = {}
        self._bank = None
        self._sources = {}
        self._sends = {}
        self._seen = set()

    def _resolve_delimiter(self):
        return None

    def _transport_kwargs(self, transport_type, kwargs):
        if not kwargs["ssl"] and kwargs["port"] == 51327:
            kwargs["port"] = 51325
        return kwargs

    def _bounds(self):
        model = self.config.get("model", "AHM-16")
        if model not in MODEL_SIZE:
            raise ConnectionFaultError(
                "Choose AHM-16, AHM-32 or AHM-64.", code="invalid_config"
            )
        count = MODEL_SIZE[model]
        return (count, count, 32, 16)

    def _load_config(self):
        sizes = self._bounds()
        self._sends.clear()
        try:
            choices = favorite_presets(self.config.get("presets") or ())
            for row in self.config.get("sends") or ():
                self._add_send(row, sizes)
        except (ValueError, KeyError, TypeError, CommandRefusedError) as exc:
            raise ConnectionFaultError(
                f"Check the named sends and favorite presets. {exc}",
                code="invalid_config",
            ) from exc
        self.set_state("preset_choices", choices)

        for key in self.list_children("send"):
            if key not in self._sends:
                self.deregister_child("send", key)

    def _add_send(self, row, sizes):
        n = {"Input": 0, "Zone": 1}[row["source_type"]]
        src = bounded(row["source"], 1, sizes[n])
        dst = bounded(row["zone"], 1, sizes[1])
        if n == 1 and src == dst:
            raise ValueError("A zone cannot feed itself.")
        key = f"{KINDS[n]}_{src}_zone_{dst}"
        self._sends[key] = (n, src - 1, 1, dst - 1)
        label = row.get("name") or f"{row['source_type']} {src} to Zone {dst}"
        self.register_child("send", key)
        self.set_child_state("send", key, "name", label)

    async def _post_connect(self):
        self._stream = MidiStream()
        self._bank = None
        self._addresses.clear()
        self._load_config()
        if self.config.get("ssl", False):
            await self._login()
        await self._liveness_probe()

    async def _login(self):
        packet = login_bytes(self.config)
        self._ack.clear()
        self._auth = asyncio.get_running_loop().create_future()
        try:
            with get_traffic_recorder().hold(self.device_id, tx=True):
                await self.transport.send(packet)
            try:
                accepted = await asyncio.wait_for(self._auth, 4)
            except TimeoutError as exc:
                raise ConnectionFaultError(
                    "The processor did not answer the secure login.", code="no_response"
                ) from exc
            if not accepted:
                raise ConnectionFaultError(
                    "The processor declined the AHM profile or password.",
                    code="auth_failed",
                )
        finally:
            self._auth = None

    def _handle_transport_disconnect(self):
        if self._auth is not None and not self._auth.done():
            self._auth.set_result(False)
            return  # _post_connect classifies the documented close on a refused login.
        super()._handle_transport_disconnect()

    def on_data_received(self, data):
        if self._auth is not None and not self._auth.done():
            self._ack.extend(data)
            if b"AuthOK".startswith(self._ack) and len(self._ack) < 6:
                return
            self._auth.set_result(self._ack.startswith(b"AuthOK"))
            data = bytes(self._ack[6:]) if self._auth.result() else b""
        for frame in self._stream.feed(data):
            self._frame(frame)

    def _frame(self, frame):
        status = frame[0]
        if status == 0xF0:
            self._system_frame(frame)
            return
        n = status & 15
        if n >= len(KINDS):
            return
        family = status >> 4
        if family == 9:
            self._mute_note(n, frame[1], frame[2])
        elif family == 11:
            self._controller(n, frame[1], frame[2])
        elif status == 0xC0:
            self._program(frame[1])

    def _system_frame(self, frame):
        if frame.startswith(HEADER):
            self._sysex(frame[len(HEADER) : -1])

    def _mute_note(self, n, ch, velocity):
        if velocity:
            self._channel(n, ch, {"muted": velocity >= 64})

    def _program(self, slot):
        if self._bank is not None:
            self._preset(self._bank * 128 + slot + 1)

    def _controller(self, n, controller, value):
        if controller == 0 and n == 0:
            self._bank = value if value < 4 else None
            return
        address = self._addresses.setdefault(n, {})
        if controller in (99, 98):
            address[controller] = value
        elif controller == 6:
            self._parameter(n, address, value)

    def _parameter(self, n, address, value):
        prop = PROPS.get(address.get(98))
        if 99 not in address or not prop:
            return
        if n and prop != "level":
            return
        self._channel(
            n, address[99], {prop: value >= 64 if prop in SWITCHES else value}
        )

    def _preset(self, number):
        if number > 500:
            return
        self.set_state("last_preset", number)
        self._schedule_refresh()

    def _schedule_refresh(self):
        if not self.connected:
            return
        self._refresh_again = True
        if self._rebuild is None or self._rebuild.done():
            self._rebuild = asyncio.create_task(self._after_recall())

    async def _after_recall(self):
        try:
            while self._refresh_again:
                self._refresh_again = False
                await self.refresh_children()
        except (ConnectionError, OSError, CommandRefusedError):
            return  # Reconnect performs its own sync.

    def _sysex(self, data):
        if len(data) < 3:
            return
        n, op, ch = data[:3]
        if op in (2, 3):
            if len(data) == 6:
                self._send_feedback(data)
        elif op in (10, 12):
            self._description(n, op, ch, data[3:])
        elif n == 0:
            self._room_feedback(op, ch, data[3:])

    def _description(self, n, op, ch, payload):
        if n >= len(KINDS):
            return
        if op == 10:
            name = name_bytes(payload) or f"{KINDS[n].title()} {ch + 1}"
            self._channel(n, ch, {"name": name})
            if n == 1 and ch + 1 in self._sources:
                self._rename_sources(ch + 1)
        elif len(payload) == 1:
            self._channel(n, ch, {"colour": colour(payload[0])})

    def _room_feedback(self, op, ch, payload):
        if op in (8, 13):
            self._selector(op, ch, payload)
        elif op == 14 and len(payload) == 2:
            self._divider(ch, *payload)

    def _channel(self, n, ch, values):
        if ch >= self._bounds()[n]:
            return
        self._publish(KINDS[n], ch + 1, values)

    def _publish(self, kind, ident, values):
        self.register_child(kind, ident)
        self._seen.add((kind, ident))
        self.set_child_state_batch(kind, ident, {**values, "online": True})
        self._capture(kind, ident, values)

    def _capture(self, kind, ident, values):
        if self._fence is None or self._fence.done():
            return
        for prop, value in values.items():
            if (kind, ident, prop) == self._pending_key:
                self._pending_value = value
        if (kind, ident) == ("input", 1) and "colour" in values:
            self._fence.set_result(self._pending_value)

    def _send_feedback(self, data):
        n, op, ch, target, dst, raw = data
        if n > 1 or target != 1:
            return
        key = f"{KINDS[n]}_{ch + 1}_zone_{dst + 1}"
        if key in self._sends:
            self._publish(
                "send", key, {"level": raw} if op == 2 else {"muted": raw >= 64}
            )

    def _selector(self, op, ch, payload):
        kind = "zone" if op == 8 else "room"
        if ch >= self._bounds()[KINDS.index(kind)]:
            return
        snapshot = source_list(payload) if op == 8 else None
        if snapshot is not None:
            self._zone_snapshot(ch, *snapshot)
        elif selector_change(payload):
            self._selection_event(kind, ch + 1, payload)

    def _zone_snapshot(self, ch, selected, entries):
        zone = ch + 1
        self._source_roster(zone, entries, selected)
        valid = selected < len(entries)
        name, tint = entries[selected] if valid else (None, None)
        self._channel(
            1,
            ch,
            {
                "source": selected + 1 if valid else None,
                "source_name": name,
                "source_colour": tint,
            },
        )

    def _selection_event(self, kind, ident, payload):
        slot, code = payload[:2]
        name = name_bytes(payload[2:])
        self._publish(
            kind,
            ident,
            {"source": slot + 1, "source_name": name, "source_colour": colour(code)},
        )
        if kind == "zone":
            self._source_change(ident, slot, name, colour(code))

    def _rename_sources(self, zone):
        selected = self.get_child_state("zone", zone).get("source") or 0
        self._source_roster(zone, self._sources[zone], selected - 1)

    def _source_change(self, zone, selected, name, tint):
        entries = self._sources.get(zone, [])
        if selected < len(entries):
            entries[selected] = (name, tint)
            self._source_roster(zone, entries, selected)

    def _source_roster(self, zone, entries, selected):
        if (
            len(entries) != len(self._sources.get(zone, ()))
            or zone not in self._sources
        ):
            self._prune_sources(zone, len(entries))
        self._sources[zone] = entries
        zone_name = self.get_child_state("zone", zone).get("name") or f"Zone {zone}"
        for slot, (name, tint) in enumerate(entries, 1):
            title = name or f"Source {slot}"
            self._publish(
                "zone_source",
                f"{zone}_{slot}",
                {
                    "name": f"{zone_name} · {title} ({slot})",
                    "colour": tint,
                    "selected": slot == selected + 1,
                },
            )

    def _prune_sources(self, zone, count):
        prefix = f"{zone}_"
        for key in self.list_children("zone_source"):
            if key.startswith(prefix) and int(key[len(prefix) :]) > count:
                self.deregister_child("zone_source", key)
                self._seen.discard(("zone_source", key))

    def _divider(self, left, right, raw):
        if max(left, right) >= 16 or left == right:
            return
        a, b = sorted((left + 1, right + 1))
        self._publish(
            "divider", f"{a}_{b}", {"name": f"Rooms {a} and {b}", "combined": raw < 64}
        )

    async def _request(self, key, query, write=b"", expected=None):
        async with self._commands:
            actual = await self._exchange(key, write + query)
            if actual is MISSING:
                raise CommandRefusedError(
                    "The processor did not report the requested control."
                )
            if expected is not None and actual != expected:
                raise CommandRefusedError(
                    "The AHM reported a different value after the change."
                )
            return actual

    async def _exchange(self, key, data):
        # A change echo and a Get answer can have identical bytes. Read through
        # a distinct, ordered Get Colour reply before releasing the next request.
        self._pending_key = key
        self._pending_value = MISSING
        self._fence = asyncio.get_running_loop().create_future()
        try:
            await self.transport.send(data + sysex(0, 11, 0))
            return await asyncio.wait_for(self._fence, 3)
        except TimeoutError as exc:
            raise ConnectionFaultError(
                "The processor stopped answering AHM queries.", code="no_response"
            ) from exc
        finally:
            self._fence = None
            self._pending_key = None

    async def _liveness_probe(self):
        await self._request(("input", 1, "name"), sysex(0, 9, 0))

    async def _initial_sync(self):
        await self.refresh_children()

    async def refresh_children(self):
        async with self._refresh_lock:
            sizes = self._bounds()
            for n in (1, 0, 2, 3):
                await self._scan(n, sizes[n])
            for n, ch, dst_n, dst in self._sends.values():
                await self._batch(
                    b"".join(sysex(n, 1, 15, op, ch, dst_n, dst) for op in (2, 3))
                )
        return {
            "message": "Requested the current channel names, levels, mutes and zone selectors."
        }

    async def _scan(self, n, count):
        for start in range(0, count, 4):
            slots = range(start, min(start + 4, count))
            await self._batch(b"".join(sysex(n, 9, ch) for ch in slots))
            frames = [
                self._readings(n, ch)
                for ch in slots
                if self.get_child_state(KINDS[n], ch + 1).get("online")
            ]
            if frames:
                await self._batch(b"".join(frames))
            await asyncio.sleep(0.03)

    def _readings(self, n, ch):
        props = (
            ("level", "muted", "trim", "gain", "pad", "phantom")
            if n == 0
            else ("level", "muted")
        )
        queries = b"".join(get_channel(n, ch, p) for p in props)
        if (n, ch) != (0, 0):
            queries += sysex(n, 11, ch)
        if n == 1:
            queries += sysex(1, 1, 15, 8, ch)
        return queries

    async def _batch(self, data):
        async with self._commands:
            await self._exchange(None, data)

    async def _close_session(self):
        if self._rebuild is not None:
            self._rebuild.cancel()
            await asyncio.gather(self._rebuild, return_exceptions=True)
            self._rebuild = None
        if self._fence is not None and not self._fence.done():
            self._fence.set_exception(ConnectionError("The AHM connection closed."))
        self.set_state("last_preset", None)
        self._sources.clear()
        for kind, ident in self._seen:
            schema = self.DRIVER_INFO["child_entity_types"][kind]["state_variables"]
            self.set_child_state_batch(
                kind,
                ident,
                {**dict.fromkeys(p for p in schema if p != "name"), "online": False},
            )
        self._seen.clear()
        await super()._close_session()

    async def send_command(self, command, params=None):
        params = params or {}
        if command == "refresh":
            return await self.refresh_children()
        operations = {
            "recall_preset": self._recall,
            "play_track": self._play,
            "set_divider": self._set_divider,
        }
        if command in operations:
            return await operations[command](params)
        if command in (
            "select_zone_source",
            "set_zone_source",
            "read_zone_source",
            "set_room_source",
        ):
            return await self._select(command, params)
        if command not in self.DRIVER_INFO["commands"]:
            raise CommandRefusedError("Unknown AHM command.")
        return await self._channel_command(command, params)

    async def _recall(self, params):
        bank, slot = divmod(bounded(params["preset"], 1, 500) - 1, 128)
        return await self.transport.send(bytes((0xB0, 0, bank, 0xC0, slot)))

    async def _play(self, params):
        player = PLAYERS.index(params["player"])
        return await self.transport.send(
            sysex(0, 6, player, bounded(params["track"], 1, 128) - 1)
        )

    async def _channel_command(self, command, params):
        action, kind, word = command.split("_", 2)
        prop = "muted" if word == "mute" else word
        if action in ("raise", "lower"):
            return await self._step(kind, params[kind], action == "raise")
        val = None
        if action == "set":
            val = (
                switch(params[prop])
                if prop in SWITCHES
                else bounded(params[prop], 0, 127)
            )
        if kind == "send":
            return await self._crosspoint(params["send"], prop, val)
        n = KINDS.index(kind)
        ident = bounded(params[kind], 1, self._bounds()[n])
        query = get_channel(n, ident - 1, prop)
        write = set_channel(n, ident - 1, prop, val) if val is not None else b""
        return await self._request((kind, ident, prop), query, write, val)

    async def _step(self, kind, ident, up):
        direction = 127 if up else 63
        if kind == "send":
            if ident not in self._sends:
                raise CommandRefusedError("Choose a configured audio send.")
            n, ch, target, dst = self._sends[ident]
            write = sysex(n, 4, ch, target, dst, direction)
            query = sysex(n, 1, 15, 2, ch, target, dst)
        else:
            n = KINDS.index(kind)
            ident = bounded(ident, 1, self._bounds()[n])
            ch = ident - 1
            write = bytes((176 + n, 99, ch, 176 + n, 98, 32, 176 + n, 6, direction))
            query = get_channel(n, ch, "level")
        return await self._request((kind, ident, "level"), query, write)

    async def _crosspoint(self, key, prop, val):
        if key not in self._sends:
            raise CommandRefusedError("Choose a configured audio send.")
        n, ch, target, dst = self._sends[key]
        op = 2 if prop == "level" else 3
        query = sysex(n, 1, 15, op, ch, target, dst)
        write = b""
        if val is not None:
            raw = (127 if val else 0) if prop == "muted" else val
            write = sysex(n, op, ch, target, dst, raw)
        return await self._request(("send", key, prop), query, write, val)

    async def _select(self, command, params):
        if command == "set_room_source":
            room = bounded(params["room"], 1, 16)
            return await self.transport.send(
                sysex(0, 13, room - 1, bounded(params["source"], 1, 20) - 1)
            )
        zone, slot = self._zone_slot(command, params)
        return await self._zone_select(zone, slot)

    def _zone_slot(self, command, params):
        if command == "select_zone_source":
            key = params["zone_source"]
            if not self.get_child_state("zone_source", key).get("online"):
                raise CommandRefusedError(
                    "Refresh controls and choose a configured zone source."
                )
            zone, slot = map(int, key.split("_"))
        else:
            zone = bounded(params["zone"], 1, self._bounds()[1])
            slot = (
                None
                if command == "read_zone_source"
                else bounded(params["source"], 1, 20)
            )
        return zone, slot

    async def _zone_select(self, zone, slot):
        if slot and slot > len(self._sources.get(zone, range(20))):
            raise CommandRefusedError(
                "This source is outside the zone’s configured selector."
            )
        query = sysex(1, 1, 15, 8, zone - 1)
        write = sysex(0, 8, zone - 1, slot - 1) if slot is not None else b""
        return await self._request(("zone", zone, "source"), query, write, slot)

    async def _set_divider(self, params):
        left, right = (bounded(params[k], 1, 16) for k in ("room_a", "room_b"))
        if left == right:
            raise CommandRefusedError("Choose two different rooms.")
        return await self.transport.send(
            sysex(0, 14, left - 1, right - 1, 0 if switch(params["combined"]) else 127)
        )
