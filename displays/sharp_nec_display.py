"""
OpenAVC Sharp/NEC Large-Format Display Driver (External Control).

Controls Sharp/NEC professional large-format displays over the NEC
"External Control" protocol — SOH-framed ASCII-hex packets on TCP port
7142 (the same protocol runs on RS-232 at 9600 8N1). Every packet is
``SOH + header + STX..ETX message + XOR block-check + CR``; values are
hex digits encoded as ASCII characters. This is the display protocol
(the one NEC's fleet-management software speaks) — NEC/Sharp
*projectors* use a different binary protocol covered by the
``sharp_nec_projector`` driver.

Why Python:
    Binary-ish framing with a computed XOR block-check on every packet
    (the check-code byte is raw binary and can itself equal 0x0D, so
    CR-delimiter framing corrupts packets — a length-aware frame parser
    is required), monitor-ID address encoding (ID 1-100 ->
    0x41..0xA4), hex-pair string decoding for serial/model reads,
    2's-complement temperature decoding, and strict request/response
    pacing (the manual requires the controller to wait for each reply,
    with 15 s / 10 s cooldowns after power / input changes) — none of
    which YAML expresses.

Push vs poll:
    Strictly request/response — the manual defines no unsolicited
    notifications, so polling is correct. The display also drops the
    TCP connection after 15 minutes of silence, so the poll doubles as
    the keep-alive: leave the poll interval well under 15 minutes.

Multi-display:
    One connection can address up to 100 displays daisy-chained by
    Monitor ID (RS-232 out ports chain from the LAN-connected master).
    Each configured ID is a ``display`` child entity; replies carry the
    answering monitor's ID, which routes values to the right child.

Input-code quirk:
    The INPUT opcode (page 00h, code 60h) documents value 4 as
    "HDMI (Set only)" — a legacy set alias every generation accepts —
    while reads report 17 (11h) for HDMI. The driver sets HDMI with 4
    and maps both 4 and 17 back to "HDMI".

Timing:
    Per the manual's Communication Timing section the controller must
    wait for a reply before the next command, and wait 15 s after
    Power On/Off (10 s after an input change) before sending anything
    else. The driver serializes all traffic and enforces those
    cooldowns; expect commands issued right after a power change to be
    delayed accordingly.

Models:
    Generation-independent protocol. Built from the NEC LCD Monitor
    External Control manual Rev.2.4 (G2 — P/V/X/UN series era) and
    cross-checked against the Sharp-era E-series (Exx8) External
    Control manual Rev.1.0 — identical framing, header, check code,
    power procedure (01D6 read / C203D6 set) and core opcodes.

Source:
    NEC "External Control - NEC LCD Monitor" Rev.2.4 (G2):
    https://www.sharpdisplays.eu/p/download/cp/Products/LargeFormatDisplays/Products/CurrentProducts/Shared/Command_Lists/PDF-ExternalControlManual-LCDMonitor-(english).pdf
    Sharp/NEC "External Control - Exx8 Series" Rev.1.0 (continuity check):
    https://sharp-displays.jp.sharp/support/webdl/dl_service/data/display/manual/e658/eu/External_Control_Exx8_Series_EN_Rev1.0.pdf
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from server.drivers.base import BaseDriver
from server.transport.binary_helpers import checksum_xor
from server.transport.frame_parsers import CallableFrameParser
from server.utils.logger import get_logger

log = get_logger(__name__)


DEFAULT_PORT = 7142
MONITOR_ID_MIN = 1
MONITOR_ID_MAX = 100

SOH = 0x01
STX = 0x02
ETX = 0x03

# Header message-type bytes.
TYPE_COMMAND = b"A"
TYPE_COMMAND_REPLY = b"B"
TYPE_GET = b"C"
TYPE_GET_REPLY = b"D"
TYPE_SET = b"E"
TYPE_SET_REPLY = b"F"

# Manual, Communication Timing: wait after receiving the reply before
# the next command.
POWER_COOLDOWN_S = 15.0
INPUT_COOLDOWN_S = 10.0

# Power modes (7.1 / 7.2). 2 and 3 are read-only power-save states.
POWER_MODES = {1: "on", 2: "standby", 3: "suspend", 4: "off"}

# INPUT opcode values (page 00h, code 60h). Value 4 is the documented
# "HDMI (Set only)" alias; reads report 17 for HDMI.
INPUT_SET = {
    "VGA": 1,
    "RGB/HV": 2,
    "DVI": 3,
    "HDMI": 4,
    "VIDEO": 5,
    "S-VIDEO": 7,
    "YPbPr": 12,
    "OPTION": 13,
    "YPbPr2": 14,
    "DPORT": 15,
    "DPORT2": 16,
    "HDMI2": 18,
    "DPORT3": 128,
}
INPUT_NAMES = {code: name for name, code in INPUT_SET.items()}
INPUT_NAMES[17] = "HDMI"  # canonical readback code

ASPECT_SET = {
    "NORMAL": 1,
    "FULL": 2,
    "WIDE": 3,
    "ZOOM": 4,
    "DYNAMIC": 6,
    "1:1": 7,
}
ASPECT_NAMES = {code: name for name, code in ASPECT_SET.items()}

PICTURE_MODE_SET = {
    "sRGB": 1,
    "HIGHBRIGHT": 3,
    "STANDARD": 4,
    "CINEMA": 5,
    "CUSTOM1": 8,
    "CUSTOM2": 9,
}
PICTURE_MODE_NAMES = {code: name for name, code in PICTURE_MODE_SET.items()}

# Self-diagnosis status codes (11.1).
DIAG_CODES = {
    "00": "normal",
    "70": "standby +3.3V abnormality",
    "71": "standby +5V abnormality",
    "72": "panel +12V abnormality",
    "78": "inverter/option-slot +24V abnormality",
    "80": "cooling fan 1 abnormality",
    "81": "cooling fan 2 abnormality",
    "82": "cooling fan 3 abnormality",
    "91": "LED backlight abnormality",
    "A0": "temperature abnormality - shutdown",
    "A1": "temperature abnormality - half brightness",
    "A2": "temperature reached user threshold",
    "B0": "no signal",
    "D0": "proof-of-play buffer reduction",
    "E0": "system error",
}

# Get/Set parameter opcodes: child prop -> (page, code).
OPCODES = {
    "input": (0x00, 0x60),
    "volume": (0x00, 0x62),
    "mute": (0x00, 0x8D),
    "video_mute": (0x10, 0xB6),
    "backlight": (0x00, 0x10),
    "brightness": (0x00, 0x92),
    "contrast": (0x00, 0x12),
    "sharpness": (0x00, 0x8C),
    "aspect": (0x02, 0x70),
    "picture_mode": (0x02, 0x1A),
}
OPCODE_TO_PROP = {pc: prop for prop, pc in OPCODES.items()}

OP_TEMP_SELECT = (0x02, 0x78)
OP_TEMP_READ = (0x02, 0x79)

# Properties read every poll cycle, in wire order.
POLL_PROPS = [
    "input", "volume", "mute", "video_mute", "backlight",
    "brightness", "contrast", "sharpness", "aspect", "picture_mode",
]


def _encode_monitor_id(monitor_id: int) -> bytes:
    """Monitor ID 1-100 -> destination address byte (4.1: 1='A'..100=A4h)."""
    return bytes([0x41 + monitor_id - 1])


def _decode_monitor_id(byte: int) -> Optional[int]:
    if 0x41 <= byte <= 0xA4:
        return byte - 0x40
    return None


def _build_packet(monitor_id: int, msg_type: bytes, message: bytes) -> bytes:
    """Assemble SOH header + message + BCC + CR (4.1-4.4).

    ``message`` is the full STX..ETX block. The BCC XORs every byte
    between SOH and the check code (header-after-SOH + message).
    """
    header = (
        b"0"                                 # reserved
        + _encode_monitor_id(monitor_id)     # destination
        + b"0"                               # source: controller
        + msg_type
        + f"{len(message):02X}".encode("ascii")
    )
    body = header + message
    return bytes([SOH]) + body + bytes([checksum_xor(body)]) + b"\r"


def _hex_word(value: int) -> bytes:
    """A 16-bit value as 4 ASCII-hex bytes (uppercase, per the examples)."""
    return f"{value & 0xFFFF:04X}".encode("ascii")


def _op_bytes(page: int, code: int) -> bytes:
    return f"{page:02X}{code:02X}".encode("ascii")


def _extract_nec_frame(buf: bytes) -> tuple[bytes | None, bytes]:
    """Length-aware frame extraction for the CallableFrameParser.

    Packets end with CR, but the check-code byte is raw binary and can
    itself be 0x0D — so delimiter framing is unsound. Instead: sync on
    SOH, read the message length from the header, and consume exactly
    ``header + message + BCC + CR``. Returns the frame WITHOUT the
    trailing CR (SOH..BCC), plus the remaining buffer.

    CallableFrameParser only persists the buffer when a message is
    returned, so garbage is consumed by returning an EMPTY message
    with the trimmed remainder (the driver ignores frames that short)
    rather than ``(None, trimmed)``.
    """
    if not buf:
        return None, buf
    start = buf.find(bytes([SOH]))
    if start < 0:
        return b"", b""  # all garbage: consume it
    if start > 0:
        return b"", buf[start:]  # drop the garbage prefix
    if len(buf) < 7:
        return None, buf
    try:
        msg_len = int(buf[5:7], 16)
    except ValueError:
        return b"", buf[1:]  # bogus header: resync past this SOH
    total = 7 + msg_len + 2  # header + message + BCC + CR
    if len(buf) < total:
        return None, buf
    return buf[:total - 1], buf[total:]


def _decode_hex_pairs(data: bytes) -> str:
    """Decode the serial/model encoding: ASCII-hex pairs -> byte string
    (12.1 step 2/3). Stops at the first NUL; drops undecodable tails."""
    try:
        raw = bytes.fromhex(data.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return ""
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()


def _child_state_vars() -> dict[str, dict[str, Any]]:
    """State variables for one ``display`` child entity."""
    return {
        "power": {
            "type": "enum",
            "values": ["on", "standby", "suspend", "off"],
            "label": "Power",
            "cloud_priority": "high",
            "control": True,
        },
        "input": {
            "type": "string",
            "label": "Input",
            "cloud_priority": "high",
            "control": True,
        },
        "volume": {
            "type": "integer",
            "label": "Volume",
            "min": 0,
            "max": 100,
            "step": 1,
            "cloud_priority": "high",
            "control": True,
        },
        "mute": {
            "type": "boolean",
            "label": "Mute",
            "cloud_priority": "high",
            "control": True,
        },
        "video_mute": {
            "type": "boolean",
            "label": "Screen Mute",
            "cloud_priority": "low",
        },
        "backlight": {
            "type": "integer",
            "label": "Backlight",
            "min": 0,
            "max": 100,
            "step": 1,
            "cloud_priority": "low",
            "control": True,
        },
        "brightness": {
            "type": "integer",
            "label": "Brightness",
            "min": 0,
            "max": 100,
            "step": 1,
            "cloud_priority": "low",
        },
        "contrast": {
            "type": "integer",
            "label": "Contrast",
            "min": 0,
            "max": 100,
            "step": 1,
            "cloud_priority": "low",
        },
        "sharpness": {
            "type": "integer",
            "label": "Sharpness",
            "min": 0,
            "max": 24,
            "step": 1,
            "cloud_priority": "low",
        },
        "aspect": {"type": "string", "label": "Aspect", "cloud_priority": "low"},
        "picture_mode": {
            "type": "string",
            "label": "Picture Mode",
            "cloud_priority": "low",
        },
        "temperature": {
            "type": "number",
            "label": "Temperature",
            "unit": "°C",
            "cloud_priority": "low",
        },
        "diagnosis": {
            "type": "string",
            "label": "Self-Diagnosis",
            "cloud_priority": "high",
            "help": "'normal', or the first reported fault code and meaning.",
        },
        "model": {"type": "string", "label": "Model", "cloud_priority": "low"},
        "serial": {
            "type": "string",
            "label": "Serial Number",
            "cloud_priority": "low",
        },
    }


def _display_param() -> dict[str, Any]:
    return {
        "type": "child_id",
        "child_type": "display",
        "required": True,
        "label": "Display",
    }


class SharpNECDisplayDriver(BaseDriver):
    """Sharp/NEC large-format display driver (External Control protocol)."""

    DRIVER_INFO = {
        "id": "sharp_nec_display",
        "name": "Sharp/NEC Display (External Control)",
        "manufacturer": "Sharp NEC",
        "category": "display",
        "version": "1.0.2",
        # The connection lifecycle hooks this driver overrides landed in 0.24.0.
        "min_platform_version": "0.24.0",
        "author": "OpenAVC",
        "description": (
            "Controls Sharp/NEC professional large-format displays over "
            "the NEC External Control protocol on TCP 7142. Each Monitor "
            "ID on the daisy chain is a display child entity with power, "
            "input, volume, mutes, picture settings, panel temperature, "
            "and self-diagnosis status. Includes a raw opcode escape "
            "hatch for the full protocol surface."
        ),
        "source_url": (
            "https://www.sharpdisplays.eu/p/download/cp/Products/"
            "LargeFormatDisplays/Products/CurrentProducts/Shared/"
            "Command_Lists/PDF-ExternalControlManual-LCDMonitor-"
            "(english).pdf"
        ),
        "tags": ["display", "signage", "nec", "sharp", "video-wall"],
        "verified": False,
        "simulated": True,
        "protocols": ["nec_external_control"],
        "ports": [7142],
        "transport": "tcp",
        "discovery": {
            # Power-status read (01D6) addressed to Monitor ID 1 — the
            # factory default ID, so nearly every single-display install
            # answers. The reply header starts SOH '0' '0' 'A' 'B'.
            "tcp_probe": {
                "port": 7142,
                "send_hex": "01304130413036023031443603740D",
                "expect_hex": "0130304142",
                "extract_manufacturer": "Sharp NEC",
            },
            "manufacturer_alias": ["NEC", "Sharp"],
        },
        "compatible_models": [
            {
                "manufacturer": "Sharp NEC",
                "models": [
                    "P series",
                    "V series",
                    "X series",
                    "X-UN video wall series",
                ],
                "confidence": "untested",
                "notes": (
                    "The G2-generation External Control manual (Rev.2.4) "
                    "this driver was built from documents these series. "
                    "Opcode availability varies by model; unsupported "
                    "opcodes answer result code 01 and the driver stops "
                    "polling them on that display."
                ),
            },
            {
                "manufacturer": "Sharp NEC",
                "models": [
                    "E series",
                    "ME series",
                    "M series",
                    "MA series",
                    "C series",
                    "UN series",
                ],
                "confidence": "untested",
                "notes": (
                    "Newer NEC and Sharp-era large-format series publish "
                    "the same External Control protocol per model (framing, "
                    "check code, power procedure and core opcodes verified "
                    "identical in the Exx8-series manual). Expected to work "
                    "unchanged; not tested on hardware."
                ),
            },
        ],
        "help": {
            "overview": (
                "NEC External Control runs on TCP 7142 (fixed). One "
                "connection can address a whole RS-232 daisy chain of "
                "displays by Monitor ID — list the IDs and each becomes a "
                "display child entity with its own power, input, volume, "
                "picture values, temperature, and self-diagnosis status. "
                "The protocol requires the controller to pause 15 seconds "
                "after a power command (10 after an input change), so "
                "commands sent right after those are delayed briefly. Use "
                "Get/Set Opcode for protocol features not surfaced as "
                "named commands (PIP, OSD, IR lock, gamma, and more)."
            ),
            "setup": (
                "1. Connect the display's LAN port and give it a fixed "
                "IP. External control is always on; port 7142 is fixed.\n"
                "2. Check the display's Monitor ID in its OSD (MULTI "
                "DISPLAY menu). Factory default is 1.\n"
                "3. For a daisy-chained wall, connect RS-232 OUT to the "
                "next display's IN, give each display a unique Monitor "
                "ID, and list them all in 'Monitor IDs' (e.g. 1,2,3,4).\n"
                "4. Values changed over the protocol are volatile until "
                "stored — run Save Settings after commissioning changes."
            ),
        },
        "default_config": {
            "host": "",
            "port": DEFAULT_PORT,
            "display_ids": "1",
            "poll_interval": 20,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {
                "type": "integer",
                "default": DEFAULT_PORT,
                "label": "TCP Port",
                "description": "NEC External Control port. Fixed at 7142.",
            },
            "display_ids": {
                "type": "string",
                "default": "1",
                "label": "Monitor IDs",
                "description": (
                    "Comma-separated Monitor IDs (1-100). One ID for a "
                    "single display (factory default is 1); list every ID "
                    "for a daisy-chained wall, e.g. 1,2,3,4. Each becomes "
                    "a display child entity."
                ),
            },
            "poll_interval": {
                "type": "integer",
                "default": 20,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "The protocol is request/response only, so polling is "
                    "how state stays fresh — and the display drops the "
                    "connection after 15 minutes of silence, so keep this "
                    "well under 900. Set to 0 to disable (not "
                    "recommended)."
                ),
            },
        },
        "state_variables": {
            "display_count": {"type": "integer", "label": "Displays"},
        },
        "child_entity_types": {
            "display": {
                "label": "Display",
                "label_plural": "Displays",
                "id_format": {
                    "type": "integer",
                    "min": MONITOR_ID_MIN,
                    "max": MONITOR_ID_MAX,
                    "pad_width": 3,
                },
                "state_variables": _child_state_vars(),
                "summary_fields": ["power", "input", "volume"],
                "label_field": "label",
            },
        },
        "quick_actions": ["all_on", "all_off", "refresh"],
        "actions": [
            {"id": "all_on", "kind": "command", "icon": "power"},
            {"id": "all_off", "kind": "command", "icon": "power-off"},
            {"id": "refresh", "kind": "command", "icon": "refresh-cw"},
        ],
        "commands": {
            "power_on": {
                "label": "Power On",
                "params": {"display": _display_param()},
                "help": (
                    "Turn a display on. The protocol requires a 15 s pause "
                    "after a power command; queued commands wait."
                ),
            },
            "power_off": {
                "label": "Power Off",
                "params": {"display": _display_param()},
                "help": "Turn a display off (same as IR power off).",
            },
            "select_input": {
                "label": "Select Input",
                "params": {
                    "display": _display_param(),
                    "input": {
                        "type": "enum",
                        "required": True,
                        "values": list(INPUT_SET.keys()),
                        "label": "Input",
                    },
                },
                "help": (
                    "Switch a display's input. Per-model availability "
                    "varies; an unsupported input answers result code 01. "
                    "The protocol requires a 10 s pause after an input "
                    "change."
                ),
            },
            "set_volume": {
                "label": "Set Volume",
                "params": {
                    "display": _display_param(),
                    "level": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 100,
                        "label": "Volume",
                    },
                },
            },
            "mute_on": {
                "label": "Mute On",
                "params": {"display": _display_param()},
            },
            "mute_off": {
                "label": "Mute Off",
                "params": {"display": _display_param()},
            },
            "screen_mute_on": {
                "label": "Screen Mute On",
                "params": {"display": _display_param()},
                "help": "Blank the picture without powering down.",
            },
            "screen_mute_off": {
                "label": "Screen Mute Off",
                "params": {"display": _display_param()},
            },
            "set_backlight": {
                "label": "Set Backlight",
                "params": {
                    "display": _display_param(),
                    "level": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 100,
                        "label": "Backlight",
                    },
                },
            },
            "set_brightness": {
                "label": "Set Brightness",
                "params": {
                    "display": _display_param(),
                    "level": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 100,
                        "label": "Brightness",
                    },
                },
            },
            "set_contrast": {
                "label": "Set Contrast",
                "params": {
                    "display": _display_param(),
                    "level": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 100,
                        "label": "Contrast",
                    },
                },
            },
            "set_sharpness": {
                "label": "Set Sharpness",
                "params": {
                    "display": _display_param(),
                    "level": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 24,
                        "label": "Sharpness",
                    },
                },
            },
            "set_aspect": {
                "label": "Set Aspect",
                "params": {
                    "display": _display_param(),
                    "aspect": {
                        "type": "enum",
                        "required": True,
                        "values": list(ASPECT_SET.keys()),
                        "label": "Aspect",
                    },
                },
            },
            "set_picture_mode": {
                "label": "Set Picture Mode",
                "params": {
                    "display": _display_param(),
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": list(PICTURE_MODE_SET.keys()),
                        "label": "Picture Mode",
                    },
                },
            },
            "save_settings": {
                "label": "Save Settings",
                "params": {"display": _display_param()},
                "help": (
                    "Store the current settings in the display (Save "
                    "Current Settings command). Values changed over the "
                    "protocol are volatile until saved."
                ),
            },
            "get_opcode": {
                "label": "Get Opcode (raw)",
                "params": {
                    "display": _display_param(),
                    "page": {
                        "type": "string",
                        "required": True,
                        "label": "OP Code Page (hex)",
                        "pattern": "^[0-9A-Fa-f]{2}$",
                        "help": "e.g. 00, 02, 10, 11",
                    },
                    "code": {
                        "type": "string",
                        "required": True,
                        "label": "OP Code (hex)",
                        "pattern": "^[0-9A-Fa-f]{2}$",
                    },
                },
                "help": (
                    "Escape hatch: read any operation code from the "
                    "protocol's opcode table. Returns the current value."
                ),
            },
            "set_opcode": {
                "label": "Set Opcode (raw)",
                "params": {
                    "display": _display_param(),
                    "page": {
                        "type": "string",
                        "required": True,
                        "label": "OP Code Page (hex)",
                        "pattern": "^[0-9A-Fa-f]{2}$",
                    },
                    "code": {
                        "type": "string",
                        "required": True,
                        "label": "OP Code (hex)",
                        "pattern": "^[0-9A-Fa-f]{2}$",
                    },
                    "value": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 65535,
                        "label": "Value",
                    },
                },
                "help": (
                    "Escape hatch: write any operation code from the "
                    "protocol's opcode table (PIP, OSD, IR lock, gamma, "
                    "zoom, and more)."
                ),
            },
            "all_on": {
                "label": "All Displays On",
                "params": {},
                "help": "Power on every configured display.",
            },
            "all_off": {
                "label": "All Displays Off",
                "params": {},
                "help": "Power off every configured display.",
            },
            "refresh": {"label": "Refresh Status", "params": {}},
        },
    }

    def __init__(
        self, device_id: str, config: dict[str, Any], state, events,
    ) -> None:
        super().__init__(device_id, config, state, events)
        # Strict request/response: one in-flight packet, guarded by a
        # lock; the reply resolves the future.
        self._request_lock = asyncio.Lock()
        self._reply_future: asyncio.Future[dict[str, Any]] | None = None
        # Monotonic deadline before which nothing may be sent (the
        # manual's post-power / post-input cooldowns).
        self._quiet_until = 0.0
        # (monitor_id, page, code) combos that answered "unsupported" —
        # dropped from subsequent polls.
        self._unsupported: set[tuple[int, int, int]] = set()
        # Rotates the slow diagnostics read across poll cycles.
        self._poll_cycle = 0

    # ── Roster ──

    def _parse_display_ids(self) -> list[int]:
        raw = str(self.config.get("display_ids", "1"))
        ids: list[int] = []
        for part in raw.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                n = int(part)
            except ValueError:
                continue
            if MONITOR_ID_MIN <= n <= MONITOR_ID_MAX and n not in ids:
                ids.append(n)
        return ids or [1]

    def _reconcile_displays(self) -> None:
        want = self._parse_display_ids()
        current = set(self.list_children("display"))
        for monitor_id in want:
            self.register_child("display", monitor_id)  # idempotent
        for monitor_id in current - set(want):
            self.deregister_child("display", monitor_id)
        self.set_state("display_count", len(want))

    # ── Lifecycle ──

    async def _pre_connect(self) -> None:
        if not str(self.config.get("host", "")).strip():
            raise ConnectionError(f"[{self.device_id}] No host configured")
        # Every attempt starts fresh: forget which opcodes a previous
        # session reported unsupported, clear any armed cooldown window,
        # and restart the diagnostics rotation.
        self._unsupported = set()
        self._quiet_until = 0.0
        self._poll_cycle = 0

    def _transport_kwargs(
        self, transport_type: str, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        # Raw byte stream — packets are length-framed by the parser below
        # (a reply's check-code byte can itself equal CR, so delimiter
        # framing would corrupt packets).
        kwargs["delimiter"] = None
        return kwargs

    def _create_frame_parser(self):
        return CallableFrameParser(_extract_nec_frame)

    async def _initial_sync(self) -> None:
        self._reconcile_displays()
        # Identity + temperature-sensor arm, then a first full read so the
        # display cards aren't blank until the first poll cycle.
        for monitor_id in self.list_children("display"):
            await self._read_identity(monitor_id)
        try:
            await self.poll()
        except (ConnectionError, TimeoutError, OSError):
            log.warning(f"[{self.device_id}] Initial poll failed")

    async def _close_session(self) -> None:
        # Runs on every teardown path: abort the in-flight request so its
        # awaiter doesn't hang on a dead link.
        if self._reply_future and not self._reply_future.done():
            self._reply_future.cancel()
        self._reply_future = None

    async def refresh_children(self) -> dict[str, Any]:
        """Re-sync the roster from config and re-read every display —
        backs the IDE 'Refresh from Device' button. (The bus reports no
        device list; the roster is declared in ``display_ids``.)"""
        self._reconcile_displays()
        for monitor_id in self.list_children("display"):
            await self._read_identity(monitor_id)
        await self.poll()
        return {"displays": len(self.list_children("display"))}

    # ── Wire ──

    async def _transact(
        self,
        monitor_id: int,
        msg_type: bytes,
        message: bytes,
        cooldown: float = 0.0,
        timeout: float = 4.0,
    ) -> dict[str, Any]:
        """Send one packet and await its reply (strict alternation).

        ``cooldown`` arms the manual's post-command quiet window AFTER
        the reply arrives; every later send waits it out first.
        """
        if self.transport is None or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        async with self._request_lock:
            loop = asyncio.get_event_loop()
            wait = self._quiet_until - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)

            fut: asyncio.Future[dict[str, Any]] = loop.create_future()
            self._reply_future = fut
            try:
                await self.transport.send(
                    _build_packet(monitor_id, msg_type, message)
                )
                reply = await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"[{self.device_id}] Display {monitor_id} did not "
                    f"reply within {timeout}s"
                ) from None
            except asyncio.CancelledError:
                if fut.cancelled():
                    # The disconnect path cancelled the reply future.
                    raise ConnectionError(
                        f"[{self.device_id}] Connection dropped while "
                        f"waiting for display {monitor_id}"
                    ) from None
                raise  # outer task cancellation (e.g. stop_polling)
            finally:
                self._reply_future = None
            if cooldown > 0:
                self._quiet_until = loop.time() + cooldown
            return reply

    async def _get_param(
        self, monitor_id: int, page: int, code: int,
    ) -> dict[str, Any]:
        message = bytes([STX]) + _op_bytes(page, code) + bytes([ETX])
        return await self._transact(monitor_id, TYPE_GET, message)

    async def _set_param(
        self, monitor_id: int, page: int, code: int, value: int,
        cooldown: float = 0.0,
    ) -> dict[str, Any]:
        message = (
            bytes([STX]) + _op_bytes(page, code) + _hex_word(value)
            + bytes([ETX])
        )
        return await self._transact(
            monitor_id, TYPE_SET, message, cooldown=cooldown,
        )

    async def _command(
        self, monitor_id: int, payload: bytes, cooldown: float = 0.0,
    ) -> dict[str, Any]:
        message = bytes([STX]) + payload + bytes([ETX])
        return await self._transact(
            monitor_id, TYPE_COMMAND, message, cooldown=cooldown,
        )

    # ── Receive ──

    async def on_data_received(self, data: bytes) -> None:
        """Parse one CR-delimited packet, update the answering display's
        child state, and resolve the in-flight future."""
        parsed = self._parse_packet(data)
        if parsed is None:
            return

        monitor_id = parsed.get("monitor_id")
        updates = parsed.get("updates") or {}
        if (
            monitor_id is not None
            and updates
            and self.is_child_registered("display", monitor_id)
        ):
            self.set_child_state_batch("display", monitor_id, updates)

        fut = self._reply_future
        if fut is not None and not fut.done():
            fut.set_result(parsed)

    def _parse_packet(self, data: bytes) -> Optional[dict[str, Any]]:
        # SOH, reserved, dest, source, type, len(2) = 7 header bytes.
        if len(data) < 9 or data[0] != SOH:
            return None
        # Verify the BCC: XOR of everything after SOH except the BCC.
        if checksum_xor(data[1:-1]) != data[-1]:
            log.warning(f"[{self.device_id}] Bad check code in packet: {data!r}")
            return None

        monitor_id = _decode_monitor_id(data[3])
        msg_type = chr(data[4])
        try:
            msg_len = int(data[5:7].decode("ascii"), 16)
        except (ValueError, UnicodeDecodeError):
            return None
        message = data[7:7 + msg_len]
        if (
            len(message) != msg_len
            or not message.startswith(bytes([STX]))
            or not message.endswith(bytes([ETX]))
        ):
            return None
        body = message[1:-1]

        parsed: dict[str, Any] = {
            "monitor_id": monitor_id,
            "type": msg_type,
            "body": body,
            "updates": {},
        }
        if msg_type in ("D", "F"):
            self._parse_parameter_reply(parsed, body)
        elif msg_type == "B":
            self._parse_command_reply(parsed, body)
        return parsed

    def _parse_parameter_reply(
        self, parsed: dict[str, Any], body: bytes,
    ) -> None:
        # result(2) page(2) code(2) type(2) max(4) current(4)
        if len(body) < 16:
            return
        try:
            result = body[0:2].decode("ascii")
            page = int(body[2:4], 16)
            code = int(body[4:6], 16)
            max_value = int(body[8:12], 16)
            current = int(body[12:16], 16)
        except (ValueError, UnicodeDecodeError):
            return
        parsed["result"] = result
        parsed["page"] = page
        parsed["code"] = code
        parsed["max"] = max_value
        parsed["value"] = current
        if result != "00":
            return

        if (page, code) == OP_TEMP_READ:
            if current >= 0x8000:
                current -= 0x10000
            parsed["updates"]["temperature"] = current / 2.0
            return

        prop = OPCODE_TO_PROP.get((page, code))
        if prop is None:
            return
        parsed["updates"].update(self._decode_prop(prop, current))

    @staticmethod
    def _decode_prop(prop: str, value: int) -> dict[str, Any]:
        if prop == "input":
            return {"input": INPUT_NAMES.get(value, f"code_{value}")}
        if prop == "mute":
            return {"mute": value == 1}
        if prop == "video_mute":
            return {"video_mute": value == 1}
        if prop == "aspect":
            return {"aspect": ASPECT_NAMES.get(value, f"code_{value}")}
        if prop == "picture_mode":
            return {
                "picture_mode": PICTURE_MODE_NAMES.get(value, f"code_{value}")
            }
        return {prop: value}

    def _parse_command_reply(
        self, parsed: dict[str, Any], body: bytes,
    ) -> None:
        # Power status read reply: '02' result 'D6' type(2) max(4) cur(4)
        if body.startswith(b"02") and body[4:6] == b"D6" and len(body) >= 16:
            result = body[2:4].decode("ascii", errors="ignore")
            parsed["result"] = result
            if result == "00":
                try:
                    mode = int(body[12:16], 16)
                except ValueError:
                    return
                parsed["value"] = mode
                parsed["updates"]["power"] = POWER_MODES.get(
                    mode, f"mode_{mode}"
                )
            return
        # Power control reply: result(2) 'C203D6' mode(4)
        if body[2:8] == b"C203D6" and len(body) >= 12:
            result = body[0:2].decode("ascii", errors="ignore")
            parsed["result"] = result
            if result == "00":
                try:
                    mode = int(body[8:12], 16)
                except ValueError:
                    return
                parsed["value"] = mode
                parsed["updates"]["power"] = POWER_MODES.get(
                    mode, f"mode_{mode}"
                )
            return
        # Self-diagnosis reply: 'A1' + status pairs.
        if body.startswith(b"A1"):
            codes: list[str] = []
            tail = body[2:].decode("ascii", errors="ignore")
            for i in range(0, len(tail) - 1, 2):
                codes.append(tail[i:i + 2].upper())
            faults = [c for c in codes if c and c != "00"]
            if faults:
                meaning = DIAG_CODES.get(faults[0], "unknown fault")
                parsed["updates"]["diagnosis"] = f"{faults[0]}: {meaning}"
            else:
                parsed["updates"]["diagnosis"] = "normal"
            return
        # Serial number reply: 'C316' + hex pairs.
        if body.startswith(b"C316"):
            serial = _decode_hex_pairs(body[4:])
            if serial:
                parsed["updates"]["serial"] = serial
            return
        # Model name reply: 'C317' + hex pairs.
        if body.startswith(b"C317"):
            model = _decode_hex_pairs(body[4:])
            if model:
                parsed["updates"]["model"] = model
            return

    # ── Reads ──

    async def _read_identity(self, monitor_id: int) -> None:
        """Model + serial, and arm temperature sensor #1 for readouts."""
        for payload in (b"C217", b"C216"):
            try:
                await self._command(monitor_id, payload)
            except TimeoutError:
                log.warning(
                    f"[{self.device_id}] Display {monitor_id} identity "
                    "read timed out"
                )
                return
        try:
            page, code = OP_TEMP_SELECT
            await self._set_param(monitor_id, page, code, 1)
        except TimeoutError:
            pass

    async def poll(self) -> None:
        """Read every display's power, parameters, temperature — and, on
        a slower rotation, its self-diagnosis status.

        Transport failures and reply timeouts propagate so the platform
        watchdog counts them; per-opcode "unsupported" results are
        remembered and skipped on later cycles.
        """
        if self.transport is None or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        self._poll_cycle += 1
        for monitor_id in self.list_children("display"):
            reply = await self._command(monitor_id, b"01D6")
            power_mode = reply.get("value")

            # Parameter reads are only meaningful when the panel is on;
            # standby-class states answer with junk or errors on many
            # models. Power + diagnosis still work in standby.
            if power_mode == 1:
                for prop in POLL_PROPS:
                    page, code = OPCODES[prop]
                    if (monitor_id, page, code) in self._unsupported:
                        continue
                    reply = await self._get_param(monitor_id, page, code)
                    if reply.get("result") == "01":
                        self._unsupported.add((monitor_id, page, code))
                page, code = OP_TEMP_READ
                if (monitor_id, page, code) not in self._unsupported:
                    reply = await self._get_param(monitor_id, page, code)
                    if reply.get("result") == "01":
                        self._unsupported.add((monitor_id, page, code))

            if self._poll_cycle % 4 == 1:
                try:
                    await self._command(monitor_id, b"B1")
                except TimeoutError:
                    # Some models lack self-diagnosis; don't fail the poll.
                    log.debug(
                        f"[{self.device_id}] Display {monitor_id} "
                        "self-diagnosis read timed out"
                    )

    # ── Commands ──

    @staticmethod
    def _require_ok(reply: dict[str, Any], what: str) -> dict[str, Any]:
        """Raise when a parameter/command reply carries a non-00 result
        (01 = the display does not support this operation)."""
        result = reply.get("result")
        if result is not None and result != "00":
            raise ValueError(
                f"{what}: display answered result code {result} "
                "(unsupported on this model)"
            )
        return reply

    def _coerce_child_ids(self, command: str, params: dict[str, Any]) -> None:
        cmd_def = self.DRIVER_INFO["commands"].get(command, {})
        for pname, pdef in cmd_def.get("params", {}).items():
            if (
                pdef.get("type") == "child_id"
                and pname in params
                and params[pname] != ""
            ):
                try:
                    params[pname] = int(params[pname])
                except (TypeError, ValueError) as e:
                    raise ValueError(
                        f"{command}: parameter {pname!r} must be an "
                        f"integer Monitor ID, got {params[pname]!r}"
                    ) from e

    async def _set_power(self, monitor_id: int, on: bool) -> Any:
        mode = 1 if on else 4
        payload = b"C203D6" + _hex_word(mode)
        return await self._command(
            monitor_id, payload, cooldown=POWER_COOLDOWN_S,
        )

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None,
    ) -> Any:
        params = params or {}
        self._coerce_child_ids(command, params)

        if command == "power_on":
            return self._require_ok(
                await self._set_power(params["display"], True), command,
            )
        if command == "power_off":
            return self._require_ok(
                await self._set_power(params["display"], False), command,
            )
        if command == "select_input":
            code = INPUT_SET.get(str(params.get("input", "")))
            if code is None:
                raise ValueError(f"Unknown input: {params.get('input')}")
            page, op = OPCODES["input"]
            return self._require_ok(
                await self._set_param(
                    params["display"], page, op, code,
                    cooldown=INPUT_COOLDOWN_S,
                ),
                command,
            )

        simple_sets: dict[str, tuple[str, int]] = {
            "set_volume": ("volume", -1),
            "mute_on": ("mute", 1),
            "mute_off": ("mute", 2),
            "screen_mute_on": ("video_mute", 1),
            "screen_mute_off": ("video_mute", 2),
            "set_backlight": ("backlight", -1),
            "set_brightness": ("brightness", -1),
            "set_contrast": ("contrast", -1),
            "set_sharpness": ("sharpness", -1),
        }
        if command in simple_sets:
            prop, fixed = simple_sets[command]
            value = fixed if fixed >= 0 else int(params["level"])
            page, op = OPCODES[prop]
            return self._require_ok(
                await self._set_param(params["display"], page, op, value),
                command,
            )

        if command == "set_aspect":
            code = ASPECT_SET.get(str(params.get("aspect", "")))
            if code is None:
                raise ValueError(f"Unknown aspect: {params.get('aspect')}")
            page, op = OPCODES["aspect"]
            return self._require_ok(
                await self._set_param(params["display"], page, op, code),
                command,
            )
        if command == "set_picture_mode":
            code = PICTURE_MODE_SET.get(str(params.get("mode", "")))
            if code is None:
                raise ValueError(
                    f"Unknown picture mode: {params.get('mode')}"
                )
            page, op = OPCODES["picture_mode"]
            return self._require_ok(
                await self._set_param(params["display"], page, op, code),
                command,
            )
        if command == "save_settings":
            return self._require_ok(
                await self._command(params["display"], b"0C"), command,
            )
        if command == "get_opcode":
            page = int(str(params["page"]), 16)
            code = int(str(params["code"]), 16)
            reply = self._require_ok(
                await self._get_param(params["display"], page, code),
                command,
            )
            return reply.get("value")
        if command == "set_opcode":
            page = int(str(params["page"]), 16)
            code = int(str(params["code"]), 16)
            reply = self._require_ok(
                await self._set_param(
                    params["display"], page, code, int(params["value"]),
                ),
                command,
            )
            return reply.get("value")
        if command == "all_on":
            for monitor_id in self.list_children("display"):
                await self._set_power(monitor_id, True)
            return True
        if command == "all_off":
            for monitor_id in self.list_children("display"):
                await self._set_power(monitor_id, False)
            return True
        if command == "refresh":
            await self.poll()
            return True

        log.warning(f"[{self.device_id}] Unknown command: {command}")
        return None
