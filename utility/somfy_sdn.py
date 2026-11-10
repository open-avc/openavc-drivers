"""Somfy Digital Network wired motor bus.

DOC155888/004 defines request/reply feedback, except for local-button address
announcements. Python handles inverted binary frames, checksums, multiple
motor addresses and the single outstanding request required by untagged ACKs.
Per-motor settings use child commands and readback: device_settings addresses
flat device state, not a motor on a shared bus. Commission limits and direction
with Somfy tools before use. Native-IP Synergy is a separate protocol.

Manufacturer Frame Builder worked examples in DOC155888/000 and /002 confirm
little-endian movement position and a big-endian, uninverted sum of the
inverted header/data bytes. Firmware reference is big-endian per /004 p.17.
"""

from __future__ import annotations

import asyncio
import json
import re
import struct
import time
from dataclasses import dataclass
from typing import Any

from openavc.drivers.base import BaseDriver
from openavc.transport.frame_parsers import CallableFrameParser


NODE_TYPES = {
    2: "30 mm DC RS485",
    6: "Glydea RS485",
    7: "50 mm AC RS485",
    8: "50 mm DC RS485",
    9: "40 mm AC RS485",
}
REPLIES = {
    0x40: 0x60,
    0x41: 0x61,
    0x74: 0x75,
    0x45: 0x65,
    0x4C: 0x6C,
    0x27: 0x37,
    0x25: 0x35,
    0x23: 0x33,
    0x26: 0x36,
    0x0C: 0x0D,
    0x0E: 0x0F,
}
MIN_DATA = {
    0x60: 0,
    0x61: 4,
    0x75: 6,
    0x65: 16,
    0x6C: 12,
    0x37: 5,
    0x35: 4,
    0x33: 3,
    0x36: 6,
    0x0D: 5,
    0x0F: 4,
    0x7F: 0,
    0x6F: 1,
}
NACKS = {
    1: "Value out of range",
    0x10: "Unknown message",
    0x11: "Wrong message length",
    0xFF: "Motor busy",
}
STATUS = {0: "stopped", 1: "running", 2: "blocked", 3: "network_locked"}
DIRECTION = {0: "down", 1: "up", 0xFF: "unknown"}
CAUSE = {
    0: "target_reached",
    1: "command",
    2: "identify",
    0x20: "obstacle",
    0x21: "overcurrent",
    0x22: "thermal",
    0x30: "runtime_exceeded",
    0x32: "adjustment_timeout",
    0xFF: "power_on_or_reset",
}
SIMPLE = {
    "up": (3, b"\x01\x00\x00\x00"),
    "down": (3, b"\x00" * 4),
    "stop": (2, b"\x00"),
    "wink": (5, b""),
    "read_speed": (0x23, b""),
    "read_lock": (0x26, b""),
    "reset_motor": (0x1F, b"\x00"),
    "clear_groups": (0x1F, b"\x01"),
    "clear_ips": (0x1F, b"\x15"),
    "clear_locks": (0x1F, b"\x17"),
}
MOVE_FUNCTIONS = {
    "set_position": 4,
    "recall_ip": 2,
    "set_tilt": 0x0F,
    "set_tilt_degrees": 0x10,
    "set_position_tilt": 0x0C,
    "set_position_tilt_degrees": 0x0D,
}
IP_FUNCTIONS = {
    "save_ip": 1,
    "set_ip": 3,
    "delete_ip": 0,
    "space_ips": 4,
    "save_ip_tilt": 5,
    "set_ip_tilt": 0x0A,
    "set_ip_tilt_degrees": 0x0B,
}


class MotorRefusal(ValueError):
    """A valid NACK, distinct from a motor that did not answer."""


def node_id(value: Any) -> str:
    text = str(value).strip().replace(":", "").upper()
    if not re.fullmatch(r"[0-9A-F]{6}", text):
        raise ValueError("Use the six hexadecimal digits printed as the motor Node ID.")
    return text


@dataclass(frozen=True)
class Frame:
    message: int
    source: str
    destination: str
    data: bytes = b""
    node_type: int = 0
    ack: bool = False

    def encode(self) -> bytes:
        if len(self.data) > 21:
            raise ValueError("An SDN frame permits at most 21 data bytes.")
        header = bytes(
            (
                self.message,
                (len(self.data) + 11) | (128 if self.ack else 0),
                self.node_type,
            )
        )
        addresses = int(self.source, 16).to_bytes(3, "little") + int(
            self.destination, 16
        ).to_bytes(3, "little")
        body = bytes(value ^ 255 for value in header + addresses + self.data)
        return body + sum(body).to_bytes(2, "big")


def candidate_length(buffer: bytes, offset: int) -> int:
    flags = buffer[offset + 1] ^ 255
    length = flags & 0x3F
    return length if not flags & 0x40 and 11 <= length <= 32 else 0


def checksum_valid(frame: bytes) -> bool:
    return sum(frame[:-2]) == int.from_bytes(frame[-2:], "big")


def parse_frame(buffer: bytes) -> tuple[bytes | None, bytes]:
    """Resynchronize on length/checksum and retain a possible incomplete prefix."""
    incomplete = []
    for offset in range(max(0, len(buffer) - 1)):
        length = candidate_length(buffer, offset)
        if not length:
            continue
        if offset + length > len(buffer):
            incomplete.append(offset)
            continue
        frame = buffer[offset : offset + length]
        if checksum_valid(frame):
            return frame, buffer[offset + length :]
    return None, buffer[incomplete[0] :] if incomplete else buffer[-1:]


def unpack_frame(frame: bytes) -> tuple[int, int, str, str, bytes]:
    body = bytes(value ^ 0xFF for value in frame[:-2])
    source = f"{int.from_bytes(body[3:6], 'little'):06X}"
    destination = f"{int.from_bytes(body[6:9], 'little'):06X}"
    return body[0], body[2], source, destination, body[9:]


def move_data(command: str, params: dict) -> bytes:
    position = int(params.get("position", 0))
    if command == "recall_ip":
        position = int(params["ip"]) - 1  # GET/SET use 1..16; CTRL uses 0..15.
    data = struct.pack("<BHB", MOVE_FUNCTIONS[command], position, 0)
    if "angle" in params:
        data += struct.pack("<h", int(params["angle"]))
    return data


def ip_data(command: str, params: dict) -> bytes:
    position = int(params.get("position", params.get("count", 0)))
    data = struct.pack(
        "<BBH", IP_FUNCTIONS[command], int(params.get("ip", 1)), position
    )
    if "angle" in params:
        data += struct.pack("<h", int(params["angle"]))
    return data


def setting_message(command: str, params: dict) -> tuple[int, bytes]:
    builders = {
        "set_label": lambda: (
            0x55,
            str(params["label"]).encode("ascii").ljust(16, b" "),
        ),
        "set_group": lambda: (
            0x51,
            bytes([int(params["slot"])])
            + int(node_id(params["group"]), 16).to_bytes(3, "little"),
        ),
        "read_group": lambda: (0x41, bytes([int(params["slot"])])),
        "read_ip": lambda: (0x25, bytes([int(params["ip"])])),
        "set_speed": lambda: (
            0x13,
            bytes(int(params[k]) for k in ("up_speed", "down_speed", "slow_speed")),
        ),
        "set_lock": lambda: (
            0x16,
            bytes([int(params["locked"]), int(params["priority"])]),
        ),
        "save_lock": lambda: (0x16, bytes([3 if params["saved"] else 4, 0])),
        "read_local_ui": lambda: (0x27, bytes([int(params["ui"])])),
        "set_local_ui": lambda: (
            0x17,
            bytes(
                [int(not params["enabled"]), int(params["ui"]), int(params["priority"])]
            ),
        ),
    }
    return builders[command]()


def command_message(command: str, params: dict) -> tuple[int, bytes]:
    if command in SIMPLE:
        return SIMPLE[command]
    if command in MOVE_FUNCTIONS:
        return 3, move_data(command, params)
    if command in IP_FUNCTIONS:
        return 0x15, ip_data(command, params)
    return setting_message(command, params)


def position_state(data: bytes) -> dict:
    result = {"position_pulses": int.from_bytes(data[:2], "little")}
    if data[2] <= 100:
        result["position"] = data[2]
    if data[3] <= 100:
        result["tilt_percent"] = data[3]
    if data[4] == 0xFF or 1 <= data[4] <= 16:
        result["current_ip"] = 0 if data[4] == 0xFF else data[4]
    angle = int.from_bytes(data[7:9], "little") if len(data) >= 9 else 0x8000
    if angle <= 180:
        result["tilt_degrees"] = angle
    return result


def status_state(data: bytes) -> dict:
    return {
        "status": STATUS.get(data[0], f"0x{data[0]:02X}"),
        "direction": DIRECTION.get(data[1], f"0x{data[1]:02X}"),
        "command_source": {0: "internal", 1: "SDN", 2: "local_control"}.get(
            data[2], f"0x{data[2]:02X}"
        ),
        "cause": CAUSE.get(data[3], f"0x{data[3]:02X}"),
    }


def lock_state(data: bytes) -> dict:
    return {
        "locked": bool(data[0]),
        "lock_owner": f"{int.from_bytes(data[1:4], 'little'):06X}",
        "lock_priority": data[4],
        "lock_saved": bool(data[5]),
    }


def simple_reply_state(message: int, data: bytes) -> dict:
    parsers = {
        0x0D: position_state,
        0x0F: status_state,
        0x36: lock_state,
        0x65: lambda d: {
            "label": d[:16].decode("ascii", errors="replace").rstrip(" \x00")
        },
        0x6C: lambda d: {"serial_number": d[:12].decode("ascii", errors="replace")},
        0x75: lambda d: {
            "firmware": f"{int.from_bytes(d[:3], 'big')}{chr(d[3])}{d[4]:02d}"
        },
        0x33: lambda d: dict(zip(("up_speed", "down_speed", "slow_speed"), d[:3])),
    }
    parser = parsers.get(message)
    return parser(data) if parser else {}


class SomfySDNDriver(BaseDriver):
    DRIVER_INFO = {
        "id": "somfy_sdn",
        "name": "Somfy SDN Shades",
        "manufacturer": "Somfy",
        "category": "utility",
        "version": '1.0.2',
        "author": "OpenAVC",
        "description": "Control Somfy Digital Network shades over RS485 or a transparent serial network "
        "bridge, with individual position and fault feedback.",
        "source_url": "https://service.somfy.com/downloads/bui_v4/sdn-integration-guide-rev.004.pdf",
        "transport": "tcp",
        "transports": ["tcp", "serial"],
        "protocols": ["somfy_sdn"],
        "simulated": True,
        "verified": False,
        "min_platform_version": "0.36.0",
        "tags": ["shades", "blinds", "rs485"],
        "web_ui": False,
        "compatible_models": [
            {
                "manufacturer": "Somfy",
                "models": ["Sonesse 30 RS485 24V DC (1241144)"],
                "confidence": "untested",
                "notes": "Wired SDN; limits and direction commissioned with Somfy tools. "
                "No native-IP Synergy or PoE control.",
            }
        ],
        "default_config": {
            "host": "",
            "poll_interval": 5,
            "controller_address": "FFFFFE",
            "motors": [],
            "baudrate": 4800,
            "bytesize": 8,
            "parity": "O",
            "stopbits": 1,
        },
        "config_schema": {
            "host": {"type": "string", "label": "Serial Bridge IP Address"},
            "port": {
                "type": "integer",
                "label": "Serial Bridge TCP Port",
                "required": True,
                "min": 1,
                "max": 65535,
            },
            "poll_interval": {
                "type": "integer",
                "label": "Poll Interval (seconds)",
                "default": 5,
                "min": 0,
            },
            "controller_address": {
                "type": "string",
                "label": "Controller Node ID",
                "default": "FFFFFE",
                "required": True,
                "help": "Unique six-digit hexadecimal address for this "
                "controller. Do not use a motor address, 000000 "
                "or FFFFFF.",
            },
            "motors": {
                "type": "table",
                "label": "Shades",
                "row_label": "shade",
                "help": "Enter printed motor Node IDs to ensure all shades are "
                "included. A bus scan also adds motors that answer; "
                "collisions may hide some.",
                "columns": {
                    "address": {
                        "type": "string",
                        "label": "Motor Node ID",
                        "required": True,
                    },
                    "label": {"type": "string", "label": "Project Label"},
                },
            },
        },
        "state_variables": {
            "last_error": {
                "type": "string",
                "label": "Last Error",
                "cloud_priority": "high",
            },
            "shade_count": {"type": "integer", "label": "Shades"},
            "discovery_note": {"type": "string", "label": "Discovery Result"},
        },
        "child_entity_types": {
            "shade": {
                "label": "Shade",
                "label_plural": "Shades",
                "id_format": {"type": "string", "max_length": 6},
                "label_field": "label",
                "summary_fields": ["position", "status", "last_error"],
                "state_variables": {
                    "label": {"type": "string", "label": "Motor Label"},
                    "node_type": {"type": "string", "label": "Motor Family"},
                    "serial_number": {
                        "type": "string",
                        "label": "Serial Number",
                        "cloud_priority": "low",
                    },
                    "firmware": {
                        "type": "string",
                        "label": "Firmware",
                        "cloud_priority": "low",
                    },
                    "status": {
                        "type": "string",
                        "label": "Motor Status",
                        "cloud_priority": "high",
                    },
                    "direction": {"type": "string", "label": "Direction"},
                    "command_source": {"type": "string", "label": "Command Source"},
                    "cause": {
                        "type": "string",
                        "label": "Last Movement Cause",
                        "cloud_priority": "high",
                    },
                    "last_error": {
                        "type": "string",
                        "label": "Last Error",
                        "cloud_priority": "high",
                    },
                    "lock_owner": {"type": "string", "label": "Network Lock Owner"},
                    "groups": {"type": "string", "label": "Group Membership"},
                    "intermediate_positions": {
                        "type": "string",
                        "label": "Saved Positions",
                    },
                    "local_controls": {"type": "string", "label": "Local Controls"},
                    "position": {
                        "type": "integer",
                        "label": "Position",
                        "min": 0,
                        "max": 100,
                        "control": True,
                        "unit": "%",
                        "cloud_priority": "high",
                    },
                    "position_pulses": {
                        "type": "integer",
                        "label": "Position in Pulses",
                        "min": 0,
                        "max": 65535,
                        "control": False,
                        "cloud_priority": "low",
                    },
                    "tilt_percent": {
                        "type": "integer",
                        "label": "Tilt",
                        "min": 0,
                        "max": 100,
                        "control": True,
                        "unit": "%",
                    },
                    "tilt_degrees": {
                        "type": "integer",
                        "label": "Reported Tilt Angle",
                        "min": 0,
                        "max": 180,
                        "control": False,
                    },
                    "current_ip": {
                        "type": "integer",
                        "label": "Current Intermediate Position",
                        "min": 0,
                        "max": 16,
                        "control": False,
                    },
                    "up_speed": {
                        "type": "integer",
                        "label": "Up Speed",
                        "min": 0,
                        "max": 255,
                        "control": True,
                        "unit": "rpm",
                    },
                    "down_speed": {
                        "type": "integer",
                        "label": "Down Speed",
                        "min": 0,
                        "max": 255,
                        "control": True,
                        "unit": "rpm",
                    },
                    "slow_speed": {
                        "type": "integer",
                        "label": "Slow Speed",
                        "min": 0,
                        "max": 255,
                        "control": True,
                        "unit": "rpm",
                    },
                    "lock_priority": {
                        "type": "integer",
                        "label": "Lock Priority",
                        "min": 0,
                        "max": 255,
                        "control": False,
                    },
                    "locked": {
                        "type": "boolean",
                        "label": "Network Locked",
                        "cloud_priority": "high",
                    },
                    "lock_saved": {"type": "boolean", "label": "Network Lock Saved"},
                },
            }
        },
        "quick_actions": ["up", "down", "stop", "set_position"],
        "commands": {
            "refresh": {'help': 'Reads status, position, identity and supported settings from every '
                        'known shade.',
                        "label": "Refresh Status", "params": {}},
            "discover": {
                "label": "Find Shades",
                "params": {},
                "help": "Adds motors that answer a bus address request. Colliding "
                "replies can hide motors; repeat the scan or enter their "
                "printed Node IDs. Existing shades are retained.",
            },
            "up": {
                'help': 'Moves the selected shade toward its commissioned upper limit. Watch Position '
                'and Motor Status for completion.',
                'sets': {'position': 0},
                "label": "Move Up",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    }
                },
            },
            "down": {
                'help': 'Moves the selected shade toward its commissioned lower limit. Watch Position '
                'and Motor Status for completion.',
                'sets': {'position': 100},
                "label": "Move Down",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    }
                },
            },
            "stop": {
                'help': 'Stops the selected shade at its current position.',
                'sets': {'status': 'stopped'},
                "label": "Stop",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    }
                },
            },
            "wink": {
                "label": "Identify Shade",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    }
                },
                "help": "Identify briefly moves the motor.",
            },
            "set_position": {
                "label": "Set Position",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    },
                    "position": {
                        "type": "integer",
                        "label": "Position (%)",
                        "required": True,
                        "min": 0,
                        "max": 100,
                    },
                },
                "help": "0% is the upper limit and 100% is the lower limit. The "
                "motor must already have its limits set in Somfy tools.",
                "sets": {"position": "{position}"},
            },
            "recall_ip": {
                'help': 'Moves the selected shade to a stored intermediate position. The position must '
                'already be saved on that motor.',
                "label": "Recall Intermediate Position",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    },
                    "ip": {
                        "type": "integer",
                        "label": "Intermediate Position",
                        "required": True,
                        "min": 1,
                        "max": 16,
                    },
                },
            },
            "set_label": {
                'help': 'Saves a label on the motor and updates its name in the shade list.',
                "label": "Set Motor Label",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    },
                    "label": {
                        "type": "string",
                        "label": "Motor Label",
                        "required": True,
                        "pattern": "[ -~]{1,16}",
                    },
                },
                "sets": {"label": "{label}"},
            },
            "set_group": {
                'help': 'Saves the group Node ID in the selected motor group slot, replacing the '
                'previous assignment in that slot.',
                "label": "Assign Group",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    },
                    "slot": {
                        "type": "integer",
                        "label": "Group Slot",
                        "required": True,
                        "min": 0,
                        "max": 15,
                    },
                    "group": {
                        "type": "string",
                        "label": "Group Node ID",
                        "required": True,
                        "pattern": "[0-9A-Fa-f]{6}",
                    },
                },
            },
            "read_group": {
                'help': "Reads the selected group slot into the shade's Group Membership feedback.",
                'query_for': 'groups',
                "label": "Read Group",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    },
                    "slot": {
                        "type": "integer",
                        "label": "Group Slot",
                        "required": True,
                        "min": 0,
                        "max": 15,
                    },
                },
            },
            "read_ip": {
                'help': "Reads the selected intermediate-position slot into the shade's Saved Positions "
                'feedback without moving the shade.',
                'query_for': 'intermediate_positions',
                "label": "Read Intermediate Position",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    },
                    "ip": {
                        "type": "integer",
                        "label": "Intermediate Position",
                        "required": True,
                        "min": 1,
                        "max": 16,
                    },
                },
            },
            "save_ip": {
                'help': 'Saves the current shade position in the selected intermediate-position slot, '
                'replacing its previous position.',
                "label": "Save Current Position",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    },
                    "ip": {
                        "type": "integer",
                        "label": "Intermediate Position",
                        "required": True,
                        "min": 1,
                        "max": 16,
                    },
                },
            },
            "set_ip": {
                'help': 'Saves the requested percentage in the selected intermediate-position slot '
                'without moving the shade. Zero is the upper limit and 100 is the lower limit.',
                "label": "Set Intermediate Position",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    },
                    "ip": {
                        "type": "integer",
                        "label": "Intermediate Position",
                        "required": True,
                        "min": 1,
                        "max": 16,
                    },
                    "position": {
                        "type": "integer",
                        "label": "Position (%)",
                        "required": True,
                        "min": 0,
                        "max": 100,
                    },
                },
            },
            "delete_ip": {
                'help': 'Erases the selected intermediate-position slot from the motor.',
                "label": "Delete Intermediate Position",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    },
                    "ip": {
                        "type": "integer",
                        "label": "Intermediate Position",
                        "required": True,
                        "min": 1,
                        "max": 16,
                    },
                },
                "confirm": "Delete this saved intermediate position?",
            },
            "space_ips": {
                'help': 'Replaces the first selected number of intermediate positions with evenly '
                'spaced positions between the motor limits.',
                "label": "Space Intermediate Positions",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    },
                    "count": {
                        "type": "integer",
                        "label": "Number of Positions",
                        "required": True,
                        "min": 1,
                        "max": 16,
                    },
                },
                "confirm": "Overwrite the first selected number of intermediate "
                "positions with evenly spaced positions?",
            },
            "read_speed": {
                'help': "Reads the motor's up, down and adjustment speeds. Available on supported DC "
                'motors.',
                'query_for': 'up_speed',
                "label": "Read Motor Speeds",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    }
                },
            },
            "read_lock": {
                'help': "Reads the motor's network lock, owner, priority and saved-lock state.",
                'query_for': 'locked',
                "label": "Read Network Lock",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    }
                },
            },
            "set_speed": {
                'sets': {'up_speed': '{up_speed}', 'down_speed': '{down_speed}', 'slow_speed': '{slow_speed}'},
                "label": "Set Motor Speeds",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    },
                    "up_speed": {
                        "type": "integer",
                        "label": "Up Speed (rpm)",
                        "required": True,
                        "min": 0,
                        "max": 255,
                    },
                    "down_speed": {
                        "type": "integer",
                        "label": "Down Speed (rpm)",
                        "required": True,
                        "min": 0,
                        "max": 255,
                    },
                    "slow_speed": {
                        "type": "integer",
                        "label": "Slow Speed (rpm)",
                        "required": True,
                        "min": 0,
                        "max": 255,
                    },
                },
                "help": "DC motors only. Use the speed range in your motor "
                "specification; unsupported speeds are refused by the motor. "
                "Sonesse 30 RS485 24V DC 1241144 specifies 10–28 rpm.",
            },
            "set_lock": {
                'sets': {'locked': '{locked}'},
                "label": "Set Network Lock",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    },
                    "locked": {"type": "boolean", "label": "Locked", "required": True},
                    "priority": {
                        "type": "integer",
                        "label": "Priority",
                        "required": True,
                        "min": 0,
                        "max": 255,
                    },
                },
                "help": "Locks or unlocks movement at the requested priority. Changing "
                "an existing lock requires equal or higher priority.",
            },
            "save_lock": {
                'help': 'Chooses whether the motor keeps its network lock through a power loss.',
                'sets': {'lock_saved': '{saved}'},
                "label": "Save Network Lock Across Power Loss",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    },
                    "saved": {
                        "type": "boolean",
                        "label": "Save Lock",
                        "required": True,
                    },
                },
            },
            "read_local_ui": {
                'help': 'Reads whether the selected local control is enabled and its lock priority into '
                'Local Controls feedback.',
                'query_for': 'local_controls',
                "label": "Read Local Control",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    },
                    "ui": {
                        "type": "enum",
                        "label": "Local Control",
                        "required": True,
                        "values": [
                            {"value": 1, "label": "Dry Contact"},
                            {"value": 2, "label": "Motor Button"},
                            {"value": 3, "label": "Local Radio"},
                            {"value": 4, "label": "Touch Motion"},
                            {"value": 5, "label": "LEDs"},
                        ],
                    },
                },
            },
            "set_local_ui": {
                'help': 'Enables or disables the selected local control, or all local controls, at the '
                'requested priority. Availability depends on the motor.',
                "label": "Set Local Control",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    },
                    "ui": {
                        "type": "enum",
                        "label": "Local Control",
                        "required": True,
                        "values": [
                            {"value": 0, "label": "All Local Controls"},
                            {"value": 1, "label": "Dry Contact"},
                            {"value": 2, "label": "Motor Button"},
                            {"value": 3, "label": "Local Radio"},
                            {"value": 4, "label": "Touch Motion"},
                            {"value": 5, "label": "LEDs"},
                        ],
                    },
                    "enabled": {
                        "type": "boolean",
                        "label": "Enabled",
                        "required": True,
                    },
                    "priority": {
                        "type": "integer",
                        "label": "Priority",
                        "required": True,
                        "min": 0,
                        "max": 255,
                    },
                },
            },
            "set_tilt": {
                "label": "Set Tilt",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    },
                    "angle": {
                        "type": "integer",
                        "label": "Tilt (%)",
                        "required": True,
                        "min": 0,
                        "max": 100,
                    },
                },
                "help": 'Moves a commissioned Venetian blind to the requested percentage tilt.',
                "sets": {"tilt_percent": "{angle}"},
            },
            "set_tilt_degrees": {
                "label": "Set Tilt in Degrees",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    },
                    "angle": {
                        "type": "integer",
                        "label": "Target Angle (degrees)",
                        "required": True,
                        "min": -90,
                        "max": 90,
                    },
                },
                "help": 'Moves a commissioned Venetian blind to the requested angle. Targets use -90 to '
                '+90 degrees; feedback reports the corresponding angle on a 0 to 180 scale.',
            },
            "set_position_tilt": {
                'help': 'Moves a commissioned Venetian blind to the requested percentage position and '
                'tilt. Position zero is the upper limit and 100 is the lower limit.',
                'sets': {'position': '{position}', 'tilt_percent': '{angle}'},
                "label": "Set Position and Tilt",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    },
                    "position": {
                        "type": "integer",
                        "label": "Position (%)",
                        "required": True,
                        "min": 0,
                        "max": 100,
                    },
                    "angle": {
                        "type": "integer",
                        "label": "Tilt (%)",
                        "required": True,
                        "min": 0,
                        "max": 100,
                    },
                },
            },
            "set_position_tilt_degrees": {
                'help': 'Moves a commissioned Venetian blind to the requested percentage position and '
                'angle. Angle targets use -90 to +90 degrees; angle feedback uses 0 to 180.',
                'sets': {'position': '{position}'},
                "label": "Set Position and Tilt in Degrees",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    },
                    "position": {
                        "type": "integer",
                        "label": "Position (%)",
                        "required": True,
                        "min": 0,
                        "max": 100,
                    },
                    "angle": {
                        "type": "integer",
                        "label": "Target Angle (degrees)",
                        "required": True,
                        "min": -90,
                        "max": 90,
                    },
                },
            },
            "save_ip_tilt": {
                'help': 'Saves the current position and tilt of a Venetian blind in the selected '
                'intermediate-position slot, replacing its previous values.',
                "label": "Save Current Position and Tilt",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    },
                    "ip": {
                        "type": "integer",
                        "label": "Intermediate Position",
                        "required": True,
                        "min": 1,
                        "max": 16,
                    },
                },
            },
            "set_ip_tilt": {
                'help': 'Saves a Venetian blind position and percentage tilt in the selected '
                'intermediate-position slot without moving the blind.',
                "label": "Set Intermediate Position and Tilt",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    },
                    "ip": {
                        "type": "integer",
                        "label": "Intermediate Position",
                        "required": True,
                        "min": 1,
                        "max": 16,
                    },
                    "position": {
                        "type": "integer",
                        "label": "Position (%)",
                        "required": True,
                        "min": 0,
                        "max": 100,
                    },
                    "angle": {
                        "type": "integer",
                        "label": "Tilt (%)",
                        "required": True,
                        "min": 0,
                        "max": 100,
                    },
                },
            },
            "set_ip_tilt_degrees": {
                'help': 'Saves a Venetian blind position and angle in the selected '
                'intermediate-position slot without moving the blind. Angle targets use -90 to '
                '+90 degrees.',
                "label": "Set Intermediate Position and Tilt in Degrees",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    },
                    "ip": {
                        "type": "integer",
                        "label": "Intermediate Position",
                        "required": True,
                        "min": 1,
                        "max": 16,
                    },
                    "position": {
                        "type": "integer",
                        "label": "Position (%)",
                        "required": True,
                        "min": 0,
                        "max": 100,
                    },
                    "angle": {
                        "type": "integer",
                        "label": "Target Angle (degrees)",
                        "required": True,
                        "min": -90,
                        "max": 90,
                    },
                },
            },
            "reset_motor": {
                'help': 'Erases the motor settings. The motor must be commissioned again with Somfy '
                'tools before use.',
                "label": "Factory Reset Motor",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    }
                },
                "confirm": "Reset all settings on this motor? It will need "
                "commissioning in Somfy tools before use.",
            },
            "clear_groups": {
                'help': 'Removes the selected motor from every stored SDN group.',
                "label": "Clear All Groups",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    }
                },
                "confirm": "Remove this motor from all groups?",
            },
            "clear_ips": {
                'help': 'Erases all intermediate positions stored on the selected motor.',
                "label": "Delete All Intermediate Positions",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    }
                },
                "confirm": "Delete every saved intermediate position on this motor?",
            },
            "clear_locks": {
                'help': "Clears the selected motor's local and network locks, including saved locks.",
                "label": "Clear All Locks",
                "params": {
                    "shade": {
                        "type": "child_id",
                        "label": "Shade",
                        "child_type": "shade",
                        "required": True,
                    }
                },
                "confirm": "Clear all local and network locks, including saved "
                "locks, on this motor?",
            },
            "group_up": {
                "label": "Move Group Up",
                "params": {
                    "group": {
                        "type": "string",
                        "label": "Group Node ID",
                        "required": True,
                        "pattern": "[0-9A-Fa-f]{6}",
                    }
                },
                "help": 'Moves the commissioned group toward its upper limits. Individual motor '
                'feedback shows progress; the group command itself has no acknowledgment.',
            },
            "group_down": {
                "label": "Move Group Down",
                "params": {
                    "group": {
                        "type": "string",
                        "label": "Group Node ID",
                        "required": True,
                        "pattern": "[0-9A-Fa-f]{6}",
                    }
                },
                "help": 'Moves the commissioned group toward its lower limits. Individual motor '
                'feedback shows progress; the group command itself has no acknowledgment.',
            },
            "group_stop": {
                "label": "Stop Group",
                "params": {
                    "group": {
                        "type": "string",
                        "label": "Group Node ID",
                        "required": True,
                        "pattern": "[0-9A-Fa-f]{6}",
                    }
                },
                "help": 'Stops the commissioned group. Individual motor feedback confirms the stops; '
                'the group command itself has no acknowledgment.',
            },
            "group_set_position": {
                "label": "Set Group Position",
                "params": {
                    "group": {
                        "type": "string",
                        "label": "Group Node ID",
                        "required": True,
                        "pattern": "[0-9A-Fa-f]{6}",
                    },
                    "position": {
                        "type": "integer",
                        "label": "Position (%)",
                        "required": True,
                        "min": 0,
                        "max": 100,
                    },
                },
                "help": 'Moves the commissioned group to the requested percentage: zero is the upper '
                'limit and 100 is the lower limit. Individual motor feedback shows progress; '
                'the group command itself has no acknowledgment.',
            },
        },
        "help": {
            "overview": "Controls wired Somfy SDN motors as individual shades. Position and motor "
            "faults are read from each motor. Motor labels, groups, saved positions, "
            "supported speeds and locks are also available. Commands vary by motor "
            "family and application; a refusal is shown as an error.",
            "setup": "Commission limits, direction and application mode with Somfy Set pro or the "
            "RS485 Setting Tool first. Use one OpenAVC driver per bus. Connect a "
            "transparent RS485 network bridge, enter its IP and configured TCP port, and "
            "set its serial side to 4800 baud, 8 data bits, odd parity, 1 stop bit, no flow "
            "control. Alternatively select a direct serial connection with an "
            "automatic-direction RS485 adapter. USB adapter access must be verified on the "
            "chosen computer; Docker needs the serial device passed through and appliance "
            "USB adapters are not yet proven. Outbound TCP bridges work across install "
            "types without inbound ports. Enter motor Node IDs from their labels; use Find "
            "Shades to supplement the roster. Polling is required for position feedback. "
            "This driver does not configure travel limits or speak the Synergy/PoE API.",
        },
    }

    HEALTH_INTERVAL_S = 20.0
    HEALTH_TIMEOUT_S = 60.0
    HEALTH_MAX_FAILURES = 2
    REPLY_TIMEOUT_S = 1.0

    def __init__(self, device_id, config, state, events):
        self._source = node_id(config.get("controller_address", "FFFFFE"))
        if self._source in ("000000", "FFFFFF"):
            raise ValueError("Controller Node ID must not be 000000 or FFFFFF.")
        self._bus_lock = asyncio.Lock()
        self._pending = None
        self._activity = 0.0
        self._answered = {}
        self._types = {}
        self._tables = {}
        self._identity_read = set()
        self._uncertain = set()
        super().__init__(device_id, config, state, events)
        for row in config.get("motors", []):
            address = node_id(row["address"])
            if address in ("000000", "FFFFFF", self._source):
                raise ValueError(
                    "A motor address must be unique and not a broadcast address."
                )
            self.register_child(
                "shade", address, {"label": str(row.get("label") or address)}
            )
        self.HEALTH_TIMEOUT_S = max(10.0, len(self.list_children("shade")) * 2.0 + 10.0)

    def _receive_frames(self, buffer):
        # Include partial and damaged frames when observing bus activity.
        self._activity = max(self._activity, time.monotonic())
        return parse_frame(buffer)

    def _create_frame_parser(self):
        return CallableFrameParser(self._receive_frames)

    def _resolve_delimiter(self):
        return None

    def _transport_kwargs(self, transport_type, kwargs):
        if transport_type == "serial":
            kwargs.update(
                baudrate=4800,
                bytesize=8,
                parity="O",
                stopbits=1,
                rtscts=False,
                xonxoff=False,
            )
        return kwargs

    async def _close_session(self):
        if self._pending and not self._pending["future"].done():
            self._pending["future"].cancel()
        self._pending = None
        self._answered.clear()
        self._uncertain.clear()
        self._identity_read.clear()

    async def _initial_sync(self):
        if not self.list_children("shade"):
            await self.refresh_children()
        await self._liveness_probe()
        if not self.config.get("poll_interval", 0):
            await self.poll()

    async def _send(self, request):
        while time.monotonic() < self._activity + 0.025:
            await asyncio.sleep(self._activity + 0.025 - time.monotonic())
        frame = request.encode()
        await self.transport.send(frame)
        # At 4800/8O1, each byte uses 11 bits. TCP send completion does not
        # mean a bridge has finished sending those bytes on the serial bus.
        self._activity = max(self._activity, time.monotonic() + len(frame) * 11 / 4800)

    async def _request(self, address, message, data=b""):
        async with self._bus_lock:
            if address in self._uncertain:
                # ACK has no command tag. Drain a late ACK behind a distinct
                # address read before another command can accept an ACK.
                await self._exchange(address, 0x40, b"")
                self._uncertain.discard(address)
            return await self._exchange(address, message, data)

    async def _exchange(self, address, message, data):
        future = asyncio.get_running_loop().create_future()
        self._pending = {
            "address": address,
            "message": REPLIES.get(message, 0x7F),
            "request": message,
            "data": data,
            "future": future,
        }
        try:
            await self._send(
                Frame(message, self._source, address, data, ack=message not in REPLIES)
            )
            reply, payload = await asyncio.wait_for(future, self.REPLY_TIMEOUT_S)
            self._answered[address] = time.monotonic()
            self.set_child_state_batch("shade", address, self.child_fault())
            if reply == 0x6F:
                error = NACKS.get(payload[0], f"Motor refusal 0x{payload[0]:02X}")
                self.set_child_state("shade", address, "last_error", error)
                self.set_state("last_error", f"{address}: {error}")
                raise MotorRefusal(error)
            self.set_child_state("shade", address, "last_error", "")
            self._apply_reply(address, reply, (payload, data))
            return payload
        except TimeoutError as exc:
            self._uncertain.add(address)
            self.set_child_state(
                "shade", address, "last_error", "Motor did not answer this request"
            )
            raise TimeoutError(
                f"Shade {address} did not answer 0x{message:02X}; the command may have reached it."
            ) from exc
        finally:
            self._pending = None

    def _valid_reply(self, message, source, destination, data):
        if destination != self._source or source in ("000000", "FFFFFF", self._source):
            return False
        return message in MIN_DATA and len(data) >= MIN_DATA[message]

    def _address_announcement(self, message, types, source):
        if message != 0x60 or source in ("000000", "FFFFFF", self._source):
            return
        if types >> 4 not in NODE_TYPES:
            return
        # POST_NODE_ADDR is also emitted after a local motor-button action.
        # Its source identifies the motor even when another controller is
        # the destination. A bus scan is never treated as a complete roster.
        self.register_child("shade", source, {"label": source})
        self._types[source] = types >> 4
        self.set_state("shade_count", len(self.list_children("shade")))

    def _reply_matches(self, message, source, data):
        pending = self._pending
        if pending is None or pending["future"].done():
            return False
        if pending["address"] != source or message not in (pending["message"], 0x6F):
            return False
        if message in (0x61, 0x35):
            return data[0] == pending["data"][0]
        return True

    async def on_data_received(self, frame):
        self._activity = max(self._activity, time.monotonic())
        message, types, source, destination, data = unpack_frame(frame)
        self._address_announcement(message, types, source)
        if not self._valid_reply(message, source, destination, data):
            return
        if self._reply_matches(message, source, data):
            self._types[source] = types >> 4
            self.set_child_state(
                "shade",
                source,
                "node_type",
                NODE_TYPES.get(types >> 4, f"0x{types >> 4:X}"),
            )
            self._pending["future"].set_result((message, data))

    def _apply_reply(self, address, message, payload):
        data, request_data = payload
        updates = simple_reply_state(message, data)
        if updates:
            self.set_child_state_batch("shade", address, updates)
        tables = self._tables.setdefault(
            address, {"groups": {}, "intermediate_positions": {}, "local_controls": {}}
        )
        table_reply = {
            0x61: lambda: (
                "groups",
                str(data[0]),
                f"{int.from_bytes(data[1:4], 'little'):06X}",
            ),
            0x35: lambda: (
                "intermediate_positions",
                str(data[0]),
                {"position": None if data[3] == 255 else data[3]},
            ),
            0x37: lambda: (
                "local_controls",
                str(request_data[0]),
                {
                    "enabled": data[0] == 0,
                    "owner": f"{int.from_bytes(data[1:4], 'little'):06X}",
                    "priority": data[4],
                },
            ),
        }
        if message in table_reply:
            table, key, value = table_reply[message]()
            if message == 0x35 and len(data) >= 9:
                angle = int.from_bytes(data[7:9], "little")
                value["angle"] = None if angle == 0x8000 else angle
            tables[table][key] = value
            self.set_child_state(
                "shade", address, table, json.dumps(tables[table], sort_keys=True)
            )

    async def refresh_children(self):
        async with self._bus_lock:
            await self._send(Frame(0x40, self._source, "FFFFFF"))
            await asyncio.sleep(0.5)
        count = len(self.list_children("shade"))
        self.HEALTH_TIMEOUT_S = max(10.0, count * 2.0 + 10.0)
        self.set_state("shade_count", count)
        self.set_state(
            "discovery_note",
            f"{count} shades in the roster. Collisions can hide replies; repeat or enter printed Node IDs.",
        )
        return count

    async def _liveness_probe(self):
        addresses = sorted(
            self.list_children("shade"),
            key=lambda key: self._answered.get(key, 0),
            reverse=True,
        )
        for address in addresses:
            try:
                await self._request(address, 0x40)
                return
            except MotorRefusal:
                return  # A motor's refusal still proves the link is alive.
            except TimeoutError:
                continue
        raise TimeoutError(
            "No Somfy motor answered. Check the bus, Node IDs and bridge serial settings."
        )

    async def _read_optional(self, address, message, data=b""):
        try:
            await self._request(address, message, data)
        except MotorRefusal:
            pass  # The motor's documented refusal remains visible.

    async def _poll_motor(self, address):
        await self._request(address, 0x0E)
        await self._read_optional(address, 0x0C)
        await self._read_optional(address, 0x26)
        if address not in self._identity_read:
            self._identity_read.add(address)
            for message in (0x45, 0x74, 0x4C):
                await self._read_optional(address, message)
            if self._types.get(address) in (2, 8):
                await self._read_optional(address, 0x23)

    async def _poll_one(self, address):
        before = self._answered.get(address, 0)
        try:
            await self._poll_motor(address)
        except (MotorRefusal, TimeoutError):
            pass
        answered = self._answered.get(address, 0) > before
        fault = (
            self.child_fault()
            if answered
            else self.child_fault(
                "not_responding", "Check motor power, the bus and its printed Node ID."
            )
        )
        self.set_child_state_batch("shade", address, fault)
        return answered

    async def poll(self):
        if not self.transport or not self.transport.connected:
            return
        answered = False
        for address in self.list_children("shade"):
            answered = await self._poll_one(address) or answered
        self.set_state("shade_count", len(self.list_children("shade")))
        if not answered:
            raise TimeoutError("No Somfy motor answered the status poll.")

    async def _read_indexes(self, address, message, indexes):
        for index in indexes:
            await self._read_optional(address, message, bytes([index]))

    async def _readback(self, address, command, params):
        reads = {
            "set_label": (0x45, b""),
            "set_group": (0x41, bytes([int(params.get("slot", 0))])),
            "set_speed": (0x23, b""),
            "set_lock": (0x26, b""),
            "save_lock": (0x26, b""),
            "set_local_ui": (0x27, bytes([int(params.get("ui", 1))])),
        }
        if command == "set_local_ui" and int(params["ui"]) == 0:
            await self._read_indexes(address, 0x27, range(1, 6))
        elif command in IP_FUNCTIONS:
            indexes = range(1, 17) if command == "space_ips" else [int(params["ip"])]
            await self._read_indexes(address, 0x25, indexes)
        elif command in reads:
            message, data = reads[command]
            await self._read_optional(address, message, data)
        elif command.startswith("clear_"):
            await self._refresh_settings(address, command)
        elif command not in (
            "reset_motor",
            "read_group",
            "read_ip",
            "read_speed",
            "read_lock",
            "read_local_ui",
        ):
            await self._read_optional(address, 0x0E)
            await self._read_optional(address, 0x0C)

    async def _refresh_settings(self, address, command):
        reads = {
            "clear_groups": (0x41, range(16)),
            "clear_ips": (0x25, range(1, 17)),
            "clear_locks": (0x27, range(1, 6)),
        }
        message, indexes = reads[command]
        for index in indexes:
            await self._read_optional(address, message, bytes([index]))
        if command == "clear_locks":
            await self._read_optional(address, 0x26)

    async def _group_command(self, command, params):
        group = node_id(params["group"])
        if group in ("000000", "FFFFFF"):
            raise ValueError("Use a commissioned group ID, not a reserved bus address.")
        message, data = command_message(command.removeprefix("group_"), params)
        async with self._bus_lock:
            await self._send(Frame(message, group, "000000", data))
        return await self.poll()

    async def send_command(self, command, params=None):
        params = params or {}
        if command == "discover":
            return await self.refresh_children()
        if command == "refresh":
            self._identity_read.clear()
            return await self.poll()
        if command.startswith("group_"):
            return await self._group_command(command, params)
        return await self._motor_command(command, params)

    async def _motor_command(self, command, params):
        if command not in self.DRIVER_INFO["commands"]:
            raise ValueError(f"Unknown command: {command}")
        address = node_id(params["shade"])
        if address not in self.list_children("shade"):
            raise ValueError(
                "Choose a shade from the roster or add its Node ID in device settings."
            )
        message, data = command_message(command, params)
        await self._request(address, message, data)
        await self._readback(address, command, params)
