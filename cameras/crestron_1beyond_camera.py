"""
OpenAVC Crestron 1 Beyond camera driver (IV-CAM series).

Controls Crestron 1 Beyond cameras over Crestron's documented VISCA command
set: the IV-CAM-I12 and IV-CAM-I20 intelligent cameras (Group Framing,
Presenter Tracking, Group Tracking on the I20), the IV-CAM-P12 and
IV-CAM-P20 PTZ cameras, and the IV-CAM-I12D-B dual-PTZ speaker-tracking
camera. Raw VISCA (``8x ... FF`` packets, no Sony VISCA-over-IP header) on
TCP 5500 or the camera's RS-232 / RS-485 port at 9600 baud.

What it adds over the generic VISCA drivers
-------------------------------------------
* The camera address (1-7, 1-3 on the I12D-B) is a setting, so every packet
  carries it and only replies from that address are read.
* Crestron's reserved presets: start / pause tracking (80 / 81), Group
  Tracking (82 / 83, I20), Intelligent Switching feeds (85 / 86), the
  I12D-B's Group Framing and Speaker Tracking modes (87 / 89), the on-screen
  menu (95), reboot (99), the I20's preset zones (101-104) and tracking
  profiles (105-108), and the Home Shot / Tracking Shot (0 / 1).
* The tracking inquiry (``8x 09 08 01 FF``), the lightbar (``8x C1``), the
  Intelligent Switching command set (``8x C2``), mount mode (P-series), the
  IR receiver, and the zoom ratio tables for the 12x and 20x lenses.

One driver for the whole line, narrowed by the Model setting: the camera
cannot say which model it is in a form the document decodes (CAM_VersionInq
returns a model code with no table), so the integrator picks it and the
instance offers only that model's commands and settings. Model has no
default: a camera with none set does not connect (``invalid_config``),
because any default would be wrong for four models out of five.

Why Python
----------
VISCA replies do not say what they answer: ``90 50 02 FF`` is the reply to
the power, focus mode, backlight, exposure compensation, tracking and IR
inquiries alike, so every reply is matched to the inquiry that asked for it,
one exchange at a time. Packets are binary with a configurable address byte.

Push vs poll
------------
Poll only. The document describes request / reply exchanges and nothing the
camera sends on its own. Every ``poll_interval`` (default 5 s) the driver
reads power first, then (while the camera is on) position, lens, exposure,
white balance, the last preset, tracking, mount mode or IR, the video format
and, on an Intelligent Switching host, the output and each camera's link.
An inquiry the camera refuses as a syntax error is not asked again until the
next connection. A poll stops at the first inquiry nothing answers.

Privacy Mode and the wake command
---------------------------------
Crestron's page says a camera in Privacy Mode accepts no VISCA command and
that "the wake command" must be sent first, without naming it. The driver
reads Power On as that command: the camera's remote turns Privacy Mode on and
off with its Power button, and the power inquiry has an "Off (Standby)"
answer. The document does not say whether a camera in Privacy Mode answers
anything, so the driver survives both: while power reads standby, an
unanswered inquiry neither ends the poll with an error nor fails the liveness
probe.

Liveness
--------
The power inquiry is the liveness probe (``_liveness_probe``), on TCP and on
serial alike. Any reply counts, an error reply included; silence twice in a
row drops the connection as no_response, except while the camera reads
standby (above).

Source: Crestron "IV-CAM Series Manual" (Doc. 9440, web edition built
2026-06-26): VISCA Commands, VISCA Lightbar Commands, VISCA Intelligent
Switching Commands, Reserved Presets, Advanced Camera Settings (ports, RTSP).
"""

from __future__ import annotations

import asyncio
import ipaddress
from typing import Any
from urllib.parse import quote

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.utils.logger import get_logger

log = get_logger(__name__)


# ── Models ──

_I_SERIES = frozenset({"i12", "i20", "i12d"})
_P_SERIES = frozenset({"p12", "p20"})
_MODELS = _I_SERIES | _P_SERIES

# Optical zoom ratio -> CAM_Zoom Direct position, from the manual's
# "Zoom Ratio / Position" tables. The I12D-B has no table (its lens is not
# specified), so it reports no ratio.
_ZOOM_TABLES: dict[str, list[tuple[int, int]]] = {
    "12x": [
        (1, 0x0000), (2, 0x1982), (3, 0x24E2), (4, 0x2BC9), (5, 0x3099),
        (6, 0x343D), (7, 0x3724), (8, 0x3988), (9, 0x3B8B), (10, 0x3D43),
        (11, 0x3EBB), (12, 0x4000),
    ],
    "20x": [
        (1, 0x0000), (2, 0x1851), (3, 0x22BE), (4, 0x28F6), (5, 0x2D45),
        (6, 0x3086), (7, 0x3320), (8, 0x3549), (9, 0x371E), (10, 0x38B3),
        (11, 0x3A12), (12, 0x3B42), (13, 0x3C47), (14, 0x3D25), (15, 0x3DDF),
        (16, 0x3E7B), (17, 0x3EFB), (18, 0x3F64), (19, 0x3FBA), (20, 0x4000),
    ],
}
_MODEL_LENS = {"i12": "12x", "p12": "12x", "i20": "20x", "p20": "20x"}

# The video feeds each model publishes, in RTSP path order (Accessing the
# RTSP Streams): rtsp://<ip>:<port>/<index>.<encoding>.
_FEEDS: dict[str, list[tuple[str, int, str]]] = {
    "i12": [("main", 1, "Main PTZ"), ("reference", 2, "Wide Reference")],
    "i20": [("main", 1, "Main PTZ"), ("reference", 2, "Wide Reference")],
    "p12": [("main", 1, "Main PTZ")],
    "p20": [("main", 1, "Main PTZ")],
    "i12d": [
        ("ptz1", 1, "PTZ 1"), ("ptz2", 2, "PTZ 2"),
        ("reference", 3, "Wide Reference"),
    ],
}

# Presets each model reserves (Reserved Presets table). They cannot be saved
# over or deleted; 0 and 1 are the Home Shot and Tracking Shot, which the
# intelligent models need set and never deleted.
_RESERVED: dict[str, frozenset[int]] = {
    "i12": frozenset({80, 81, 84, 85, 86, 95, 99}),
    "i20": frozenset({80, 81, 82, 83, 95, 99, *range(101, 109)}),
    "i12d": frozenset({80, 81, 85, 86, 87, 88, 89, 95, 99}),
    "p12": frozenset({95, 99}),
    "p20": frozenset({95, 99}),
}
_PRESET_NAMES = {
    80: "Start Tracking", 81: "Pause Tracking", 82: "Start Group Tracking",
    83: "Pause Group Tracking", 84: "Resume Switching",
    85: "Show Group Framing Feed", 86: "Show Presenter Feed",
    87: "Enable Group Framing", 88: "a reserved preset",
    89: "Enable Speaker Tracking", 95: "Toggle On-Screen Menu", 99: "Reboot",
    101: "Recall Preset Zone", 102: "Recall Preset Zone",
    103: "Recall Preset Zone", 104: "Recall Preset Zone",
    105: "Select Tracking Profile", 106: "Select Tracking Profile",
    107: "Select Tracking Profile", 108: "Select Tracking Profile",
}

# Commands a model has. Absent = every model.
_COMMAND_MODELS: dict[str, frozenset[str]] = {
    "start_tracking": _I_SERIES,
    "pause_tracking": _I_SERIES,
    "recall_home_shot": _I_SERIES,
    "set_home_shot": _I_SERIES,
    "set_tracking_shot": _I_SERIES,
    "start_group_tracking": frozenset({"i20"}),
    "pause_group_tracking": frozenset({"i20"}),
    "recall_preset_zone": frozenset({"i20"}),
    "select_tracking_profile": frozenset({"i20"}),
    "enable_group_framing": frozenset({"i12d"}),
    "enable_speaker_tracking": frozenset({"i12d"}),
    "set_mount_mode": _P_SERIES,
    "set_ir_receiver": frozenset({"i12", "i12d"}),
    "zoom_to_ratio": frozenset({"i12", "i20", "p12", "p20"}),
}
# Quick actions an intelligent model drops: its Home Shot button replaces
# Pan/Tilt Home there.
_QUICK_SKIP_I_SERIES = frozenset({"pt_home"})
# The Intelligent Switching surface: offered on a host model (an I12 in
# Group Framing or an I12D-B) whose "Intelligent Switching Host" setting is on.
_SWITCHING_HOSTS = frozenset({"i12", "i12d"})
_SWITCHING_COMMANDS = frozenset({
    "set_switching_camera", "clear_switching_cameras", "switch_to_camera",
    "resume_switching", "pause_switching", "show_group_framing_feed",
    "show_presenter_feed",
})
_SETTING_MODELS: dict[str, frozenset[str]] = {
    "mount_mode": _P_SERIES,
    "ir_receiver": frozenset({"i12", "i12d"}),
}


# ── Wire tables ──

_PT_DIR = {
    "pt_up": (0x03, 0x01),
    "pt_down": (0x03, 0x02),
    "pt_left": (0x01, 0x03),
    "pt_right": (0x02, 0x03),
    "pt_up_left": (0x01, 0x01),
    "pt_up_right": (0x02, 0x01),
    "pt_down_left": (0x01, 0x02),
    "pt_down_right": (0x02, 0x02),
    "pt_stop": (0x03, 0x03),
}

_AE_MODES = {"full_auto": 0x00, "manual": 0x03, "shutter": 0x0A, "iris": 0x0B, "bright": 0x0D}
_AE_FROM = {v: k for k, v in _AE_MODES.items()}
_WB_MODES = {"auto": 0x00, "indoor": 0x01, "outdoor": 0x02, "one_push": 0x03, "manual": 0x05}
_WB_FROM = {v: k for k, v in _WB_MODES.items()}

# CAM_RGain / BGain / Shutter / Iris / Gain / Bright / ExpComp share one
# shape: 8x 01 04 <step> 00|02|03 FF (reset / up / down) and
# 8x 01 04 <direct> 00 00 0p 0q FF, inquired with 8x 09 04 <direct> FF.
_LEVELS = {
    "r_gain": (0x03, 0x43),
    "b_gain": (0x04, 0x44),
    "shutter": (0x0A, 0x4A),
    "iris": (0x0B, 0x4B),
    "gain": (0x0C, 0x4C),
    "bright": (0x0D, 0x4D),
    "exp_comp": (0x0E, 0x4E),
}
_STEP = {"reset": 0x00, "up": 0x02, "down": 0x03}

_VIDEO_FORMATS = {
    0x00: "1080i60", 0x01: "1080p30", 0x02: "720p60", 0x03: "720p30",
    0x07: "1080p60", 0x08: "1080i50", 0x09: "1080p25", 0x0A: "720p50",
    0x0B: "720p25", 0x0F: "1080p50",
}

# Lightbar: each of the four segment bytes is 0b0000BBCC, brightness then
# colour (VISCA Lightbar Commands, "Create a Custom VISCA Lightbar Command").
# Segments 1 and 4 are the outer ones, 2 and 3 the inner ("half width") ones.
_LIGHT_COLOR = {"green": 0b00, "red": 0b01, "yellow": 0b11}
_LIGHT_LEVEL = {"off": 0b00, "dim": 0b01, "medium": 0b10, "bright": 0b11}

# VISCA error codes (Error Messages table).
_ERRORS = {
    0x02: "syntax error",
    0x03: "command buffer full",
    0x04: "command canceled",
    0x05: "no socket",
    0x41: "command not executable",
}

POWER_INQUIRY = b"\x09\x04\x00"
REPLY_TIMEOUT_S = 1.0
# How long a packet the document gives no reply for (lightbar, IF_Clear)
# waits in case the camera answers it with an error.
OPTIONAL_REPLY_TIMEOUT_S = 0.3
# Inquiries re-read only every this many polls (the switching cameras' IPs).
_SLOW_POLL_EVERY = 12


def _encode_4nibble(value: int) -> bytes:
    v = value & 0xFFFF
    return bytes([(v >> 12) & 0x0F, (v >> 8) & 0x0F, (v >> 4) & 0x0F, v & 0x0F])


def _decode_4nibble(data: bytes, signed: bool = False) -> int:
    v = (
        ((data[0] & 0x0F) << 12) | ((data[1] & 0x0F) << 8)
        | ((data[2] & 0x0F) << 4) | (data[3] & 0x0F)
    )
    if signed and v >= 0x8000:
        v -= 0x10000
    return v


def _decode_byte_nibbles(data: bytes) -> int:
    """The ``0p 0q`` pair of a level reply, as one byte."""
    return ((data[0] & 0x0F) << 4) | (data[1] & 0x0F)


def lightbar_segment(color: str, level: str) -> int:
    """One lightbar segment byte. Off keeps the colour bits, which is how the
    manual's half-width rows write the dark outer segments (``03`` for
    yellow, ``01`` for red)."""
    return (_LIGHT_LEVEL[level] << 2) | _LIGHT_COLOR[color]


def lightbar_bytes(width: str, color: str, level: str) -> bytes:
    """The four segment bytes for a width / colour / brightness row."""
    lit = lightbar_segment(color, level)
    if width == "off":
        return b"\x00\x00\x00\x00"
    if width == "half":
        dark = lightbar_segment(color, "off")
        return bytes([dark, lit, lit, dark])
    return bytes([lit, lit, lit, lit])


def segment_from_choice(choice: str) -> int:
    """A ``<colour>_<brightness>`` segment choice (or ``off``) as its byte."""
    if choice == "off":
        return 0x00
    color, _, level = choice.partition("_")
    if color not in _LIGHT_COLOR or level not in ("dim", "medium", "bright"):
        raise ValueError(f"Unknown lightbar segment choice: {choice}")
    return lightbar_segment(color, level)


def encode_ip(ip: str) -> bytes:
    """An IPv4 address as the eight ``0W`` digits Set Camera takes: each W is
    one hexadecimal digit of the address, high digit first."""
    try:
        packed = ipaddress.IPv4Address(ip.strip()).packed
    except (ipaddress.AddressValueError, ValueError) as exc:
        raise ValueError(f"'{ip}' is not an IPv4 address") from exc
    out = bytearray()
    for octet in packed:
        out += bytes([(octet >> 4) & 0x0F, octet & 0x0F])
    return bytes(out)


def decode_ip(digits: bytes) -> str:
    octets = [((digits[i] & 0x0F) << 4) | (digits[i + 1] & 0x0F) for i in range(0, 8, 2)]
    return ".".join(str(o) for o in octets)


def zoom_ratio_for(position: int, lens: str) -> float:
    """The optical zoom ratio a zoom position stands for, read off the
    manual's table and straight between its whole-number steps."""
    table = _ZOOM_TABLES[lens]
    position = max(0, min(table[-1][1], position))
    for (r0, p0), (r1, p1) in zip(table, table[1:]):
        if position <= p1:
            frac = (position - p0) / (p1 - p0) if p1 != p0 else 0.0
            return round(r0 + frac * (r1 - r0), 1)
    return float(table[-1][0])


def zoom_position_for(ratio: float, lens: str) -> int:
    table = _ZOOM_TABLES[lens]
    ratio = max(1.0, min(float(table[-1][0]), float(ratio)))
    for (r0, p0), (r1, p1) in zip(table, table[1:]):
        if ratio <= r1:
            return int(round(p0 + (ratio - r0) / (r1 - r0) * (p1 - p0)))
    return table[-1][1]


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "on", "yes")


class _CameraRefused(RuntimeError):
    """The camera answered a command with a VISCA error."""


class CrestronOneBeyondCameraDriver(BaseDriver):
    """Crestron 1 Beyond IV-CAM cameras over VISCA (TCP 5500 or serial)."""

    HEALTH_INTERVAL_S = 20.0
    HEALTH_FAULT_MESSAGE = (
        "Connected, but the camera stopped answering the power query."
    )

    DRIVER_INFO = {
        "id": "crestron_1beyond_camera",
        "name": "Crestron 1 Beyond Camera",
        "manufacturer": "Crestron",
        "category": "camera",
        "version": "1.0.1",
        "author": "OpenAVC",
        "min_platform_version": "0.36.0",
        "description": (
            "Controls Crestron 1 Beyond IV-CAM cameras (I12, I20, P12, P20 and "
            "I12D-B) over VISCA on TCP 5500 or RS-232 / RS-485. Power and "
            "Privacy Mode, pan / tilt / zoom / focus, presets, exposure and "
            "white balance, tracking and the intelligent modes through the "
            "camera's reserved presets, the lightbar, mount mode, and the "
            "Intelligent Switching host commands. Publishes each camera's RTSP "
            "feeds for the Video Panel."
        ),
        "source_url": "https://docs.crestron.com/en-us/9440/Content/Topics/NextGenCameras/Configuration/VISCA-Commands.htm",
        "tags": [
            "ptz", "camera", "visca", "crestron", "1-beyond", "tracking",
            "auto-framing", "rtsp", "ndi",
        ],
        "verified": False,
        "simulated": True,
        "protocols": ["visca"],
        "ports": [5500],
        "transport": "tcp",
        "transports": ["tcp", "serial"],
        "discovery": {
            # The tracking inquiry is Crestron's own: an intelligent 1 Beyond
            # camera answers active (02) or paused (03); another VISCA camera
            # refuses it as a syntax error.
            "tcp_probe": {
                "port": 5500,
                "send_hex": "81 09 08 01 FF",
                "expect_regex": "^\\x90\\x50[\\x02\\x03]\\xff",
                "timeout_ms": 1500,
            },
        },
        "compatible_models": [
            {
                "manufacturer": "Crestron",
                "models": [
                    "IV-CAM-I12-B", "IV-CAM-I12-GV-B",
                    "IV-CAM-I20-B", "IV-CAM-I20-W", "IV-CAM-I20-GV-B",
                ],
                "confidence": "untested",
                "notes": (
                    "Intelligent cameras. Set Model to I12 or I20. Tracking, "
                    "Group Tracking (I20), preset zones and tracking profiles "
                    "(I20) and the Intelligent Switching host commands (I12 in "
                    "Group Framing) are offered for the model picked."
                ),
            },
            {
                "manufacturer": "Crestron",
                "models": [
                    "IV-CAM-P12-B", "IV-CAM-P12-W",
                    "IV-CAM-P20-B", "IV-CAM-P20-W", "IV-CAM-P20-GV-B",
                ],
                "confidence": "untested",
                "notes": "PTZ cameras. Set Model to P12 or P20 for mount mode and the zoom ratio.",
            },
            {
                "manufacturer": "Crestron",
                "models": ["IV-CAM-I12D-B"],
                "confidence": "untested",
                "notes": (
                    "Dual-PTZ speaker-tracking camera. Set Model to I12D-B. "
                    "Commands reach the PTZ head the camera addresses over "
                    "VISCA; all three feeds are published."
                ),
            },
        ],
        "help": {
            "overview": (
                "Crestron 1 Beyond IV-CAM cameras over VISCA. Set Model to the "
                "camera in hand: the commands offered follow it. Power Off puts "
                "the camera in Privacy Mode (video output off, lightbar half "
                "red) and Power On wakes it; Crestron's manual says to send Power "
                "On before any other command. Start Tracking and Pause Tracking run the "
                "intelligent camera's Group Framing or Presenter Tracking, "
                "whichever mode Camera Manager set. The camera is polled every "
                "few seconds; it does not report changes on its own."
            ),
            "setup": (
                "1. Set Model to the camera in hand (the label on the camera or "
                "its box).\n"
                "2. Network: add the camera's IP address. VISCA control uses TCP "
                "port 5500.\n"
                "3. Serial: wire the RS-232 / RS-485 terminal block and use 9600 "
                "baud (the camera's OSD can set 2400 to 38400).\n"
                "4. Set Camera Address to the address in the camera's OSD "
                "(System > Address, 1 by default).\n"
                "5. Set the Home Shot and Tracking Shot (presets 0 and 1) on an "
                "intelligent camera before using tracking.\n"
                "6. Video Panel: set the Camera Password (the one set in Camera "
                "Manager) and turn on Login in Stream Address to play the RTSP "
                "feeds, or add the stream under Video Streams with its login."
            ),
        },
        "default_config": {
            "host": "",
            "port": 5500,
            "model": "",
            "camera_address": 1,
            "pan_speed": 12,
            "tilt_speed": 10,
            "poll_interval": 5,
            "baudrate": 9600,
            "switching_host": False,
            "rtsp_port": 554,
            "stream_encoding": "h264",
            "camera_password": "",
            "credentials_in_stream_url": False,
        },
        "config_schema": {
            "host": {
                "type": "string", "required": True, "label": "IP Address",
                "description": "The camera's IP address. VISCA control is on TCP port 5500.",
            },
            "port": {
                "type": "integer", "default": 5500, "label": "VISCA Port",
                "description": "5500 unless it was changed in Camera Manager (Advanced Settings > Control Port).",
            },
            "model": {
                "type": "enum", "required": True, "label": "Model",
                "values": [
                    {"value": "i12", "label": "IV-CAM-I12 (intelligent, 12x)"},
                    {"value": "i20", "label": "IV-CAM-I20 (intelligent, 20x)"},
                    {"value": "p12", "label": "IV-CAM-P12 (PTZ, 12x)"},
                    {"value": "p20", "label": "IV-CAM-P20 (PTZ, 20x)"},
                    {"value": "i12d", "label": "IV-CAM-I12D-B (dual PTZ, speaker tracking)"},
                ],
                "help": "The camera model. The commands and settings offered follow it, and the camera does not connect until it is set.",
            },
            "camera_address": {
                "type": "integer", "default": 1, "min": 1, "max": 7,
                "label": "Camera Address",
                "help": "The address set in the camera's OSD (System > Address). 1 by default; the I12D-B allows 1 to 3.",
            },
            "pan_speed": {
                "type": "integer", "default": 12, "min": 1, "max": 24,
                "label": "Default Pan Speed (1-24)",
            },
            "tilt_speed": {
                "type": "integer", "default": 10, "min": 1, "max": 20,
                "label": "Default Tilt Speed (1-20)",
            },
            "poll_interval": {
                "type": "integer", "default": 5, "min": 0,
                "label": "Poll Interval (sec)",
                "help": "How often the camera is read. 0 stops polling; the power query still runs every 20 seconds to notice a camera that stops answering.",
            },
            "baudrate": {
                "type": "integer", "default": 9600, "label": "Baud Rate (serial)",
                "help": "9600 unless the camera's OSD (System > Baudrate) was changed.",
            },
            "switching_host": {
                "type": "boolean", "default": False,
                "label": "Intelligent Switching Host",
                "help": "Turn on for the host camera of an Intelligent Switching system (an I12 in Group Framing or an I12D-B): adds the switching commands and reads the output and each switching camera's link.",
            },
            "rtsp_port": {
                "type": "integer", "default": 554, "min": 1, "max": 65535,
                "label": "RTSP Port", "advanced": True,
                "help": "The camera's RTSP port (Camera Manager > Advanced Settings > Network). 554, or 3479 to 7999.",
            },
            "stream_encoding": {
                "type": "enum", "default": "h264", "label": "Stream Encoding", "advanced": True,
                "values": [
                    {"value": "h264", "label": "H.264"},
                    {"value": "h265", "label": "H.265"},
                ],
                "help": "The encoding set for the stream in Camera Manager (Advanced Settings > Streaming).",
            },
            "camera_password": {
                "type": "string", "default": "", "secret": True,
                "label": "Camera Password", "advanced": True,
                "help": "The camera's password from Camera Manager. Only the RTSP feeds use it; VISCA control has no login.",
            },
            "credentials_in_stream_url": {
                "type": "boolean", "default": False,
                "label": "Login in Stream Address", "advanced": True,
                "help": "Put admin and the Camera Password in the published RTSP addresses so the Video Panel can play them without adding the stream by hand. The address, login included, is then visible in Live State and to a paired cloud account. Off keeps the login out of state; add the stream under Video Streams with its login instead.",
            },
        },
        "state_variables": {
            "power": {
                "type": "enum", "values": ["on", "standby", "fault"],
                "label": "Power", "control": True, "cloud_priority": "high",
                "help": "on, standby (Privacy Mode: video output off, other commands refused) or fault (the camera reports a power circuit error).",
            },
            "pan_position": {
                "type": "integer", "label": "Pan Position", "min": -32768, "max": 32767,
                "help": "The pan position the camera reports, 14.4 per degree.",
            },
            "tilt_position": {
                "type": "integer", "label": "Tilt Position", "min": -32768, "max": 32767,
                "help": "The tilt position the camera reports, 14.4 per degree.",
            },
            "pan_angle": {
                "type": "number", "label": "Pan Angle", "unit": "°",
                "help": "The pan position in degrees (the camera pans -130 to 130).",
            },
            "tilt_angle": {
                "type": "number", "label": "Tilt Angle", "unit": "°",
                "help": "The tilt position in degrees (the camera tilts -30 to 90).",
            },
            "zoom_position": {
                "type": "integer", "label": "Zoom Position", "min": 0, "max": 16384,
                "help": "0 is fully wide; 16384 is the lens's full optical zoom.",
            },
            "zoom_ratio": {
                "type": "number", "label": "Zoom Ratio", "unit": "x", "min": 1, "max": 20,
                "help": "The optical zoom ratio, from Crestron's table for the model's lens. Not reported for the I12D-B.",
            },
            "focus_position": {"type": "integer", "label": "Focus Position", "min": 0, "max": 65535},
            "focus_mode": {
                "type": "enum", "values": ["auto", "manual"], "label": "Focus Mode", "control": True,
            },
            "ae_mode": {
                "type": "enum", "values": ["full_auto", "manual", "shutter", "iris", "bright"],
                "label": "Exposure Mode",
            },
            "wb_mode": {
                "type": "enum", "values": ["auto", "indoor", "outdoor", "one_push", "manual"],
                "label": "White Balance Mode",
            },
            "r_gain": {
                "type": "integer", "label": "Red Gain", "min": 0, "max": 255,
                "help": "The camera's red gain position (manual white balance).",
            },
            "b_gain": {
                "type": "integer", "label": "Blue Gain", "min": 0, "max": 255,
                "help": "The camera's blue gain position (manual white balance).",
            },
            "shutter_position": {
                "type": "integer", "label": "Shutter Position", "min": 0, "max": 255,
                "help": "The camera's shutter position (manual or shutter priority exposure).",
            },
            "iris_position": {
                "type": "integer", "label": "Iris Position", "min": 0, "max": 255,
                "help": "The camera's iris position (manual or iris priority exposure).",
            },
            "gain_position": {
                "type": "integer", "label": "Gain Position", "min": 0, "max": 255,
                "help": "The camera's gain position (manual exposure).",
            },
            "bright_position": {
                "type": "integer", "label": "Bright Position", "min": 0, "max": 255,
                "help": "The camera's brightness position (bright exposure mode).",
            },
            "exp_comp": {"type": "boolean", "label": "Exposure Compensation"},
            "exp_comp_level": {
                "type": "integer", "label": "Exposure Compensation Level",
                "min": -7, "max": 7, "step": 1,
                "help": "-7 to +7, as the camera's OSD shows it.",
            },
            "backlight": {"type": "boolean", "label": "Backlight Compensation"},
            "last_preset": {
                "type": "integer", "label": "Last Preset", "min": 0, "max": 254,
                "help": "The preset the camera last ran.",
            },
            "tracking": {
                "type": "enum", "values": ["active", "paused"],
                "label": "Tracking", "control": True, "cloud_priority": "high",
                "help": "Whether the intelligent function (Group Framing or Presenter Tracking) is running or paused.",
            },
            "ir_receiver": {"type": "boolean", "label": "IR Receiver"},
            "mount_mode": {
                "type": "enum", "values": ["stand", "ceiling"], "label": "Mount Mode",
                "help": "ceiling inverts the video and the pan / tilt controls (P12 / P20).",
            },
            "video_format": {
                "type": "string", "label": "Video Format",
                "help": "The SDI / HDMI output format, for example 1080p60.",
            },
            "pan_max_speed": {"type": "integer", "label": "Pan Max Speed"},
            "tilt_max_speed": {"type": "integer", "label": "Tilt Max Speed"},
            "model_code": {
                "type": "string", "label": "Model Code",
                "help": "The model code the camera reports, in hexadecimal.",
            },
            "firmware_version": {
                "type": "string", "label": "ROM Version",
                "help": "The ROM version the camera reports, in hexadecimal.",
            },
            "switching_active": {
                "type": "boolean", "label": "Intelligent Switching", "control": True,
                "help": "Whether the host camera is switching between cameras on its own (host only).",
            },
            "switching_output": {
                "type": "integer", "label": "Switching Output", "min": 1, "max": 5, "control": True,
                "help": "The camera whose video the host is sending (1 is the host itself).",
            },
        },
        "child_entity_types": {
            "feed": {
                "label": "Video Feed",
                "label_plural": "Video Feeds",
                "id_format": {"type": "string", "max_length": 9},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "preview_url": {
                        "type": "string", "label": "Preview URL",
                        "help": "The feed's RTSP address, for the Video Panel.",
                    },
                    "preview_format": {"type": "string", "label": "Preview Format"},
                },
                "summary_fields": ["preview_url"],
            },
            "switch_camera": {
                "label": "Switching Camera",
                "label_plural": "Switching Cameras",
                "id_format": {"type": "integer", "min": 2, "max": 5},
                "state_variables": {
                    "ip": {
                        "type": "string", "label": "IP Address",
                        "help": "The camera's IP address as set on the host.",
                    },
                    "connected": {
                        "type": "boolean", "label": "Connected",
                        "help": "Whether the host reports the camera connected and operational.",
                    },
                },
                "summary_fields": ["ip", "connected"],
            },
        },
        "device_settings": {
            "ae_mode": {
                "type": "enum", "label": "Exposure Mode",
                "help": "Full auto, manual, shutter priority, iris priority or bright.",
                "values": ["full_auto", "manual", "shutter", "iris", "bright"],
                "state_key": "ae_mode", "default": "full_auto", "setup": False,
            },
            "wb_mode": {
                "type": "enum", "label": "White Balance Mode",
                "help": "Auto, indoor, outdoor, one push or manual.",
                "values": ["auto", "indoor", "outdoor", "one_push", "manual"],
                "state_key": "wb_mode", "default": "auto", "setup": False,
            },
            "backlight": {
                "type": "boolean", "label": "Backlight Compensation",
                "help": "Brightens a subject lit from behind.",
                "state_key": "backlight", "default": False, "setup": False,
            },
            "exp_comp": {
                "type": "boolean", "label": "Exposure Compensation",
                "help": "Applies the exposure compensation level.",
                "state_key": "exp_comp", "default": False, "setup": False,
            },
            "exp_comp_level": {
                "type": "integer", "label": "Exposure Compensation Level",
                "help": "-7 to +7.",
                "min": -7, "max": 7,
                "state_key": "exp_comp_level", "default": 0, "setup": False,
            },
            "ir_receiver": {
                "type": "boolean", "label": "IR Receiver",
                "help": "Whether the camera answers the IR remote (I12 and I12D-B).",
                "state_key": "ir_receiver", "default": True, "setup": False,
            },
            "mount_mode": {
                "type": "enum", "label": "Mount Mode",
                "help": "Ceiling inverts the video and the pan / tilt controls for an upside-down camera (P12 / P20).",
                "values": ["stand", "ceiling"],
                "state_key": "mount_mode", "default": "stand", "setup": False,
            },
        },
        "quick_actions": [
            "power_on", "power_off", "start_tracking", "pause_tracking",
            "recall_home_shot", "pt_home",
        ],
        "commands": {
            # ── Power / Privacy Mode ──
            "power_on": {
                "label": "Power On (Wake)", "params": {},
                "help": "Wakes the camera from Privacy Mode. Crestron's manual says to send it before any other command.",
                "sets": {"power": "on"},
            },
            "power_off": {
                "label": "Power Off (Privacy Mode)", "params": {},
                "help": "Puts the camera in Privacy Mode: the video output stops and the lightbar shows half red.",
                "sets": {"power": "standby"},
            },
            # ── Pan / tilt ──
            "pt_up": {
                "label": "Pan/Tilt Up",
                "params": {
                    "pan_speed": {"type": "integer", "min": 1, "max": 24},
                    "tilt_speed": {"type": "integer", "min": 1, "max": 20},
                },
                "help": "Starts tilting up until Pan/Tilt Stop.",
            },
            "pt_down": {
                "label": "Pan/Tilt Down",
                "params": {
                    "pan_speed": {"type": "integer", "min": 1, "max": 24},
                    "tilt_speed": {"type": "integer", "min": 1, "max": 20},
                },
                "help": "Starts tilting down until Pan/Tilt Stop.",
            },
            "pt_left": {
                "label": "Pan/Tilt Left",
                "params": {
                    "pan_speed": {"type": "integer", "min": 1, "max": 24},
                    "tilt_speed": {"type": "integer", "min": 1, "max": 20},
                },
                "help": "Starts panning left until Pan/Tilt Stop.",
            },
            "pt_right": {
                "label": "Pan/Tilt Right",
                "params": {
                    "pan_speed": {"type": "integer", "min": 1, "max": 24},
                    "tilt_speed": {"type": "integer", "min": 1, "max": 20},
                },
                "help": "Starts panning right until Pan/Tilt Stop.",
            },
            "pt_up_left": {
                "label": "Pan/Tilt Up-Left",
                "params": {
                    "pan_speed": {"type": "integer", "min": 1, "max": 24},
                    "tilt_speed": {"type": "integer", "min": 1, "max": 20},
                },
                "help": "Starts moving up and left until Pan/Tilt Stop.",
            },
            "pt_up_right": {
                "label": "Pan/Tilt Up-Right",
                "params": {
                    "pan_speed": {"type": "integer", "min": 1, "max": 24},
                    "tilt_speed": {"type": "integer", "min": 1, "max": 20},
                },
                "help": "Starts moving up and right until Pan/Tilt Stop.",
            },
            "pt_down_left": {
                "label": "Pan/Tilt Down-Left",
                "params": {
                    "pan_speed": {"type": "integer", "min": 1, "max": 24},
                    "tilt_speed": {"type": "integer", "min": 1, "max": 20},
                },
                "help": "Starts moving down and left until Pan/Tilt Stop.",
            },
            "pt_down_right": {
                "label": "Pan/Tilt Down-Right",
                "params": {
                    "pan_speed": {"type": "integer", "min": 1, "max": 24},
                    "tilt_speed": {"type": "integer", "min": 1, "max": 20},
                },
                "help": "Starts moving down and right until Pan/Tilt Stop.",
            },
            "pt_stop": {
                "label": "Pan/Tilt Stop", "params": {},
                "help": "Stops pan and tilt movement.",
            },
            "pt_home": {
                "label": "Pan/Tilt Home", "params": {},
                "help": "Moves the camera to its center position.",
            },
            "pt_absolute": {
                "label": "Pan/Tilt Absolute",
                "params": {
                    "pan": {"type": "integer", "required": True, "min": -32768, "max": 32767,
                            "help": "Pan position, 14.4 per degree (the camera pans -130 to 130 degrees)."},
                    "tilt": {"type": "integer", "required": True, "min": -32768, "max": 32767,
                             "help": "Tilt position, 14.4 per degree (the camera tilts -30 to 90 degrees)."},
                    "pan_speed": {"type": "integer", "min": 1, "max": 24},
                    "tilt_speed": {"type": "integer", "min": 1, "max": 20},
                },
                "help": "Moves to a pan and tilt position.",
            },
            "pt_relative": {
                "label": "Pan/Tilt Relative",
                "params": {
                    "pan": {"type": "integer", "required": True, "min": -32768, "max": 32767,
                            "help": "Pan distance, 14.4 per degree."},
                    "tilt": {"type": "integer", "required": True, "min": -32768, "max": 32767,
                             "help": "Tilt distance, 14.4 per degree."},
                    "pan_speed": {"type": "integer", "min": 1, "max": 24},
                    "tilt_speed": {"type": "integer", "min": 1, "max": 20},
                },
                "help": "Moves the camera by a pan and tilt distance from where it is.",
            },
            "pt_reset": {
                "label": "PTZ Correction", "params": {},
                "help": "Resets the pan / tilt head position.",
            },
            "set_pan_tilt_limit": {
                "label": "Set Pan/Tilt Limit",
                "params": {
                    "corner": {
                        "type": "enum", "required": True,
                        "values": [
                            {"value": "up_right", "label": "Up / Right"},
                            {"value": "down_left", "label": "Down / Left"},
                        ],
                    },
                    "pan": {"type": "integer", "required": True, "min": -32768, "max": 32767},
                    "tilt": {"type": "integer", "required": True, "min": -32768, "max": 32767},
                },
                "help": "Sets the camera's up-right or down-left travel limit to a pan and tilt position.",
            },
            # ── Zoom ──
            "zoom_in": {
                "label": "Zoom In",
                "params": {"speed": {"type": "integer", "min": 0, "max": 7,
                                     "help": "0 (slow) to 7 (fast); blank for the standard speed."}},
                "help": "Starts zooming in until Zoom Stop.",
            },
            "zoom_out": {
                "label": "Zoom Out",
                "params": {"speed": {"type": "integer", "min": 0, "max": 7,
                                     "help": "0 (slow) to 7 (fast); blank for the standard speed."}},
                "help": "Starts zooming out until Zoom Stop.",
            },
            "zoom_stop": {"label": "Zoom Stop", "params": {}, "help": "Stops zooming."},
            "zoom_direct": {
                "label": "Zoom to Position",
                "params": {"position": {"type": "integer", "required": True, "min": 0, "max": 16384,
                                        "help": "0 is fully wide; 16384 is full optical zoom."}},
                "help": "Zooms to a zoom position.",
                "sets": {"zoom_position": "{position}"},
            },
            "zoom_to_ratio": {
                "label": "Zoom to Ratio",
                "params": {"ratio": {"type": "number", "required": True, "min": 1, "max": 20,
                                     "unit": "x", "help": "The optical zoom ratio, up to 12 or 20 by lens."}},
                "help": "Zooms to an optical zoom ratio, using Crestron's position table for the model's lens.",
            },
            "zoom_focus_direct": {
                "label": "Zoom and Focus to Position",
                "params": {
                    "zoom": {"type": "integer", "required": True, "min": 0, "max": 16384},
                    "focus": {"type": "integer", "required": True, "min": 0, "max": 65535},
                },
                "help": "Moves zoom and focus to positions in one command.",
            },
            # ── Focus ──
            "focus_auto": {
                "label": "Auto Focus", "params": {},
                "help": "Turns auto focus on.", "sets": {"focus_mode": "auto"},
            },
            "focus_manual": {
                "label": "Manual Focus", "params": {},
                "help": "Turns auto focus off.", "sets": {"focus_mode": "manual"},
            },
            "focus_far": {
                "label": "Focus Far",
                "params": {"speed": {"type": "integer", "min": 0, "max": 7}},
                "help": "Starts focusing farther until Focus Stop (manual focus).",
            },
            "focus_near": {
                "label": "Focus Near",
                "params": {"speed": {"type": "integer", "min": 0, "max": 7}},
                "help": "Starts focusing nearer until Focus Stop (manual focus).",
            },
            "focus_stop": {"label": "Focus Stop", "params": {}, "help": "Stops a focus move."},
            "focus_direct": {
                "label": "Focus to Position",
                "params": {"position": {"type": "integer", "required": True, "min": 0, "max": 65535}},
                "help": "Moves focus to a focus position (manual focus).",
            },
            "focus_one_push": {
                "label": "One Push Auto Focus", "params": {},
                "help": "Focuses once on the current shot.",
            },
            # ── White balance ──
            "set_wb_mode": {
                "label": "Set White Balance Mode",
                "params": {"mode": {"type": "enum", "required": True,
                                    "values": ["auto", "indoor", "outdoor", "one_push", "manual"]}},
                "help": "Sets the white balance mode.",
                "sets": {"wb_mode": "{mode}"},
            },
            "wb_one_push_trigger": {
                "label": "One Push White Balance", "params": {},
                "help": "Measures white balance once, in One Push mode.",
            },
            "set_r_gain": {
                "label": "Set Red Gain",
                "params": {"value": {"type": "integer", "required": True, "min": 0, "max": 255}},
                "help": "Sets the red gain position (manual white balance).",
                "sets": {"r_gain": "{value}"},
            },
            "r_gain_up": {"label": "Red Gain Up", "params": {}, "help": "Raises the red gain one step."},
            "r_gain_down": {"label": "Red Gain Down", "params": {}, "help": "Lowers the red gain one step."},
            "r_gain_reset": {"label": "Red Gain Reset", "params": {}, "help": "Returns the red gain to its default."},
            "set_b_gain": {
                "label": "Set Blue Gain",
                "params": {"value": {"type": "integer", "required": True, "min": 0, "max": 255}},
                "help": "Sets the blue gain position (manual white balance).",
                "sets": {"b_gain": "{value}"},
            },
            "b_gain_up": {"label": "Blue Gain Up", "params": {}, "help": "Raises the blue gain one step."},
            "b_gain_down": {"label": "Blue Gain Down", "params": {}, "help": "Lowers the blue gain one step."},
            "b_gain_reset": {"label": "Blue Gain Reset", "params": {}, "help": "Returns the blue gain to its default."},
            # ── Exposure ──
            "set_ae_mode": {
                "label": "Set Exposure Mode",
                "params": {"mode": {"type": "enum", "required": True,
                                    "values": ["full_auto", "manual", "shutter", "iris", "bright"]}},
                "help": "Sets the exposure mode.",
                "sets": {"ae_mode": "{mode}"},
            },
            "set_shutter": {
                "label": "Set Shutter",
                "params": {"value": {"type": "integer", "required": True, "min": 0, "max": 255}},
                "help": "Sets the shutter position (manual or shutter priority).",
                "sets": {"shutter_position": "{value}"},
            },
            "shutter_up": {"label": "Shutter Up", "params": {}, "help": "Raises the shutter speed one step."},
            "shutter_down": {"label": "Shutter Down", "params": {}, "help": "Lowers the shutter speed one step."},
            "shutter_reset": {"label": "Shutter Reset", "params": {}, "help": "Returns the shutter to its default."},
            "set_iris": {
                "label": "Set Iris",
                "params": {"value": {"type": "integer", "required": True, "min": 0, "max": 255}},
                "help": "Sets the iris position (manual or iris priority).",
                "sets": {"iris_position": "{value}"},
            },
            "iris_up": {"label": "Iris Up", "params": {}, "help": "Opens the iris one step."},
            "iris_down": {"label": "Iris Down", "params": {}, "help": "Closes the iris one step."},
            "iris_reset": {"label": "Iris Reset", "params": {}, "help": "Returns the iris to its default."},
            "set_gain": {
                "label": "Set Gain",
                "params": {"value": {"type": "integer", "required": True, "min": 0, "max": 255}},
                "help": "Sets the gain position (manual exposure).",
                "sets": {"gain_position": "{value}"},
            },
            "gain_up": {"label": "Gain Up", "params": {}, "help": "Raises the gain one step."},
            "gain_down": {"label": "Gain Down", "params": {}, "help": "Lowers the gain one step."},
            "gain_reset": {"label": "Gain Reset", "params": {}, "help": "Returns the gain to its default."},
            "set_bright": {
                "label": "Set Bright",
                "params": {"value": {"type": "integer", "required": True, "min": 0, "max": 255}},
                "help": "Sets the brightness position (bright exposure mode).",
                "sets": {"bright_position": "{value}"},
            },
            "bright_up": {"label": "Bright Up", "params": {}, "help": "Raises the brightness one step."},
            "bright_down": {"label": "Bright Down", "params": {}, "help": "Lowers the brightness one step."},
            "bright_reset": {"label": "Bright Reset", "params": {}, "help": "Returns the brightness to its default."},
            "set_exp_comp": {
                "label": "Set Exposure Compensation",
                "params": {"enabled": {"type": "boolean", "required": True}},
                "help": "Turns exposure compensation on or off.",
                "sets": {"exp_comp": "{enabled}"},
            },
            "set_exp_comp_level": {
                "label": "Set Exposure Compensation Level",
                "params": {"level": {"type": "integer", "required": True, "min": -7, "max": 7}},
                "help": "Sets the exposure compensation level, -7 to +7.",
                "sets": {"exp_comp_level": "{level}"},
            },
            "exp_comp_up": {"label": "Exposure Compensation Up", "params": {}, "help": "Raises exposure compensation one step."},
            "exp_comp_down": {"label": "Exposure Compensation Down", "params": {}, "help": "Lowers exposure compensation one step."},
            "exp_comp_reset": {"label": "Exposure Compensation Reset", "params": {}, "help": "Returns exposure compensation to 0."},
            "set_backlight": {
                "label": "Set Backlight Compensation",
                "params": {"enabled": {"type": "boolean", "required": True}},
                "help": "Turns backlight compensation on or off.",
                "sets": {"backlight": "{enabled}"},
            },
            # ── Presets ──
            "recall_preset": {
                "label": "Recall Preset",
                "params": {"number": {"type": "integer", "required": True, "min": 0, "max": 254,
                                      "help": "0 to 254. (255 cannot be sent: FF ends every VISCA packet.)"}},
                "help": "Moves the camera to a saved preset.",
                "sets": {"last_preset": "{number}"},
            },
            "save_preset": {
                "label": "Save Preset",
                "params": {"number": {"type": "integer", "required": True, "min": 0, "max": 254,
                                      "help": "0 to 254. (255 cannot be sent: FF ends every VISCA packet.)"}},
                "help": "Saves the current shot to a preset.",
            },
            "delete_preset": {
                "label": "Delete Preset",
                "confirm": "Deletes this preset from the camera.",
                "params": {"number": {"type": "integer", "required": True, "min": 0, "max": 254,
                                      "help": "0 to 254. (255 cannot be sent: FF ends every VISCA packet.)"}},
                "help": "Deletes a saved preset.",
            },
            "set_freeze": {
                "label": "Set Freeze",
                "params": {"enabled": {"type": "boolean", "required": True}},
                "help": "Freezes the video output on the current picture, or releases it.",
            },
            "set_preset_freeze": {
                "label": "Set Preset Freeze",
                "params": {"enabled": {"type": "boolean", "required": True}},
                "help": "Holds the picture while the camera moves to a preset, or stops holding it.",
            },
            # ── Intelligent functions (reserved presets) ──
            "start_tracking": {
                "label": "Start Tracking", "params": {},
                "help": "Starts Group Framing or Presenter Tracking, whichever mode the camera is set to.",
                "sets": {"tracking": "active"},
            },
            "pause_tracking": {
                "label": "Pause Tracking", "params": {},
                "help": "Pauses Group Framing or Presenter Tracking. The video keeps running.",
                "sets": {"tracking": "paused"},
            },
            "start_group_tracking": {
                "label": "Start Group Tracking", "params": {},
                "help": "Starts Group Tracking (I20).",
            },
            "pause_group_tracking": {
                "label": "Pause Group Tracking", "params": {},
                "help": "Pauses Group Tracking (I20).",
            },
            "enable_group_framing": {
                "label": "Enable Group Framing", "params": {},
                "help": "Switches the I12D-B to Group Framing.",
            },
            "enable_speaker_tracking": {
                "label": "Enable Speaker Tracking", "params": {},
                "help": "Switches the I12D-B to Speaker Tracking.",
            },
            "recall_home_shot": {
                "label": "Recall Home Shot", "params": {},
                "help": "Moves the camera to the Home Shot (preset 0).",
                "sets": {"last_preset": 0},
            },
            "set_home_shot": {
                "label": "Set Home Shot", "params": {},
                "help": "Saves the current shot as the Home Shot (preset 0), which tracking needs.",
            },
            "set_tracking_shot": {
                "label": "Set Tracking Shot", "params": {},
                "help": "Saves the current shot as the Tracking Shot (preset 1), which tracking needs.",
            },
            "recall_preset_zone": {
                "label": "Recall Preset Zone",
                "params": {"zone": {"type": "integer", "required": True, "min": 1, "max": 4}},
                "help": "Runs preset zone 1 to 4 (I20).",
            },
            "select_tracking_profile": {
                "label": "Select Tracking Profile",
                "params": {"profile": {"type": "integer", "required": True, "min": 1, "max": 4}},
                "help": "Selects tracking profile 1 to 4 (I20).",
            },
            "toggle_osd_menu": {
                "label": "Toggle On-Screen Menu", "params": {},
                "help": "Opens or closes the camera's on-screen menu, which is shown over its video output.",
            },
            "reboot": {
                "label": "Reboot", "params": {},
                "help": "Restarts the camera.",
                "restarts_device_for": 90,
            },
            # ── Mount / IR ──
            "set_mount_mode": {
                "label": "Set Mount Mode",
                "params": {"mode": {"type": "enum", "required": True, "values": ["stand", "ceiling"]}},
                "help": "Ceiling inverts the video and the pan / tilt controls for an upside-down camera (P12 / P20).",
                "sets": {"mount_mode": "{mode}"},
            },
            "set_ir_receiver": {
                "label": "Set IR Receiver",
                "params": {"enabled": {"type": "boolean", "required": True}},
                "help": "Turns the camera's IR receiver on or off.",
                "sets": {"ir_receiver": "{enabled}"},
            },
            # ── Lightbar ──
            "set_lightbar": {
                "label": "Set Lightbar",
                "params": {
                    "width": {"type": "enum", "required": True, "values": ["full", "half", "off"]},
                    "color": {"type": "enum", "values": ["green", "red", "yellow"], "default": "green"},
                    "brightness": {"type": "enum", "values": ["bright", "medium", "dim"], "default": "bright"},
                },
                "help": "Sets the front lightbar's width, colour and brightness.",
            },
            "set_lightbar_segments": {
                "label": "Set Lightbar Segments",
                "params": {
                    "segment_1": {
                        "type": "enum", "required": True, "label": "Segment 1 (outer)",
                        "values": ["off", "green_dim", "green_medium", "green_bright",
                                   "red_dim", "red_medium", "red_bright",
                                   "yellow_dim", "yellow_medium", "yellow_bright"],
                    },
                    "segment_2": {
                        "type": "enum", "required": True, "label": "Segment 2 (inner)",
                        "values": ["off", "green_dim", "green_medium", "green_bright",
                                   "red_dim", "red_medium", "red_bright",
                                   "yellow_dim", "yellow_medium", "yellow_bright"],
                    },
                    "segment_3": {
                        "type": "enum", "required": True, "label": "Segment 3 (inner)",
                        "values": ["off", "green_dim", "green_medium", "green_bright",
                                   "red_dim", "red_medium", "red_bright",
                                   "yellow_dim", "yellow_medium", "yellow_bright"],
                    },
                    "segment_4": {
                        "type": "enum", "required": True, "label": "Segment 4 (outer)",
                        "values": ["off", "green_dim", "green_medium", "green_bright",
                                   "red_dim", "red_medium", "red_bright",
                                   "yellow_dim", "yellow_medium", "yellow_bright"],
                    },
                },
                "help": "Sets each of the lightbar's four segments to its own colour and brightness. Segments 1 and 4 are the outer ones.",
            },
            # ── Intelligent Switching (host) ──
            "set_switching_camera": {
                "label": "Set Switching Camera",
                "params": {
                    "camera": {"type": "integer", "required": True, "min": 2, "max": 5,
                               "help": "Camera 2 to 5 (camera 2 has the highest priority)."},
                    "ip": {"type": "string", "required": True,
                           "pattern": "^\\s*\\d{1,3}(\\.\\d{1,3}){3}\\s*$",
                           "help": "The camera's IPv4 address."},
                },
                "help": "Sets the IP address the host uses for switching camera 2 to 5.",
            },
            "clear_switching_cameras": {
                "label": "Clear Switching Cameras", "params": {},
                "confirm": "Clears every switching camera's IP address from this host camera.",
                "help": "Clears every camera set for Intelligent Switching.",
            },
            "switch_to_camera": {
                "label": "Switch to Camera",
                "params": {"camera": {"type": "integer", "required": True, "min": 1, "max": 5,
                                      "help": "1 is the host camera."}},
                "help": "Sends that camera's video from the host camera's output.",
                "sets": {"switching_output": "{camera}"},
            },
            "resume_switching": {
                "label": "Resume Intelligent Switching", "params": {},
                "help": "Resumes automatic switching between cameras.",
                "sets": {"switching_active": True},
            },
            "pause_switching": {
                "label": "Pause Intelligent Switching", "params": {},
                "help": "Pauses automatic switching and holds the current camera.",
                "sets": {"switching_active": False},
            },
            "show_group_framing_feed": {
                "label": "Show Group Framing Feed", "params": {},
                "help": "Pauses Intelligent Switching and sends the Group Framing (host) camera's video. Tracking stays on.",
            },
            "show_presenter_feed": {
                "label": "Show Presenter Feed", "params": {},
                "help": "Pauses Intelligent Switching and sends the Presenter Tracking camera's video. Tracking stays on.",
            },
            # ── Interface ──
            "clear_interface": {
                "label": "Clear Command Buffers", "params": {},
                "help": "Clears the camera's VISCA command buffers (IF_Clear).",
            },
        },
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._lock: asyncio.Lock | None = None
        self._pending: tuple[asyncio.Future, bool] | None = None
        # Inquiries this camera refused as a syntax error on this connection.
        self._unsupported: set[str] = set()
        self._poll_count = 0
        self._narrow()

    # ── Model narrowing ──

    @property
    def _model(self) -> str:
        """The picked model, or "" when none is set (the camera then offers
        the full declaration and does not connect)."""
        model = str(self.config.get("model") or "").strip().lower()
        return model if model in _MODELS else ""

    def _switching_host(self) -> bool:
        return self._model in _SWITCHING_HOSTS and _as_bool(self.config.get("switching_host", False))

    def _offers(self, command: str) -> bool:
        models = _COMMAND_MODELS.get(command)
        if models is not None and self._model not in models:
            return False
        if command in _SWITCHING_COMMANDS:
            return self._switching_host()
        return True

    def _narrow(self) -> None:
        """Offer only the picked model's commands and settings."""
        base = type(self).DRIVER_INFO
        model = self._model
        if not model:
            self.DRIVER_INFO = base
            return
        commands: dict[str, Any] = {}
        for command_id, cdef in base["commands"].items():
            if not self._offers(command_id):
                continue
            if command_id == "zoom_to_ratio":
                lens_max = _ZOOM_TABLES[_MODEL_LENS[model]][-1][0]
                cdef = {
                    **cdef,
                    "params": {"ratio": {**cdef["params"]["ratio"], "max": lens_max}},
                }
            commands[command_id] = cdef
        settings = {
            key: sdef for key, sdef in base["device_settings"].items()
            if model in _SETTING_MODELS.get(key, _MODELS)
        }
        quick = [
            c for c in base["quick_actions"]
            if c in commands and not (model in _I_SERIES and c in _QUICK_SKIP_I_SERIES)
        ]
        self.DRIVER_INFO = {
            **base,
            "commands": commands,
            "device_settings": settings,
            "quick_actions": quick,
        }

    # ── Addressing ──

    @property
    def _address(self) -> int:
        try:
            address = int(self.config.get("camera_address", 1))
        except (TypeError, ValueError):
            address = 1
        return max(1, min(7, address))

    @property
    def _reply_head(self) -> int:
        # "z = Device address + 8": address 1 answers 0x90, address 2 0xA0.
        return (self._address + 8) << 4

    def _packet(self, body: bytes) -> bytes:
        return bytes([0x80 | self._address]) + body + b"\xff"

    def _resolve_delimiter(self) -> bytes:
        return b"\xff"

    # ── Connection lifecycle ──

    async def _pre_connect(self) -> None:
        if not self._model:
            raise ConnectionFaultError(
                "Set Model to the camera's model (I12, I20, P12, P20 or I12D-B).",
                code="invalid_config",
            )

    async def _initial_sync(self) -> None:
        self._lock = asyncio.Lock()
        self._unsupported = set()
        self._poll_count = 0
        self._publish_feeds()
        if self._switching_host():
            for camera in range(2, 6):
                self.register_child("switch_camera", camera)
        reply = await self._ask(b"\x09\x00\x02")
        if reply is not None and reply[0] == "data" and len(reply[1]) >= 6:
            data = reply[1]
            self.set_states({
                "model_code": data[2:4].hex().upper(),
                "firmware_version": data[4:6].hex().upper(),
            })
        reply = await self._ask(b"\x09\x06\x11")
        if reply is not None and reply[0] == "data" and len(reply[1]) >= 2:
            self.set_states({
                "pan_max_speed": reply[1][0],
                "tilt_max_speed": reply[1][1],
            })
        try:
            await self.poll(slow=True)
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Initial poll failed")

    async def _close_session(self) -> None:
        # End a wait in progress as "no answer" rather than cancelling the
        # task that is waiting.
        pending = self._pending
        if pending is not None and not pending[0].done():
            pending[0].set_result(None)
        self._pending = None

    def _link_up(self) -> bool:
        return bool(self.transport and self.transport.connected)

    # ── Feeds ──

    def _feed_url(self, index: int) -> str:
        host = str(self.config.get("host") or "").strip()
        if not host:
            return ""
        try:
            port = int(self.config.get("rtsp_port") or 554)
        except (TypeError, ValueError):
            port = 554
        encoding = "h265" if self.config.get("stream_encoding") == "h265" else "h264"
        login = ""
        password = str(self.config.get("camera_password") or "")
        if password and _as_bool(self.config.get("credentials_in_stream_url", False)):
            login = f"admin:{quote(password, safe='')}@"
        return f"rtsp://{login}{host}:{port}/{index}.{encoding}"

    def _publish_feeds(self) -> None:
        for feed_id, index, name in _FEEDS[self._model]:
            values = {
                "name": name,
                "preview_url": self._feed_url(index),
                "preview_format": "rtsp",
            }
            self.register_child("feed", feed_id, initial_state=values)
            self.set_child_state_batch("feed", feed_id, values)

    # ── Exchanges ──

    async def on_data_received(self, data: bytes) -> None:
        # The frame parser strips the trailing FF.
        if len(data) < 2 or data[0] != self._reply_head:
            return
        pending = self._pending
        if pending is None or pending[0].done():
            return
        future, inquiry = pending
        kind, socket = data[1] & 0xF0, data[1] & 0x0F
        if kind == 0x60:
            # An inquiry's errors come on socket 0; a command's on its socket.
            if inquiry and socket != 0:
                return
            future.set_result(("error", data[2] if len(data) > 2 else 0))
        elif kind == 0x50:
            if inquiry and socket == 0 and len(data) > 2:
                future.set_result(("data", bytes(data[2:])))
            # A completion (no payload) after a command's ACK is not waited for.
        elif kind == 0x40 and not inquiry:
            future.set_result(("ack",))

    async def _exchange(
        self, body: bytes, *, inquiry: bool, timeout: float | None = None,
        broadcast: bool = False,
    ) -> tuple | None:
        """Send one packet and wait for its reply: ``("ack",)``,
        ``("data", payload)`` or ``("error", code)``; None when nothing
        answered. One exchange at a time, because a reply does not say what
        it answers. ``broadcast`` sends ``body`` as the whole packet."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if not self._link_up():
                raise ConnectionError(f"[{self.device_id}] Not connected")
            future = asyncio.get_running_loop().create_future()
            self._pending = (future, inquiry)
            try:
                await self.transport.send(body if broadcast else self._packet(body))
                try:
                    return await asyncio.wait_for(
                        future, REPLY_TIMEOUT_S if timeout is None else timeout
                    )
                except asyncio.TimeoutError:
                    return None
            finally:
                self._pending = None

    async def _ask(self, body: bytes) -> tuple | None:
        """An inquiry; a transport failure reads as no answer."""
        try:
            return await self._exchange(body, inquiry=True)
        except (ConnectionError, OSError):
            return None

    def _label(self, command: str) -> str:
        cdef = type(self).DRIVER_INFO["commands"].get(command, {})
        return cdef.get("label", command)

    async def _command(
        self, command: str, body: bytes, *, require_ack: bool = True,
        broadcast: bool = False,
    ) -> None:
        """Send a command and wait for the camera to accept it. A refusal
        raises with the camera's reason. ``require_ack=False`` is for packets
        the document gives no reply for: silence is not a failure there, a
        VISCA error still is."""
        reply = await self._exchange(
            body, inquiry=False, broadcast=broadcast,
            timeout=REPLY_TIMEOUT_S if require_ack else OPTIONAL_REPLY_TIMEOUT_S,
        )
        label = self._label(command)
        if reply is None and not require_ack:
            return
        if reply is None:
            if self.get_state("power") == "standby" and command != "power_on":
                raise _CameraRefused(
                    f"The camera did not answer {label}: it is in Privacy Mode. "
                    "Send Power On first."
                )
            raise _CameraRefused(
                f"The camera did not answer {label}. Check that Camera Address "
                "matches the camera's OSD and that it is not in Privacy Mode."
            )
        if reply[0] != "error":
            return
        code = reply[1]
        if code == 0x41:
            if self.get_state("power") == "standby":
                raise _CameraRefused(
                    f"The camera refused {label}: it is in Privacy Mode. Send Power On first."
                )
            raise _CameraRefused(
                f"The camera cannot run {label} right now (for example, a manual "
                "focus command while auto focus is on)."
            )
        if code == 0x03:
            raise _CameraRefused(
                f"The camera was busy with two other commands and refused {label}. Send it again."
            )
        if code == 0x02:
            raise _CameraRefused(
                f"The camera refused {label} as a syntax error: this camera does not "
                "accept that command or value."
            )
        reason = _ERRORS.get(code, f"error {code:#04x}")
        raise _CameraRefused(f"The camera refused {label} ({reason}).")

    # ── Commands ──

    def _speeds(self, params: dict[str, Any]) -> bytes:
        pan = params.get("pan_speed")
        tilt = params.get("tilt_speed")
        if pan is None:
            pan = self.config.get("pan_speed", 12)
        if tilt is None:
            tilt = self.config.get("tilt_speed", 10)
        return bytes([max(1, min(0x18, int(pan))), max(1, min(0x14, int(tilt)))])

    def _check_preset(self, number: int, action: str) -> None:
        reserved = _RESERVED.get(self._model, frozenset())
        if action == "recall" and number == 99:
            raise ValueError("Preset 99 restarts the camera. Use Reboot.")
        if action != "recall" and number in reserved:
            name = _PRESET_NAMES.get(number, "a reserved preset")
            raise ValueError(
                f"Preset {number} is reserved for {name} on this camera and cannot be {action}d."
            )
        if action == "delete" and number in (0, 1) and self._model in _I_SERIES:
            shot = "Home Shot" if number == 0 else "Tracking Shot"
            raise ValueError(
                f"Preset {number} is the {shot}, which tracking needs. Save over it instead."
            )

    async def _recall(self, command: str, number: int) -> None:
        await self._command(command, bytes([0x01, 0x04, 0x3F, 0x02, number]))

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if command not in self.DRIVER_INFO["commands"]:
            if command in type(self).DRIVER_INFO["commands"]:
                raise ValueError(f"{self._label(command)} is not available on this camera model.")
            raise ValueError(f"Unknown command: {command}")

        if command in _PT_DIR:
            pan_dir, tilt_dir = _PT_DIR[command]
            await self._command(
                command, b"\x01\x06\x01" + self._speeds(params) + bytes([pan_dir, tilt_dir])
            )
            return

        level_cmd = self._level_command(command, params)
        if level_cmd is not None:
            await self._command(command, level_cmd)
            return

        match command:
            case "power_on":
                await self._command(command, b"\x01\x04\x00\x02")
                self.set_state("power", "on")
            case "power_off":
                await self._command(command, b"\x01\x04\x00\x03")
                self.set_state("power", "standby")

            case "pt_home":
                await self._command(command, b"\x01\x06\x04")
            case "pt_reset":
                await self._command(command, b"\x01\x06\x05")
            case "pt_absolute" | "pt_relative":
                op = 0x02 if command == "pt_absolute" else 0x03
                await self._command(
                    command,
                    bytes([0x01, 0x06, op]) + self._speeds(params)
                    + _encode_4nibble(int(params["pan"])) + _encode_4nibble(int(params["tilt"])),
                )
            case "set_pan_tilt_limit":
                corner = 0x01 if str(params["corner"]) == "up_right" else 0x00
                await self._command(
                    command,
                    bytes([0x01, 0x06, 0x07, 0x00, corner])
                    + _encode_4nibble(int(params["pan"])) + _encode_4nibble(int(params["tilt"])),
                )

            case "zoom_in" | "zoom_out" | "focus_far" | "focus_near":
                group = 0x07 if command.startswith("zoom") else 0x08
                standard = 0x02 if command in ("zoom_in", "focus_far") else 0x03
                speed = params.get("speed")
                if speed is None:
                    code = standard
                else:
                    code = (0x20 if standard == 0x02 else 0x30) | max(0, min(7, int(speed)))
                await self._command(command, bytes([0x01, 0x04, group, code]))
            case "zoom_stop":
                await self._command(command, b"\x01\x04\x07\x00")
            case "focus_stop":
                await self._command(command, b"\x01\x04\x08\x00")
            case "zoom_direct":
                await self._command(command, b"\x01\x04\x47" + _encode_4nibble(int(params["position"])))
            case "zoom_to_ratio":
                position = zoom_position_for(float(params["ratio"]), _MODEL_LENS[self._model])
                await self._command(command, b"\x01\x04\x47" + _encode_4nibble(position))
            case "zoom_focus_direct":
                await self._command(
                    command,
                    b"\x01\x04\x47" + _encode_4nibble(int(params["zoom"]))
                    + _encode_4nibble(int(params["focus"])),
                )
            case "focus_direct":
                await self._command(command, b"\x01\x04\x48" + _encode_4nibble(int(params["position"])))
            case "focus_auto":
                await self._command(command, b"\x01\x04\x38\x02")
            case "focus_manual":
                await self._command(command, b"\x01\x04\x38\x03")
            case "focus_one_push":
                await self._command(command, b"\x01\x04\x18\x01")

            case "set_wb_mode":
                mode = str(params["mode"])
                if mode not in _WB_MODES:
                    raise ValueError(f"Unknown white balance mode: {mode}")
                await self._command(command, bytes([0x01, 0x04, 0x35, _WB_MODES[mode]]))
            case "wb_one_push_trigger":
                await self._command(command, b"\x01\x04\x10\x05")
            case "set_ae_mode":
                mode = str(params["mode"])
                if mode not in _AE_MODES:
                    raise ValueError(f"Unknown exposure mode: {mode}")
                await self._command(command, bytes([0x01, 0x04, 0x39, _AE_MODES[mode]]))
            case "set_exp_comp":
                on = _as_bool(params["enabled"])
                await self._command(command, b"\x01\x04\x3e" + (b"\x02" if on else b"\x03"))
            case "set_exp_comp_level":
                level = max(-7, min(7, int(params["level"])))
                await self._command(command, self._level_direct("exp_comp", level + 7))
            case "set_backlight":
                on = _as_bool(params["enabled"])
                await self._command(command, b"\x01\x04\x33" + (b"\x02" if on else b"\x03"))

            case "recall_preset":
                number = int(params["number"])
                self._check_preset(number, "recall")
                await self._recall(command, number)
            case "save_preset":
                number = int(params["number"])
                self._check_preset(number, "save")
                await self._command(command, bytes([0x01, 0x04, 0x3F, 0x01, number]))
            case "delete_preset":
                number = int(params["number"])
                self._check_preset(number, "delete")
                await self._command(command, bytes([0x01, 0x04, 0x3F, 0x00, number]))
            case "set_freeze":
                on = _as_bool(params["enabled"])
                await self._command(command, b"\x01\x04\x62" + (b"\x02" if on else b"\x03"))
            case "set_preset_freeze":
                on = _as_bool(params["enabled"])
                await self._command(command, b"\x01\x04\x62" + (b"\x22" if on else b"\x23"))

            case "start_tracking":
                await self._recall(command, 80)
            case "pause_tracking":
                await self._recall(command, 81)
            case "start_group_tracking":
                await self._recall(command, 82)
            case "pause_group_tracking":
                await self._recall(command, 83)
            case "show_group_framing_feed":
                await self._recall(command, 85)
            case "show_presenter_feed":
                await self._recall(command, 86)
            case "enable_group_framing":
                await self._recall(command, 87)
            case "enable_speaker_tracking":
                await self._recall(command, 89)
            case "toggle_osd_menu":
                await self._recall(command, 95)
            case "reboot":
                await self._recall(command, 99)
            case "recall_home_shot":
                await self._recall(command, 0)
            case "set_home_shot":
                await self._command(command, b"\x01\x04\x3f\x01\x00")
            case "set_tracking_shot":
                await self._command(command, b"\x01\x04\x3f\x01\x01")
            case "recall_preset_zone":
                await self._recall(command, 100 + max(1, min(4, int(params["zone"]))))
            case "select_tracking_profile":
                await self._recall(command, 104 + max(1, min(4, int(params["profile"]))))

            case "set_mount_mode":
                mode = str(params["mode"])
                if mode not in ("stand", "ceiling"):
                    raise ValueError(f"Unknown mount mode: {mode}")
                await self._command(command, b"\x01\x04\xa4" + (b"\x02" if mode == "stand" else b"\x03"))
            case "set_ir_receiver":
                on = _as_bool(params["enabled"])
                await self._command(command, b"\x01\x06\x08" + (b"\x02" if on else b"\x03"))

            case "set_lightbar":
                width = str(params["width"])
                color = str(params.get("color") or "green")
                level = str(params.get("brightness") or "bright")
                if width not in ("full", "half", "off") or color not in _LIGHT_COLOR \
                        or level not in ("bright", "medium", "dim"):
                    raise ValueError("Unknown lightbar width, colour or brightness")
                # The document gives no reply for a lightbar command.
                await self._command(
                    command, b"\xc1" + lightbar_bytes(width, color, level), require_ack=False
                )
            case "set_lightbar_segments":
                segments = bytes(segment_from_choice(str(params[f"segment_{n}"])) for n in range(1, 5))
                await self._command(command, b"\xc1" + segments, require_ack=False)

            case "set_switching_camera":
                camera = int(params["camera"])
                if not 2 <= camera <= 5:
                    raise ValueError("Switching cameras are numbered 2 to 5.")
                await self._command(command, bytes([0xC2, 0x01, 0x09, camera]) + encode_ip(str(params["ip"])))
                self.set_child_state("switch_camera", camera, "ip", str(params["ip"]).strip())
            case "clear_switching_cameras":
                await self._command(command, b"\xc2\x01\x0a\x00")
                for camera in range(2, 6):
                    self.set_child_state_batch("switch_camera", camera, {"ip": "", "connected": False})
            case "switch_to_camera":
                camera = max(1, min(5, int(params["camera"])))
                await self._command(command, bytes([0xC2, 0x01, 0x08, camera]))
            case "resume_switching":
                await self._command(command, b"\xc2\x01\x08\x00")
            case "pause_switching":
                await self._command(command, b"\xc2\x01\x0b\x00")

            case "clear_interface":
                # A broadcast (88): every camera on a serial chain answers it
                # with its own copy, which is not an ACK.
                await self._command(
                    command, b"\x88\x01\x00\x01\xff", require_ack=False, broadcast=True
                )

            case _:
                raise ValueError(f"Unknown command: {command}")

    def _level_direct(self, name: str, value: int) -> bytes:
        direct = _LEVELS[name][1]
        value = max(0, min(255, value))
        return bytes([0x01, 0x04, direct, 0x00, 0x00, (value >> 4) & 0x0F, value & 0x0F])

    def _level_command(self, command: str, params: dict[str, Any]) -> bytes | None:
        """The packet body for a ``<level>_up`` / ``_down`` / ``_reset`` or
        ``set_<level>`` command; None for any other command. (``set_exp_comp``
        is the on / off switch, handled with the other switches.)"""
        for name, (step, _direct) in _LEVELS.items():
            for suffix, code in _STEP.items():
                if command == f"{name}_{suffix}":
                    return bytes([0x01, 0x04, step, code])
            if command == f"set_{name}" and name != "exp_comp":
                return self._level_direct(name, int(params["value"]))
        return None

    # ── Device settings ──

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if key not in self.DRIVER_INFO["device_settings"]:
            raise ValueError(f"Unknown device setting: {key}")
        if key == "ae_mode":
            await self.send_command("set_ae_mode", {"mode": str(value)})
        elif key == "wb_mode":
            await self.send_command("set_wb_mode", {"mode": str(value)})
        elif key == "backlight":
            await self.send_command("set_backlight", {"enabled": _as_bool(value)})
        elif key == "exp_comp":
            await self.send_command("set_exp_comp", {"enabled": _as_bool(value)})
        elif key == "exp_comp_level":
            await self.send_command("set_exp_comp_level", {"level": int(value)})
        elif key == "ir_receiver":
            await self.send_command("set_ir_receiver", {"enabled": _as_bool(value)})
        elif key == "mount_mode":
            await self.send_command("set_mount_mode", {"mode": str(value)})

    # ── Polling ──

    def _poll_plan(self, slow: bool) -> list[tuple[str, bytes]]:
        model = self._model
        plan = [
            ("pt_position", b"\x09\x06\x12"),
            ("zoom_position", b"\x09\x04\x47"),
            ("focus_position", b"\x09\x04\x48"),
            ("focus_mode", b"\x09\x04\x38"),
            ("ae_mode", b"\x09\x04\x39"),
            ("wb_mode", b"\x09\x04\x35"),
            ("r_gain", b"\x09\x04\x43"),
            ("b_gain", b"\x09\x04\x44"),
            ("shutter_position", b"\x09\x04\x4a"),
            ("iris_position", b"\x09\x04\x4b"),
            ("gain_position", b"\x09\x04\x4c"),
            ("bright_position", b"\x09\x04\x4d"),
            ("exp_comp", b"\x09\x04\x3e"),
            ("exp_comp_level", b"\x09\x04\x4e"),
            ("backlight", b"\x09\x04\x33"),
            ("last_preset", b"\x09\x04\x3f"),
            ("video_format", b"\x09\x06\x23"),
        ]
        if model in _I_SERIES:
            plan.insert(0, ("tracking", b"\x09\x08\x01"))
        if model in _P_SERIES:
            plan.append(("mount_mode", b"\x09\x04\xa4"))
        if model in ("i12", "i12d"):
            plan.append(("ir_receiver", b"\x09\x06\x08"))
        if self._switching_host():
            plan.append(("switching_output", b"\xc2\x09\x08"))
            for camera in range(2, 6):
                plan.append((f"connection_{camera}", bytes([0xC2, 0x09, 0x0D, camera])))
            if slow:
                for camera in range(2, 6):
                    plan.append((f"camera_ip_{camera}", bytes([0xC2, 0x09, 0x09, camera])))
        return plan

    def _apply(self, key: str, data: bytes) -> None:
        """Write one inquiry's reply payload (after ``y0 50``) to state."""
        model = self._model
        if key == "pt_position" and len(data) >= 8:
            pan = _decode_4nibble(data[0:4], signed=True)
            tilt = _decode_4nibble(data[4:8], signed=True)
            self.set_states({
                "pan_position": pan, "tilt_position": tilt,
                "pan_angle": round(pan / 14.4, 1), "tilt_angle": round(tilt / 14.4, 1),
            })
        elif key in ("zoom_position", "focus_position") and len(data) >= 4:
            position = _decode_4nibble(data[0:4])
            self.set_state(key, position)
            if key == "zoom_position" and model in _MODEL_LENS:
                self.set_state("zoom_ratio", zoom_ratio_for(position, _MODEL_LENS[model]))
        elif key == "focus_mode":
            if data[0] in (0x02, 0x03):
                self.set_state("focus_mode", "auto" if data[0] == 0x02 else "manual")
        elif key == "ae_mode":
            if data[0] in _AE_FROM:
                self.set_state("ae_mode", _AE_FROM[data[0]])
        elif key == "wb_mode":
            if data[0] in _WB_FROM:
                self.set_state("wb_mode", _WB_FROM[data[0]])
        elif key in ("r_gain", "b_gain", "shutter_position", "iris_position",
                     "gain_position", "bright_position") and len(data) >= 4:
            self.set_state(key, _decode_byte_nibbles(data[2:4]))
        elif key == "exp_comp_level" and len(data) >= 4:
            self.set_state(key, max(-7, min(7, _decode_byte_nibbles(data[2:4]) - 7)))
        elif key in ("exp_comp", "backlight", "ir_receiver"):
            if data[0] in (0x02, 0x03):
                self.set_state(key, data[0] == 0x02)
        elif key == "last_preset":
            self.set_state(key, data[0])
        elif key == "tracking":
            if data[0] in (0x02, 0x03):
                self.set_state("tracking", "active" if data[0] == 0x02 else "paused")
        elif key == "mount_mode":
            if data[0] in (0x02, 0x03):
                self.set_state("mount_mode", "stand" if data[0] == 0x02 else "ceiling")
        elif key == "video_format":
            fmt = _VIDEO_FORMATS.get(data[0])
            if fmt:
                self.set_state("video_format", fmt)
        elif key == "switching_output" and len(data) >= 2:
            self.set_states({"switching_active": data[0] == 0x01, "switching_output": data[1]})
        elif key.startswith("connection_") and len(data) >= 2:
            self.set_child_state("switch_camera", int(key[-1]), "connected", data[1] == 0x01)
        elif key.startswith("camera_ip_") and len(data) >= 9:
            ip = decode_ip(data[1:9])
            self.set_child_state("switch_camera", int(key[-1]), "ip", "" if ip == "0.0.0.0" else ip)

    def _apply_power(self, data: bytes) -> None:
        power = {0x02: "on", 0x03: "standby", 0x04: "fault"}.get(data[0])
        if power:
            self.set_state("power", power)

    async def poll(self, slow: bool | None = None) -> None:
        if not self._link_up():
            return
        if slow is None:
            self._poll_count += 1
            slow = self._poll_count % _SLOW_POLL_EVERY == 0
        reply = await self._ask(POWER_INQUIRY)
        if reply is None:
            # Silence ends the poll; the liveness probe decides whether the
            # camera is gone (a camera in Privacy Mode may answer nothing).
            return
        if reply[0] == "data":
            self._apply_power(reply[1])
        if self.get_state("power") != "on":
            # Privacy Mode takes no other command (and a fault, no reading).
            return
        for key, body in self._poll_plan(slow):
            if key in self._unsupported:
                continue
            reply = await self._ask(body)
            if reply is None:
                return
            if reply[0] == "error":
                if reply[1] == 0x02:
                    self._unsupported.add(key)
                continue
            if reply[0] == "data" and reply[1]:
                self._apply(key, reply[1])

    async def _liveness_probe(self) -> None:
        reply = await self._ask(POWER_INQUIRY)
        if reply is not None:
            if reply[0] == "data":
                self._apply_power(reply[1])
            return
        if self.get_state("power") == "standby":
            # The document does not say a camera in Privacy Mode answers.
            return
        raise ConnectionError(f"[{self.device_id}] The camera is not responding")
