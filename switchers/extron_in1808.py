"""Extron IN1806 and IN1808 scalers, SIS 68-3131-01 Rev. B.

Python uses the released SSH transport and reconciles model-specific audio channels,
sparse named presets, breakaway routes and asynchronous SIS notifications.
"""

from __future__ import annotations

import asyncio
import re
from decimal import Decimal, InvalidOperation

from openavc.drivers.base import BaseDriver, ConnectionFaultError

try:
    from openavc.drivers.base import CommandRefusedError
except ImportError:  # Releases before typed command refusals.
    CommandRefusedError = RuntimeError

ESC = "\x1b"
LINE_REPORT = r"Cpn0?9\s+Ccp(\d+),([NOEMS][a-z]*),([78]),([12])"
BAUDS = (
    300,
    600,
    1200,
    1800,
    2400,
    3600,
    4800,
    7200,
    9600,
    14400,
    19200,
    28800,
    38400,
    57600,
    115200,
)
PARTS = {
    "IN1806": "60-1663-01",
    "IN1808": "60-1615-01",
    "IN1808 IPCP SA": "60-1615-02",
    "IN1808 IPCP MA 70": "60-1615-03",
    "IN1808 IPCP Q SA": "60-1615-92",
    "IN1808 IPCP Q MA 70": "60-1615-93",
}
POWER = ("Full power", "Standby", "Standby with TP power")
AUTO = ("Manual", "User priority", "Last connected")
MUTES = ("Visible", "Black video", "Video and sync off")
HDCP = ("No device detected", "Connected without HDCP", "HDCP present")
FORMAT = ("No signal", "DVI", "HDMI", "DisplayPort")
MIXES = {
    1: ("Microphones", 1, 2),
    2: ("Program audio", 3, 4),
    3: ("Line inputs", 5, 6),
    4: ("Room master", 9, 10),
}
GROUPS = {
    g: (i, prop)
    for i, (_, f, m) in MIXES.items()
    for g, prop in ((f, "level"), (m, "muted"))
}
AUDIO_LABELS = (
    "HDMI left",
    "HDMI right",
    "TP left",
    "TP right",
    "DTP analog left",
    "DTP analog right",
    "Line output 1",
    "Line output 2",
    "Line output 3",
    "Line output 4",
)
OUTPUT_LABELS = ("HDMI 1A", "Twisted pair 1B", "HDMI loop")
ERRORS = {
    "E10": "unrecognized command",
    "E11": "preset unavailable",
    "E12": "invalid port",
    "E13": "invalid value",
    "E14": "unavailable in this configuration",
    "E17": "unavailable for this signal",
    "E22": "scaler busy",
    "E24": "insufficient account privileges",
}
ROUTES = {
    "video_input": ("%", "Vid"),
    "audio_input": ("$", "Aud"),
    "loop_input": ("LOUT", "Loop"),
}
GLOBAL = {
    "freeze": ("frozen", "1F", "Frz0?1\\*"),
    "auto_switch": ("auto_switch", ESC + "AUSW", "Ausw"),
    "power_mode": ("power_mode", ESC + "PSAV", "Psav"),
}


def switch(value):
    if str(value).lower() in ("true", "1", "on"):
        return 1
    if str(value).lower() in ("false", "0", "off"):
        return 0
    raise CommandRefusedError("Choose on or off.")


def tenths(value, low, high):
    try:
        level = Decimal(str(value))
        if not level.is_finite() or not low <= level <= high:
            raise ValueError
        scaled = level * 10
        if scaled != scaled.to_integral_value():
            raise ValueError
        return int(scaled)
    except (ValueError, InvalidOperation) as exc:
        raise CommandRefusedError(
            f"Choose a level from {low} to {high} dB in 0.1 dB steps."
        ) from exc


def serial_format(params):
    baud = str(params.get("baudrate", 9600))
    bits = str(params.get("bytesize", 8))
    parity = str(params.get("parity", "N")).upper()
    stop = str(params.get("stopbits", 1))
    if not baud.isdecimal() or int(baud) not in BAUDS:
        raise CommandRefusedError(
            "Choose one of the scaler's supported baud rates from 300 to 115200."
        )
    if bits not in ("7", "8") or parity not in ("N", "O", "E", "M", "S"):
        raise CommandRefusedError(
            "Choose seven or eight data bits and a supported RS-232 parity."
        )
    flow = str(params.get("flow_control", "none")).lower()
    if (
        flow not in ("none", "")
        or stop not in ("1", "1.0", "2", "2.0")
        or any(params.get(k) for k in ("xonxoff", "rtscts", "dsrdtr"))
    ):
        raise CommandRefusedError(
            "Choose one or two stop bits and no flow control for output 1B."
        )
    return baud, parity, bits, stop[0]


def choice(value, options):
    if value not in options:
        raise CommandRefusedError("Choose one of the listed options.")
    return options.index(value)


def valid_input_name(slot):
    if slot == 1:
        return "DisplayPort 1"
    return f"HDMI {slot}" if slot <= 6 else f"Twisted pair {slot}"


def amp_labels(model):
    if model.endswith("SA"):
        return ("Amplifier left", "Amplifier right")
    if model.endswith("70"):
        return ("70 V amplifier",)
    return ()


def route_pattern(prop):
    if prop == "loop_input":
        return r"Lout(\d+)"
    return rf"In\s*(\d+)\*0?1\s*{ROUTES[prop][1]}"


def route_query(prop):
    return ESC + "LOUT" if prop == "loop_input" else "1" + ROUTES[prop][0]


def matches(line, pattern):
    return re.fullmatch(pattern, line, re.I)


class ExtronIN1808(BaseDriver):
    DRIVER_INFO = {
        "id": "extron_in1808",
        "name": "Extron IN1806 / IN1808 Series",
        "manufacturer": "Extron",
        "category": "switcher",
        "version": "1.0.0",
        "min_platform_version": "0.27.0",
        "author": "OpenAVC",
        "source_url": "https://media.extron.com/public/download/files/userman/68-3131-01_B_IN1806-1808_UG.pdf",
        "description": "Control IN1806 and IN1808 classroom presentation switchers using source names from the "
        "scaler. Includes signal and HDCP feedback, independent loop routing, room volume and "
        "model-specific amplifier channels.",
        "tags": [
            "presentation-switcher",
            "scaler",
            "classroom",
            "sis",
            "serial-bridge",
        ],
        "ports": [22023],
        "protocols": ["extron_sis"],
        "verified": False,
        "simulated": True,
        "compatible_models": [
            {
                "manufacturer": "Extron",
                "models": [
                    "IN1808 IPCP Q MA 70",
                    "IN1808 IPCP Q SA",
                    "IN1808",
                    "IN1808 IPCP MA 70",
                    "IN1808 IPCP SA",
                    "IN1806",
                ],
                "confidence": "untested",
                "notes": "SIS controls from user guide 68-3131-01 Rev. B. Tested in simulation; "
                "physical verification pending.",
            }
        ],
        "transport": "ssh",
        "transports": ["ssh", "serial"],
        "bridge": {
            "ports": [
                {
                    "id": "serial:1",
                    "kind": "serial",
                    "label": "Output 1B · far-end RS-232 (TCP 2003)",
                    "passthrough_port": 2003,
                }
            ]
        },
        "default_config": {
            "host": "",
            "port": 22023,
            "baudrate": 9600,
            "bytesize": 8,
            "parity": "N",
            "stopbits": 1,
            "username": "admin",
            "ssh_auth_method": "password",
            "model": "IN1808 IPCP Q MA 70",
            "poll_interval": 120,
        },
        "config_schema": {
            "host": {"type": "string", "label": "Scaler address", "required": True},
            "port": {
                "type": "integer",
                "label": "SIS over SSH port",
                "min": 1,
                "max": 65535,
                "default": 22023,
            },
            "username": {"type": "string", "label": "SSH username", "default": "admin"},
            "password": {"type": "string", "label": "Scaler password", "secret": True},
            "model": {
                "type": "enum",
                "label": "Model for offline setup and simulation",
                "values": [
                    "IN1808 IPCP Q MA 70",
                    "IN1808 IPCP Q SA",
                    "IN1808",
                    "IN1808 IPCP MA 70",
                    "IN1808 IPCP SA",
                    "IN1806",
                ],
                "default": "IN1808 IPCP Q MA 70",
                "learned_from": "model",
                "description": "The connected scaler identifies itself. Choose a model here to "
                "simulate its input and amplifier configuration.",
            },
            "poll_interval": {
                "type": "integer",
                "label": "Status refresh interval (seconds)",
                "min": 0,
                "max": 3600,
                "default": 120,
                "description": "Changes also arrive live. Zero turns off the periodic "
                "status refresh.",
            },
        },
        "discovery": {
            "tcp_probe": {
                "port": 23,
                "expect_regex": "(?i)Extron Electronics,\\s*IN180(?:6|8)(?: IPCP(?: Q)? (?:SA|MA "
                "70))?\\s*,",
                "extract_manufacturer": "Extron",
                "extract": {
                    "model": {
                        "regex": "(?i)(IN180(?:6|8)(?: IPCP(?: Q)? (?:SA|MA 70))?)\\s*,"
                    }
                },
            },
            "oui": ["00:05:a6"],
            "manufacturer_alias": ["extron"],
        },
        "help": {
            "overview": "Present one source to the room or switch video and audio separately. The main HDMI and "
            "twisted-pair outputs share the presentation source. The HDMI loop monitor has its own "
            "source.",
            "setup": "Enter the scaler’s address and SIS account for SSH. On IPCP models use the scaler address "
            "on a reachable LAN or AV LAN. The factory password is normally the serial number. For "
            "RS-232 use the rear Remote port at 9600 baud, 8 data bits, no parity and one stop bit. "
            "Refresh sources after editing their names in PCS.\n"
            "To control a projector through the scaler, wire the projector to the receiver’s RS-232 connector. "
            "Use a reachable scaler LAN address, UART start point 2000 and TCP/IP UART insertion on output 1B. "
            "Add the projector with its own serial driver. Choose Through a bridge, this scaler and Output 1B. "
            "Enter the projector’s line settings on the projector device. The scaler applies and reads them back. "
            "Read output 1B serial setup, then request a real status from the projector. Full power and Standby with TP power support the link.",
            "connection": "Bind the main video source for selection and Selected source has video for signal "
            "presence. The amplifier channels appear under Audio output channels on amplified "
            "models. For one room fader use Room master volume. Room groups respect their "
            "configured soft limits. Picture presets adjust the current input.",
        },
        "state_variables": {
            "model": {"type": "string", "label": "Detected scaler model"},
            "firmware": {"type": "string", "label": "Firmware version"},
            "part_number": {"type": "string", "label": "Part number"},
            "video_input": {
                "type": "integer",
                "label": "Main video source",
                "min": 1,
                "max": 8,
                "control": True,
            },
            "audio_input": {
                "type": "integer",
                "label": "Main audio source",
                "min": 1,
                "max": 9,
                "control": True,
            },
            "loop_input": {
                "type": "integer",
                "label": "Loop output source",
                "min": 1,
                "max": 8,
                "control": True,
            },
            "video_name": {"type": "string", "label": "Main video source name"},
            "audio_name": {"type": "string", "label": "Main audio source name"},
            "loop_name": {"type": "string", "label": "Loop source name"},
            "video_signal": {
                "type": "boolean",
                "label": "Selected source has video",
                "control": True,
                "cloud_priority": "high",
            },
            "breakaway": {"type": "boolean", "label": "Audio routed separately"},
            "frozen": {"type": "boolean", "label": "Picture frozen", "control": True},
            "auto_switch": {
                "type": "enum",
                "label": "Automatic source selection",
                "values": ["Manual", "User priority", "Last connected"],
                "control": True,
            },
            "power_mode": {
                "type": "enum",
                "label": "Scaler power mode",
                "values": ["Full power", "Standby", "Standby with TP power"],
                "control": True,
            },
            "room_volume": {
                "type": "number",
                "label": "Room master volume",
                "min": -100,
                "max": 80,
                "step": 0.1,
                "unit": "dB",
                "control": True,
            },
            "room_muted": {
                "type": "boolean",
                "label": "Room master muted",
                "control": True,
            },
            "bridge_status": {"type": "string", "label": "Output 1B serial setup"},
            "last_picture_preset": {
                "type": "integer",
                "label": "Last observed picture preset recall",
                "min": 1,
                "max": 16,
                "help": "Unknown until the scaler reports a recall on this connection.",
            },
        },
        "child_entity_types": {
            "input": {
                "label": "Video input",
                "label_plural": "Video inputs",
                "label_field": "name",
                "id_format": {"type": "integer", "min": 1, "max": 8},
                "state_variables": {
                    "name": {"type": "string", "label": "Video input name"},
                    "signal": {
                        "type": "boolean",
                        "label": "Video signal present",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "format": {
                        "type": "enum",
                        "label": "Detected format",
                        "values": ["No signal", "DVI", "HDMI", "DisplayPort"],
                    },
                    "hdcp": {
                        "type": "enum",
                        "label": "Input HDCP status",
                        "values": [
                            "No device detected",
                            "Connected without HDCP",
                            "HDCP present",
                        ],
                    },
                },
                "summary_fields": ["signal", "format"],
            },
            "audio_source": {
                "label": "Audio source",
                "label_plural": "Audio sources",
                "label_field": "name",
                "id_format": {"type": "integer", "min": 1, "max": 9},
                "state_variables": {
                    "name": {"type": "string", "label": "Audio source name"}
                },
                "summary_fields": ["name"],
            },
            "output": {
                "label": "Video output",
                "label_plural": "Video outputs",
                "label_field": "name",
                "id_format": {"type": "integer", "min": 1, "max": 3},
                "state_variables": {
                    "name": {"type": "string", "label": "Video output name"},
                    "mute_mode": {
                        "type": "enum",
                        "label": "Picture mute",
                        "values": ["Visible", "Black video", "Video and sync off"],
                        "control": True,
                    },
                    "hdcp": {
                        "type": "enum",
                        "label": "Output HDCP status",
                        "values": [
                            "No device detected",
                            "Connected without HDCP",
                            "HDCP present",
                        ],
                    },
                    "source": {
                        "type": "integer",
                        "label": "Routed video input",
                        "min": 1,
                        "max": 8,
                    },
                },
                "summary_fields": ["source", "mute_mode", "hdcp"],
            },
            "audio_channel": {
                "label": "Audio output channel",
                "label_plural": "Audio output channels",
                "label_field": "name",
                "id_format": {"type": "integer", "min": 1, "max": 12},
                "state_variables": {
                    "name": {"type": "string", "label": "Audio output channel name"},
                    "level": {
                        "type": "number",
                        "label": "Output level",
                        "min": -100,
                        "max": 0,
                        "step": 0.1,
                        "unit": "dB",
                        "control": True,
                    },
                    "muted": {
                        "type": "boolean",
                        "label": "Muted",
                        "control": True,
                        "cloud_priority": "high",
                    },
                },
                "summary_fields": ["level", "muted"],
            },
            "mix": {
                "dynamic": True,
                "label": "Room audio group",
                "label_plural": "Room audio groups",
                "label_field": "name",
                "id_format": {"type": "integer", "min": 1, "max": 4},
                "state_variables": {
                    "name": {"type": "string", "label": "Room audio group name"},
                    "level": {
                        "type": "number",
                        "label": "Group level",
                        "min": -100,
                        "max": 80,
                        "step": 0.1,
                        "unit": "dB",
                        "control": True,
                    },
                    "muted": {
                        "type": "boolean",
                        "label": "Muted",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "minimum": {"type": "number", "label": "Lowest allowed level (dB)"},
                    "maximum": {
                        "type": "number",
                        "label": "Highest allowed level (dB)",
                    },
                },
                "summary_fields": ["level", "muted"],
            },
            "preset": {
                "label": "Picture preset",
                "label_plural": "Picture presets",
                "label_field": "name",
                "id_format": {"type": "integer", "min": 1, "max": 16},
                "state_variables": {
                    "name": {"type": "string", "label": "Picture preset name"}
                },
                "summary_fields": ["name"],
            },
        },
        "commands": {
            "set_video_input": {
                "label": "Set main video source",
                "help": "Switch the mirrored presentation outputs while keeping the audio "
                "route.",
                "params": {
                    "source": {
                        "type": "child_id",
                        "label": "Video input",
                        "child_type": "input",
                        "required": True,
                    }
                },
                "sets": {"video_input": "{source}"},
            },
            "read_video_input": {
                "label": "Read main video source",
                "help": "Read the current value from the scaler.",
                "params": {},
                "query_for": "video_input",
            },
            "set_audio_input": {
                "label": "Set main audio source",
                "help": "Select presentation audio independently, including the auxiliary "
                "audio input.",
                "params": {
                    "source": {
                        "type": "child_id",
                        "label": "Audio source",
                        "child_type": "audio_source",
                        "required": True,
                    }
                },
                "sets": {"audio_input": "{source}"},
            },
            "read_audio_input": {
                "label": "Read main audio source",
                "help": "Read the current value from the scaler.",
                "params": {},
                "query_for": "audio_input",
            },
            "set_loop_input": {
                "label": "Set loop output source",
                "help": "Choose the input on the HDMI loop monitor independently of the main "
                "presentation.",
                "params": {
                    "source": {
                        "type": "child_id",
                        "label": "Video input",
                        "child_type": "input",
                        "required": True,
                    }
                },
                "sets": {"loop_input": "{source}"},
            },
            "read_loop_input": {
                "label": "Read loop output source",
                "help": "Read the current value from the scaler.",
                "params": {},
                "query_for": "loop_input",
            },
            "present_source": {
                "label": "Present a source",
                "help": "Wake the scaler if needed and switch both picture and program audio on the main presentation outputs.",
                "params": {
                    "source": {
                        "type": "child_id",
                        "label": "Video input",
                        "child_type": "input",
                        "required": True,
                    }
                },
                "sets": {"video_input": "{source}", "audio_input": "{source}"},
            },
            "set_freeze": {
                "label": "Set picture freeze",
                "help": "Hold the last frame on the main scaled outputs or return to live video.",
                "params": {
                    "frozen": {"type": "boolean", "label": "Freeze", "required": True}
                },
                "sets": {"frozen": "{frozen}"},
            },
            "read_freeze": {
                "label": "Read picture freeze",
                "help": "Read the current value from the scaler.",
                "params": {},
                "query_for": "frozen",
            },
            "set_auto_switch": {
                "label": "Set automatic source selection",
                "help": "Choose manual, site-defined priority, or last-connected switching.",
                "params": {
                    "mode": {
                        "type": "enum",
                        "label": "Mode",
                        "values": ["Manual", "User priority", "Last connected"],
                        "required": True,
                    }
                },
                "sets": {"auto_switch": "{mode}"},
            },
            "read_auto_switch": {
                "label": "Read automatic source selection",
                "help": "Read the current value from the scaler.",
                "params": {},
                "query_for": "auto_switch",
            },
            "set_power_mode": {
                "label": "Set scaler power mode",
                "help": "Choose full power or standby. Standby with TP power keeps the "
                "twisted-pair link powered.",
                "params": {
                    "mode": {
                        "type": "enum",
                        "label": "Mode",
                        "values": ["Full power", "Standby", "Standby with TP power"],
                        "required": True,
                    }
                },
                "sets": {"power_mode": "{mode}"},
            },
            "read_power_mode": {
                "label": "Read scaler power mode",
                "help": "Read the current value from the scaler.",
                "params": {},
                "query_for": "power_mode",
            },
            "set_output_mute": {
                "label": "Set output picture mute",
                "help": "Choose black video, disable sync, or restore the selected output.",
                "params": {
                    "output": {
                        "type": "child_id",
                        "label": "Video output",
                        "child_type": "output",
                        "required": True,
                    },
                    "mode": {
                        "type": "enum",
                        "label": "Picture mute",
                        "values": ["Visible", "Black video", "Video and sync off"],
                        "required": True,
                    },
                },
                "sets": {"mute_mode": "{mode}"},
            },
            "read_output_mute": {
                "label": "Read output picture mute",
                "help": "Read the current value from the scaler.",
                "params": {
                    "output": {
                        "type": "child_id",
                        "label": "Video output",
                        "child_type": "output",
                        "required": True,
                    }
                },
                "query_for": "mute_mode",
            },
            "set_room_volume": {
                "label": "Set room master volume",
                "help": "Adjust room master volume and confirm the value reported by the "
                "scaler.",
                "params": {
                    "level": {
                        "type": "number",
                        "label": "Level (dB)",
                        "min": -100,
                        "max": 80,
                        "required": True,
                    }
                },
                "sets": {"room_volume": "{level}"},
            },
            "read_room_volume": {
                "label": "Read room master volume",
                "help": "Read the current value from the scaler.",
                "params": {},
                "query_for": "room_volume",
            },
            "set_room_mute": {
                "label": "Set room master mute",
                "help": "Adjust room master mute and confirm the value reported by the scaler.",
                "params": {
                    "muted": {"type": "boolean", "label": "Muted", "required": True}
                },
                "sets": {"room_muted": "{muted}"},
            },
            "read_room_mute": {
                "label": "Read room master mute",
                "help": "Read the current value from the scaler.",
                "params": {},
                "query_for": "room_muted",
            },
            "set_mix_volume": {
                "label": "Set room audio group volume",
                "help": "Adjust room audio group volume and confirm the value reported by the "
                "scaler.",
                "params": {
                    "mix": {
                        "type": "child_id",
                        "label": "Room audio group",
                        "child_type": "mix",
                        "required": True,
                    },
                    "level": {
                        "type": "number",
                        "label": "Level (dB)",
                        "min": -100,
                        "max": 80,
                        "required": True,
                    },
                },
                "sets": {"level": "{level}"},
            },
            "read_mix_volume": {
                "label": "Read room audio group volume",
                "help": "Read the current value from the scaler.",
                "params": {
                    "mix": {
                        "type": "child_id",
                        "label": "Room audio group",
                        "child_type": "mix",
                        "required": True,
                    }
                },
                "query_for": "level",
            },
            "set_mix_mute": {
                "label": "Set room audio group mute",
                "help": "Adjust room audio group mute and confirm the value reported by the "
                "scaler.",
                "params": {
                    "mix": {
                        "type": "child_id",
                        "label": "Room audio group",
                        "child_type": "mix",
                        "required": True,
                    },
                    "muted": {"type": "boolean", "label": "Muted", "required": True},
                },
                "sets": {"muted": "{muted}"},
            },
            "read_mix_mute": {
                "label": "Read room audio group mute",
                "help": "Read the current value from the scaler.",
                "params": {
                    "mix": {
                        "type": "child_id",
                        "label": "Room audio group",
                        "child_type": "mix",
                        "required": True,
                    }
                },
                "query_for": "muted",
            },
            "set_audio_channel_volume": {
                "label": "Set audio output channel volume",
                "help": "Adjust audio output channel volume and confirm the value "
                "reported by the scaler.",
                "params": {
                    "audio_channel": {
                        "type": "child_id",
                        "label": "Audio output channel",
                        "child_type": "audio_channel",
                        "required": True,
                    },
                    "level": {
                        "type": "number",
                        "label": "Level (dB)",
                        "min": -100,
                        "max": 0,
                        "required": True,
                    },
                },
                "sets": {"level": "{level}"},
            },
            "read_audio_channel_volume": {
                "label": "Read audio output channel volume",
                "help": "Read the current value from the scaler.",
                "params": {
                    "audio_channel": {
                        "type": "child_id",
                        "label": "Audio output channel",
                        "child_type": "audio_channel",
                        "required": True,
                    }
                },
                "query_for": "level",
            },
            "set_audio_channel_mute": {
                "label": "Set audio output channel mute",
                "help": "Adjust audio output channel mute and confirm the value "
                "reported by the scaler.",
                "params": {
                    "audio_channel": {
                        "type": "child_id",
                        "label": "Audio output channel",
                        "child_type": "audio_channel",
                        "required": True,
                    },
                    "muted": {"type": "boolean", "label": "Muted", "required": True},
                },
                "sets": {"muted": "{muted}"},
            },
            "read_audio_channel_mute": {
                "label": "Read audio output channel mute",
                "help": "Read the current value from the scaler.",
                "params": {
                    "audio_channel": {
                        "type": "child_id",
                        "label": "Audio output channel",
                        "child_type": "audio_channel",
                        "required": True,
                    }
                },
                "query_for": "muted",
            },
            "read_input_signal": {
                "label": "Read input signal",
                "help": "Check whether the selected physical input has an active video "
                "signal.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "label": "Video input",
                        "child_type": "input",
                        "required": True,
                    }
                },
                "query_for": "signal",
            },
            "read_input_hdcp": {
                "label": "Read input HDCP",
                "help": "Read the source connection and copy-protection status.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "label": "Video input",
                        "child_type": "input",
                        "required": True,
                    }
                },
                "query_for": "hdcp",
            },
            "read_output_hdcp": {
                "label": "Read output HDCP",
                "help": "Read the display connection and copy-protection status.",
                "params": {
                    "output": {
                        "type": "child_id",
                        "label": "Video output",
                        "child_type": "output",
                        "required": True,
                    }
                },
                "query_for": "hdcp",
            },
            "recall_picture_preset": {
                "label": "Recall picture preset",
                "help": "Apply a saved image adjustment preset to the currently "
                "selected input.",
                "params": {
                    "preset": {
                        "type": "child_id",
                        "label": "Picture preset",
                        "child_type": "preset",
                        "required": True,
                    }
                },
                "sets": {"last_picture_preset": "{preset}"},
            },
            "read_serial_bridge": {
                "label": "Read output 1B serial setup",
                "help": "Check the device’s UART starting port, insertion mode and line settings for the far-end RS-232 connector.",
                "query_for": "bridge_status",
                "params": {},
            },
            "refresh": {
                "label": "Refresh sources and settings",
                "help": "Read the names, routes, signal status, audio limits and saved picture "
                "presets again.",
                "params": {},
            },
        },
        "quick_actions": [
            "present_source",
            "set_room_volume",
            "set_room_mute",
            "set_freeze",
            "set_loop_input",
            "refresh",
        ],
        "routing": {
            "planes": [
                {
                    "label": "Main video",
                    "route_property": "video_input",
                    "command": "set_video_input",
                    "source_param": "source",
                    "source_child_type": "input",
                },
                {
                    "label": "Main audio",
                    "route_property": "audio_input",
                    "command": "set_audio_input",
                    "source_param": "source",
                    "source_child_type": "audio_source",
                },
                {
                    "label": "Loop monitor",
                    "route_property": "loop_input",
                    "command": "set_loop_input",
                    "source_param": "source",
                    "source_child_type": "input",
                },
            ]
        },
        "device_settings": {
            "auto_switch": {
                "type": "enum",
                "label": "Automatic source selection",
                "values": ["Manual", "User priority", "Last connected"],
                "state_key": "auto_switch",
            }
        },
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._io = asyncio.Lock()
        self._sync = asyncio.Lock()
        self._pending = None
        self._write_errors = None
        self._rx = b""
        self._session = False
        self._model = None
        self._needs_inventory = True
        self._refresh_task = None
        self._refresh_due = False
        self._changed_inputs = set()

    def _resolve_delimiter(self):
        return None

    async def _ask(self, cmd, pattern):
        if not self.transport or not self._session:
            raise ConnectionError("The scaler is offline.")
        future = asyncio.get_running_loop().create_future()
        self._pending = (cmd, re.compile(pattern, re.I), future)
        try:
            await self.transport.send((cmd + "\r").encode("ascii"))
            return await asyncio.wait_for(future, 4)
        except TimeoutError as exc:
            self._session = False
            self._force_disconnect(
                "no_response", "The scaler stopped answering SIS commands."
            )
            raise ConnectionError("The scaler did not answer the request.") from exc
        finally:
            self._pending = None
            future.cancel()

    async def _write(self, command, pattern):
        self._write_errors = []
        try:
            await self._ask(command, pattern)
            # Mode 3 may report another controller's change before our reply.
            # The distinct part-number reply closes the write before readback.
            await self._ask("N", r"Pno\s*60-\d{4}-\d{2}[A-Z]?")
            if self._write_errors:
                raise self._refusal(self._write_errors[0])
        finally:
            self._write_errors = None

    def on_data_received(self, data):
        self._rx += data
        while b"\n" in self._rx:
            raw, _, self._rx = self._rx.partition(b"\n")
            self._receive(raw.rstrip(b"\r").decode("latin-1"))
        if len(self._rx) > 8192:
            self._rx = b""

    def _receive(self, line):
        if not self._session:
            return
        if self._pending:
            cmd, pattern, future = self._pending
            if line in (cmd, cmd.replace(ESC, "^[")):
                return
            if self._write_errors is not None and matches(line, r"E\d\d"):
                self._write_errors.append(line)
                if cmd == "N":
                    return
            self._answer(line, pattern, future)
        if self._model:
            self._notice(line)

    @staticmethod
    def _refusal(line):
        reason = ERRORS.get(line, "device error")
        return CommandRefusedError(
            f"The scaler refused the command ({line}, {reason})."
        )

    @classmethod
    def _answer(cls, line, pattern, future):
        if future.done():
            return
        if matches(line, r"E\d\d"):
            future.set_exception(cls._refusal(line))
        elif result := pattern.fullmatch(line):
            future.set_result(result)

    async def _post_connect(self):
        self._rx = b""
        self._session = True
        async with self._io:
            await self._ask(ESC + "3CV", r"Vrb0?3")
            reply = await self._ask(
                "1I", r"Inf0?1\*\s*([A-Za-z][A-Za-z0-9 +/.-]{0,60})"
            )
            model = " ".join(reply[1].upper().split())
            if model not in PARTS:
                raise ConnectionFaultError(
                    f"The device identified as {model}. Choose its matching driver.",
                    code="invalid_config",
                )
            self._model = model
            self.set_state("model", model)
            self._roster(model)
            self.set_state(
                "part_number",
                (await self._ask("N", r"(?:Pno\s*\*?)?(60-\d{4}-\d{2}[A-Z]?)"))[1],
            )
            self.set_state(
                "firmware", (await self._ask("Q", r"(?:Ver0?1\*?)?(\d+(?:\.\d+)+)"))[1]
            )

    def _roster(self, model):
        count = 6 if model == "IN1806" else 8
        inputs = {n: valid_input_name(n) for n in range(1, count + 1)}
        self._roster_type("input", inputs)
        self._roster_type("audio_source", {**inputs, 9: "Aux audio"})
        self._roster_type("output", dict(enumerate(OUTPUT_LABELS, 1)))
        self._roster_type(
            "audio_channel", dict(enumerate(AUDIO_LABELS + amp_labels(model), 1))
        )
        self._roster_type("mix", {n: r[0] for n, r in MIXES.items()})

    def _roster_type(self, kind, labels):
        for ident in set(self.list_children(kind)) - labels.keys():
            self.deregister_child(kind, ident)
        for ident, label in labels.items():
            self.register_child(kind, ident)
            self.set_child_state_batch(kind, ident, {"name": label, "online": True})

    async def _initial_sync(self):
        await self.refresh_children()

    async def _liveness_probe(self):
        async with self._io:
            await self._global("power_mode")

    async def _close_session(self):
        self._session = False
        pending, self._pending = self._pending, None
        if pending and not pending[2].done():
            pending[2].set_exception(ConnectionError("The scaler connection closed."))
        task, self._refresh_task = self._refresh_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._model = None
        self._needs_inventory = True
        self._refresh_due = False
        self._changed_inputs.clear()
        self.set_states(dict.fromkeys(self.DRIVER_INFO["state_variables"]))
        self._expire_children()
        await super()._close_session()

    def _expire_children(self):
        for kind, spec in self.DRIVER_INFO["child_entity_types"].items():
            empty = dict.fromkeys(
                prop for prop in spec["state_variables"] if prop != "name"
            )
            for ident in self.list_children(kind):
                self.set_child_state_batch(kind, ident, {**empty, "online": False})

    def _child(self, kind, ident, updates):
        if self.is_child_registered(kind, ident):
            self.set_child_state_batch(kind, ident, {**updates, "online": True})

    def _route(self, prop, ident):
        kind = "audio_source" if prop == "audio_input" else "input"
        if not self.is_child_registered(kind, ident):
            return
        self.set_state(prop, ident)
        self._derived()
        if prop == "audio_input":
            return
        for port in (3,) if prop == "loop_input" else (1, 2):
            self._child("output", port, {"source": ident})

    def _derived(self):
        video, audio = self.get_state("video_input"), self.get_state("audio_input")
        values = {}
        for prop, kind, ident in (
            ("video_name", "input", video),
            ("loop_name", "input", self.get_state("loop_input")),
            ("audio_name", "audio_source", audio),
        ):
            values[prop] = (
                self.get_child_state(kind, ident).get("name") if ident else None
            )
        values["video_signal"] = (
            self.get_child_state("input", video).get("signal") if video else None
        )
        values["breakaway"] = audio != video if audio and video else None
        self.set_states(values)

    def _notice(self, line):
        # A query in verbose mode 3 returns the same tagged form as a change.
        for pattern, handler in self._report_handlers():
            match = matches(line, pattern)
            if match:
                handler(*match.groups())
                return
        if line.casefold() == "reconfig":
            self._schedule_refresh()

    def _report_handlers(self):
        return (
            (r"In\s*(\d+)\*0?1\s*(All|Vid|Aud)", self._main_notice),
            (r"Lout(\d+)", lambda slot: self._route("loop_input", int(slot))),
            (r"IN00\s+([01](?:\*[01]){5,7})", self._signals),
            (r"(Vnam[IO]|AnamI)\s*(\d+)\*(.*)", self._name_notice),
            (r"Vtyp\s*(\d+)\*([0-3])", self._format_notice),
            (r"Hdcp([IO])\s*(\d+)\*([0-2])", self._hdcp_notice),
            (r"HplgO\s*(\d+)\*[12]", lambda _: self._schedule_refresh()),
            (r"Vmt\s*(\d+)\*([0-2])", self._mute_notice),
            (r"Vmt([0-2])", self._mute_all),
            (r"Vmt([0-2])\s+([0-2])\s+([0-2])", self._mute_list),
            (r"Frz0?1\*([01])", lambda v: self.set_state("frozen", v == "1")),
            (r"Ausw([0-2])", lambda v: self.set_state("auto_switch", AUTO[int(v)])),
            (r"Psav([0-2])", self._power_notice),
            (r"Grpm([DL])0*(\d+)\*([+-]?\d+)(?:\*([+-]?\d+))?", self._group_notice),
            (r"Ds([GM])(60\d{3})\*([+-]?\d+)", self._channel_notice),
            (r"Pnam2\*0*(\d+),(.*)", self._preset_name),
            (r"2Rpr0*(\d+)", self._preset_recalled),
            (r"(?:2Spr|PrstX2\*)0*(\d+)", self._preset_changed),
        )

    def _main_notice(self, raw, plane):
        ident = int(raw)
        if plane.lower() in ("all", "vid"):
            self._route("video_input", ident)
        if plane.lower() in ("all", "aud"):
            self._route("audio_input", ident)

    def _signals(self, raw):
        bits = raw.split("*")
        slots = self.list_children("input")
        if len(bits) != len(slots):
            return
        changed = set()
        for n, bit in enumerate(bits, 1):
            present = bit == "1"
            previous = self.get_child_state("input", n).get("signal")
            if previous is not None and previous != present:
                changed.add(n)
            self._child("input", n, {"signal": present})
        self._derived()
        if changed:
            self._schedule_refresh(changed)

    def _name_notice(self, op, raw, name):
        kind = {"vnami": "input", "vnamo": "output", "anami": "audio_source"}[
            op.lower()
        ]
        self._child(kind, int(raw), {"name": name.strip()})
        self._derived()

    def _format_notice(self, slot, code):
        self._child("input", int(slot), {"format": FORMAT[int(code)]})

    def _hdcp_notice(self, side, slot, code):
        kind = "input" if side.upper() == "I" else "output"
        self._child(kind, int(slot), {"hdcp": HDCP[int(code)]})

    def _mute_notice(self, slot, code):
        self._child("output", int(slot), {"mute_mode": MUTES[int(code)]})

    def _mute_all(self, code):
        self._mute_list(code, code, code)

    def _mute_list(self, *codes):
        for slot, code in enumerate(codes, 1):
            self._mute_notice(slot, code)

    def _power_notice(self, code):
        mode = POWER[int(code)]
        old = self.get_state("power_mode")
        self.set_state("power_mode", mode)
        if old == mode:
            return
        self._needs_inventory = True
        if code == "0":
            if old is not None:
                self._schedule_refresh()
            return
        self.set_states(
            dict.fromkeys(
                k
                for k in self.DRIVER_INFO["state_variables"]
                if k not in ("model", "part_number", "firmware", "power_mode")
            )
        )
        self._expire_children()

    def _schedule_refresh(self, inputs=None):
        if not self.connected or not self._session:
            return
        if inputs is None:
            self._refresh_due = True
        else:
            self._changed_inputs.update(inputs)
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh_changes())

    async def _refresh_changes(self):
        try:
            while self._refresh_due or self._changed_inputs:
                inputs, self._changed_inputs = self._changed_inputs, set()
                if self._refresh_due:
                    self._refresh_due = False
                    await self.poll()
                else:
                    await self._refresh_inputs(inputs)
        except (ConnectionError, CommandRefusedError):
            return

    async def _refresh_inputs(self, inputs):
        async with self._io:
            if self.get_state("power_mode") == POWER[0]:
                for n in sorted(inputs):
                    await self._input_status(n)

    def _group_notice(self, op, raw, val, low):
        group = int(raw)
        if group not in GROUPS:
            return
        ident, prop = GROUPS[group]
        if op.upper() == "L":
            if low is not None and prop == "level":
                self._mix_limits(ident, int(low) / 10, int(val) / 10)
            return
        value = int(val) / 10 if prop == "level" else bool(int(val))
        self._child("mix", ident, {prop: value})
        if ident == 4:
            self.set_state("room_volume" if prop == "level" else "room_muted", value)

    def _mix_limits(self, ident, low, high):
        if low > high:
            return
        defs = self.DRIVER_INFO["child_entity_types"]["mix"]["state_variables"]
        bounds = self.get_child_schema("mix", ident)["level"]
        if (bounds["min"], bounds["max"]) != (low, high):
            state = self.get_child_state("mix", ident)
            retained = {key: state[key] for key in defs if state.get(key) is not None}
            schema = {**defs, "level": dict(defs["level"], min=low, max=high)}
            self.deregister_child("mix", ident)
            self.register_child("mix", ident, initial_state=retained, schema=schema)
        self._child("mix", ident, {"minimum": low, "maximum": high})

    def _channel_notice(self, op, raw, value):
        ident = int(raw) - 59999
        prop = "level" if op.upper() == "G" else "muted"
        self._child(
            "audio_channel",
            ident,
            {prop: int(value) / 10 if prop == "level" else bool(int(value))},
        )

    def _preset_name(self, raw, name):
        ident = int(raw)
        if ident not in range(1, 17):
            return
        if name.strip().lower() in ("", "[unassigned]"):
            if self.is_child_registered("preset", ident):
                self.deregister_child("preset", ident)
            return
        self.register_child("preset", ident)
        self._child("preset", ident, {"name": name.strip()})

    def _preset_changed(self, raw):
        if int(raw) in range(1, 17):
            self._needs_inventory = True
            self._schedule_refresh()

    def _preset_recalled(self, raw):
        ident = int(raw)
        if ident in range(1, 17):
            self.set_state("last_picture_preset", ident)
            self._schedule_refresh()

    async def _global(self, key, value=None):
        prop, query, prefix = GLOBAL[key]
        pattern = prefix + r"([0-2])"
        if value is not None:
            wire = {
                "freeze": f"1*{value}F",
                "auto_switch": f"{ESC}{value}AUSW",
                "power_mode": f"{ESC}{value}PSAV",
            }[key]
            await self._write(wire, pattern)
        raw = (await self._ask(query, pattern))[1]
        self._confirm(value, int(raw))
        return self.get_state(prop)

    @staticmethod
    def _confirm(requested, actual):
        if requested is not None and requested != actual:
            raise CommandRefusedError(
                "The scaler reported a different setting after the change."
            )

    async def _read_route(self, prop):
        return int((await self._ask(route_query(prop), route_pattern(prop)))[1])

    async def _signal_query(self):
        extra = len(self.list_children("input")) - 1
        return await self._ask(ESC + "0LS", rf"IN00\s+[01](?:\*[01]){{{extra}}}")

    async def _hdcp_query(self, kind, n):
        side = "I" if kind == "input" else "O"
        return await self._ask(f"{ESC}{side}{n}HDCP", rf"Hdcp{side}\s*0*{n}\*[0-2]")

    async def _mute_query(self, n, value=None):
        pattern = rf"Vmt\s*0*{n}\*([0-2])"
        if value is not None:
            await self._write(f"{n}*{value}B", pattern)
        result = await self._ask(f"{n}*B", pattern)
        self._confirm(value, int(result[1]))
        return self.get_child_state("output", n).get("mute_mode")

    async def _names(self):
        for kind, side in (("input", "I"), ("output", "O")):
            for n in self.list_children(kind):
                await self._ask(f"{ESC}{side}{n}VNAM", rf"Vnam{side}\s*0*{n}\*.*")
        for n in self.list_children("audio_source"):
            await self._ask(f"{ESC}I{n}ANAM", rf"AnamI\s*0*{n}\*.*")
        for n in range(1, 17):
            await self._ask(f"{ESC}2*{n}PNAM", rf"Pnam2\*0*{n},.*")
        for n, (_, f, _) in MIXES.items():
            await self._limits(n, f)

    async def _limits(self, n, fader):
        await self._ask(f"{ESC}L{fader}GRPM", rf"GrpmL0*{fader}\*[+-]?\d+\*[+-]?\d+")
        return self.get_child_state("mix", n)

    async def _inventory(self, full):
        async with self._sync, self._io:
            await self._global("power_mode")
            if self.get_state("power_mode") != POWER[0]:
                return
            if full or self._needs_inventory:
                await self._names()
                self._needs_inventory = False
            await self._status()

    async def _input_status(self, n):
        await self._ask(f"{n}*\\", rf"Vtyp\s*0*{n}\*[0-3]")
        await self._hdcp_query("input", n)

    async def _status(self):
        for prop in ROUTES:
            await self._read_route(prop)
        await self._signal_query()
        for n in self.list_children("input"):
            await self._input_status(n)
        for n in self.list_children("output"):
            await self._hdcp_query("output", n)
            await self._mute_query(n)
        for kind in ("mix", "audio_channel"):
            for n in self.list_children(kind):
                for prop in ("level", "muted"):
                    await self._audio(kind, n, prop)
        await self._global("auto_switch")
        await self._global("freeze")

    async def refresh_children(self):
        await self._inventory(True)
        return {
            "message": "Read the scaler’s source names, saved presets and room settings."
        }

    async def poll(self):
        await self._inventory(False)

    async def _audio(self, kind, n, prop, value=None):
        if kind == "mix":
            group = MIXES[n][1 if prop == "level" else 2]
            head, tail, prefix = f"{ESC}D{group}", "GRPM", rf"GrpmD0*{group}"
        else:
            op = "G" if prop == "level" else "M"
            oid = 59999 + n
            head, tail, prefix = f"{ESC}{op}{oid}", "AU", f"Ds{op}{oid}"
        pattern = prefix + r"\*([+-]?\d+)"
        if value is not None:
            await self._write(head + "*" + str(value) + tail, pattern)
        raw = int((await self._ask(head + tail, pattern))[1])
        self._confirm(value, raw)
        return raw / 10 if prop == "level" else bool(raw)

    def _id(self, kind, raw):
        if not str(raw).isdecimal():
            raise CommandRefusedError(
                "Choose a control from the scaler’s reported list."
            )
        ident = int(raw)
        if not self.is_child_registered(kind, ident):
            raise CommandRefusedError(
                f"This scaler has no such {kind.replace('_', ' ')}."
            )
        return ident

    async def _bridge_status(self, desired=None):
        if self.get_state("power_mode") == POWER[1]:
            raise CommandRefusedError(
                "Output 1B is off in Standby. Select Full power or Standby with TP power."
            )
        start = int((await self._ask(ESC + "MD", r"Pmd(\d+)"))[1])
        if start != 2000:
            raise CommandRefusedError(
                f"Output 1B uses TCP {start + 3}. Through a bridge uses 2003. "
                "Check the site's UART start point in PCS before changing it."
            )
        mode = (await self._ask(ESC + "O2LRPT", r"LrptO\s*0?2\*([12])"))[1]
        if mode != "1":
            raise CommandRefusedError(
                "Output 1B is set to control the scaler. Select TCP/IP UART insertion "
                "for output 1B in PCS to control the far-end equipment."
            )
        current = await self._bridge_line()
        if desired is not None and current != desired:
            await self._write(ESC + "9*" + ",".join(desired) + "CP", LINE_REPORT)
            current = await self._bridge_line()
            if current != desired:
                raise CommandRefusedError(
                    "The scaler did not confirm the requested serial settings for output 1B."
                )
        baud, parity, bits, stop = current
        status = f"Output 1B · TCP 2003 · {baud} {bits}{parity}{stop}. Request status from the far-end equipment to check the cable."
        self.set_state("bridge_status", status)
        return status

    async def _bridge_line(self):
        reply = await self._ask(ESC + "9CP", LINE_REPORT)
        return (reply[1], reply[2][0].upper(), reply[3], reply[4])

    async def _check_bridge(self, desired=None):
        try:
            return await self._bridge_status(desired)
        except (CommandRefusedError, ConnectionError) as exc:
            self.set_state("bridge_status", str(exc))
            raise

    async def prepare_bridge_port(self, port_id, params):
        if port_id != "serial:1":
            raise CommandRefusedError("Choose the far-end RS-232 port on output 1B.")
        async with self._io:
            await self._check_bridge(serial_format(params))

    async def set_device_setting(self, key, value):
        if key != "auto_switch":
            raise CommandRefusedError("Unknown scaler setting.")
        return await self.send_command("set_auto_switch", {"mode": value})

    async def send_command(self, command, params=None):
        if command == "read_serial_bridge":
            async with self._io:
                return await self._check_bridge()
        if command == "refresh":
            return await self.refresh_children()
        params = params or {}
        action, _, key = command.partition("_")
        async with self._io:
            if (
                command == "present_source"
                and self.get_state("power_mode") in POWER[1:]
            ):
                self._id("input", params["source"])
                await self._global("power_mode", 0)
            if key != "power_mode" and self.get_state("power_mode") != POWER[0]:
                raise CommandRefusedError(
                    "The scaler must be at full power for this control."
                )
            return await self._operate(command, key, action, params)

    async def _operate(self, command, key, action, params):
        if key in GLOBAL:
            return await self._global_command(key, action, params)
        if key in ROUTES or command == "present_source":
            return await self._route_command(key, action, params)
        if command == "recall_picture_preset":
            n = self._id("preset", params["preset"])
            await self._write(f"2*{n}.", rf"2Rpr0*{n}")
            return n
        return await self._child_command(key, action, params)

    async def _global_command(self, key, action, params):
        value = None
        if action == "set":
            if key == "freeze":
                value = switch(params["frozen"])
            else:
                value = choice(params["mode"], POWER if key == "power_mode" else AUTO)
        return await self._global(key, value)

    async def _route_command(self, key, action, params):
        if action == "read":
            return await self._read_route(key)
        n = self._id(
            "audio_source" if key == "audio_input" else "input", params["source"]
        )
        if action == "present":
            await self._write(f"{n}*1!", rf"In\s*0*{n}\*0?1\s*All")
            for prop in ("video_input", "audio_input"):
                self._confirm(n, await self._read_route(prop))
            return n
        wire = f"{ESC}{n}LOUT" if key == "loop_input" else f"{n}*1{ROUTES[key][0]}"
        await self._write(wire, route_pattern(key))
        result = await self._read_route(key)
        self._confirm(n, result)
        return result

    async def _child_command(self, key, action, params):
        if key.endswith("_hdcp"):
            kind = key.split("_")[0]
            n = self._id(kind, params[kind])
            await self._hdcp_query(kind, n)
            return self.get_child_state(kind, n).get("hdcp")
        if key == "input_signal":
            n = self._id("input", params["input"])
            await self._signal_query()
            return self.get_child_state("input", n).get("signal")
        if key == "output_mute":
            n = self._id("output", params["output"])
            return await self._mute_query(
                n, choice(params["mode"], MUTES) if action == "set" else None
            )
        return await self._audio_command(key, action, params)

    async def _audio_command(self, key, action, params):
        kind, _, word = key.rpartition("_")
        if kind not in ("mix", "room", "audio_channel") or word not in (
            "volume",
            "mute",
        ):
            raise CommandRefusedError("Unknown scaler command.")
        n = 4 if kind == "room" else self._id(kind, params[kind])
        target = "mix" if kind == "room" else kind
        prop = "level" if word == "volume" else "muted"
        value = None
        if action == "set":
            value = await self._audio_value(target, n, prop, params[prop])
        return await self._audio(target, n, prop, value)

    async def _audio_value(self, kind, n, prop, value):
        if prop == "muted":
            return switch(value)
        if kind == "audio_channel":
            return tenths(value, -100, 0)
        limits = await self._limits(n, MIXES[n][1])
        return tenths(value, limits["minimum"], limits["maximum"])
