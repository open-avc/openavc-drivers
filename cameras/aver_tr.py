"""
OpenAVC AVer TR Series Auto Tracking Camera Driver (TR615, TR211 / TR315 / TR335).

AVer's TR-series AI auto tracking PTZ cameras expose two control surfaces,
both documented in each model's user manual:

  * VISCA over IP (UDP 52381 by default, the camera's "VISCA Port Number"
    setting; Sony's 8-byte header): power, pan / tilt / zoom / focus,
    presets, exposure and white balance, tally, freeze, auto tracking and
    the tracking options, and a full inquiry set (power, positions,
    exposure, white balance, tracking, tally, freeze, firmware, USB / UVC).
  * HTTP CGI (TCP 80, ``/cgi-bin?Set=...`` / ``?Get=...`` /
    ``?ActPreset=...``): tracking mode, preset save and recall, exposure
    value, saturation, contrast, flip, theme mode, USB transfer mode,
    preset speed and factory reset. This is not the PTZ310 / PTZ330's
    ``/storks`` CGI that ``aver_ptz`` speaks.

Covers the TR615 (TR615 User Manual 2026.05) and the TR211 / TR315 / TR315N
/ TR335 / TR335N ("AI Auto Tracking PTZ Camera User Manual", 2025.12): the
two CGI chapters match row for row and the VISCA tables match except where
the help says a command is one model's. AVer confirmed by email (2026-11)
that these manuals, not the 2022 TR3XX V2 CGI document, are the reference
for them. The TR535 / TR535N is a dual-lens camera with a different command
list and is not covered here.

Push vs poll
------------
Neither surface pushes. VISCA over IP and the CGI are request / response,
so the driver polls: VISCA inquiries for everything the camera reports
that way, the CGI for tracking mode (the only value the CGI reports that
VISCA does not).

Why Python
----------
Two transports at once (VISCA over UDP beside the HTTP CGI) from one
driver, with VISCA's binary framing and ACK / completion / error replies,
and a unique suffix the camera requires on every CGI request (it ignores a
request that repeats the previous one). The same shape as ``aver_ptz``.

Sources (archived as ``driver-roadmap/reference-docs/aver-tr*``)
---------------------------------------------------------------
* TR615 User Manual (2026.05, firmware 0.0.0000.36): VISCA pp. 60-65, CGI
  pp. 66-70.
* AI Auto Tracking PTZ Camera User Manual, TR211 / TR315 / TR315N / TR335 /
  TR335N (2025.12): VISCA pp. 59-65, CGI pp. 66-70.
* HTTP CGI Commands (Updated 6/2/2022), for the exposure value scale (1-9
  is -4..+4) and the random-suffix rule.
"""

from __future__ import annotations

import asyncio
import re
import struct
import time
from typing import Any

import httpx

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.utils.logger import get_logger

try:
    from openavc.drivers.base import CommandRefusedError
except ImportError:  # OpenAVC before 0.37.1
    CommandRefusedError = RuntimeError

log = get_logger(__name__)


# ── VISCA over IP framing (the manuals' "VISCA over IP Settings" page) ──

_VISCA_PORT = 52381

PAYLOAD_VISCA_COMMAND = 0x0100
PAYLOAD_VISCA_INQUIRY = 0x0110
PAYLOAD_VISCA_REPLY = 0x0111
PAYLOAD_CONTROL_CMD = 0x0200
PAYLOAD_CONTROL_REPLY = 0x0201


def _wrap(payload: bytes, payload_type: int, sequence: int) -> bytes:
    return struct.pack(">HHI", payload_type, len(payload), sequence) + payload


def _unwrap(packet: bytes) -> tuple[int, int, bytes] | None:
    if len(packet) < 9:
        return None
    payload_type, length, sequence = struct.unpack(">HHI", packet[:8])
    payload = packet[8:]
    if len(payload) != length:
        return None
    return payload_type, sequence, payload


def _nibbles(data: bytes) -> int:
    value = 0
    for b in data:
        value = (value << 4) | (b & 0x0F)
    return value


def _to_nibbles(value: int, count: int = 4) -> bytes:
    return bytes((value >> (4 * (count - 1 - i))) & 0x0F for i in range(count))


# Inquiries, in poll order: the state a reply feeds and the packet that asks.
# Power first: it is the one every model answers in every state, so a silent
# power inquiry means VISCA over IP is not reaching the camera at all.
_INQUIRIES: tuple[tuple[str, bytes], ...] = (
    ("power", b"\x81\x09\x04\x00\xff"),
    ("tracking", b"\x81\x09\x36\x69\x02\xff"),
    ("pan_tilt", b"\x81\x09\x06\x12\xff"),
    ("zoom_position", b"\x81\x09\x04\x47\xff"),
    ("focus_position", b"\x81\x09\x04\x48\xff"),
    ("focus_mode", b"\x81\x09\x04\x38\xff"),
    ("last_preset", b"\x81\x09\x04\x3f\xff"),
    ("tracking_body", b"\x81\x09\x36\x69\x03\xff"),
    ("tally", b"\x81\x09\x7e\x01\x0a\xff"),
    ("freeze", b"\x81\x09\x04\x62\x01\xff"),
    ("preset_freeze", b"\x81\x09\x04\x62\x02\xff"),
    ("exposure_mode", b"\x81\x09\x04\x39\xff"),
    ("shutter_position", b"\x81\x09\x04\x4a\xff"),
    ("iris_position", b"\x81\x09\x04\x4b\xff"),
    ("gain_position", b"\x81\x09\x04\x4c\xff"),
    ("bright_position", b"\x81\x09\x04\x4d\xff"),
    ("exposure_comp_position", b"\x81\x09\x04\x4e\xff"),
    ("backlight", b"\x81\x09\x04\x33\xff"),
    ("wdr", b"\x81\x09\x04\x3d\xff"),
    ("wb_mode", b"\x81\x09\x04\x35\xff"),
    ("red_gain", b"\x81\x09\x04\x43\xff"),
    ("blue_gain", b"\x81\x09\x04\x44\xff"),
    ("osd_menu", b"\x81\x09\x7e\x04\x76\x01\xff"),
    ("usb_connected", b"\x81\x09\x36\x69\x05\xff"),
    ("uvc_streaming", b"\x81\x09\x36\x69\x06\xff"),
)
_INQUIRY = dict(_INQUIRIES)
_INQUIRY["firmware_version"] = b"\x81\x09\x36\x69\x04\xff"

_EXPOSURE_MODES = {
    0x00: "full_auto", 0x03: "manual", 0x0A: "shutter_priority",
    0x0B: "iris_priority", 0x0D: "bright",
}
_EXPOSURE_MODE_CODES = {v: k for k, v in _EXPOSURE_MODES.items()}
_WB_MODES = {
    0x00: "auto", 0x01: "indoor", 0x02: "outdoor", 0x03: "one_push",
    0x04: "atw", 0x05: "manual",
}
_WB_MODE_CODES = {v: k for k, v in _WB_MODES.items()}
# 02 = on, 03 = off: power, focus mode and every on/off inquiry.
_ON_OFF = {0x02: True, 0x03: False}
_TALLY_CODES = {"red": 0x02, "off": 0x03, "amber": 0x04, "green": 0x05}
_AF_MODE_CODES = {"after_ptz": 0x00, "continuous": 0x01, "face_priority": 0x02}

# CGI tracking mode (trk_mode,3=N), from the Mode Get and Mode rows.
_TRACKING_MODES = {1: "presenter", 2: "zone", 3: "hybrid", 6: "framing"}
_TRACKING_MODE_CODES = {v: k for k, v in _TRACKING_MODES.items()}
_THEME_MODE_CODES = {
    "standard": 0, "zoom": 1, "teams": 2, "ndi": 3, "portrait": 4, "dante": 7,
}

# VISCA on/off commands (8x 01 04 <code> 02|03 FF): command -> code.
_ON_OFF_COMMANDS = {
    "set_backlight": 0x33,
    "set_wdr": 0x3D,
    "set_auto_zoom": 0xA0,
    "set_effective_tracking_area": 0xA1,
    "set_preset_affects_ptz_focus_only": 0xA5,
    "set_relative_zoom_ratio": 0xA6,
    "set_auto_tilt": 0xA7,
    "set_multi_presenter": 0xA9,
}

# Parameterless VISCA commands.
_SIMPLE_COMMANDS = {
    "power_on": b"\x81\x01\x04\x00\x02\xff",
    "power_off": b"\x81\x01\x04\x00\x03\xff",
    "pt_home": b"\x81\x01\x06\x04\xff",
    "pt_reset": b"\x81\x01\x06\x05\xff",
    "zoom_stop": b"\x81\x01\x04\x07\x00\xff",
    "focus_far": b"\x81\x01\x04\x08\x02\xff",
    "focus_near": b"\x81\x01\x04\x08\x03\xff",
    "focus_stop": b"\x81\x01\x04\x08\x00\xff",
    "focus_one_push": b"\x81\x01\x04\x18\x01\xff",
    "wb_one_push_trigger": b"\x81\x01\x04\x10\x05\xff",
    "red_gain_up": b"\x81\x01\x04\x03\x02\xff",
    "red_gain_down": b"\x81\x01\x04\x03\x03\xff",
    "blue_gain_up": b"\x81\x01\x04\x04\x02\xff",
    "blue_gain_down": b"\x81\x01\x04\x04\x03\xff",
    "shutter_up": b"\x81\x01\x04\x0a\x02\xff",
    "shutter_down": b"\x81\x01\x04\x0a\x03\xff",
    "iris_up": b"\x81\x01\x04\x0b\x02\xff",
    "iris_down": b"\x81\x01\x04\x0b\x03\xff",
    "gain_up": b"\x81\x01\x04\x0c\x02\xff",
    "gain_down": b"\x81\x01\x04\x0c\x03\xff",
    "bright_up": b"\x81\x01\x04\x0d\x02\xff",
    "bright_down": b"\x81\x01\x04\x0d\x03\xff",
    "exposure_comp_up": b"\x81\x01\x04\x0e\x02\xff",
    "exposure_comp_down": b"\x81\x01\x04\x0e\x03\xff",
    "tracking_on": b"\x81\x01\x04\x7d\x02\xff",
    "tracking_off": b"\x81\x01\x04\x7d\x03\xff",
    "osd_menu": b"\x81\x01\x06\x06\x10\xff",
    "osd_enter": b"\x81\x01\x7e\x01\x02\x00\x01\xff",
    "rtmp_start": b"\x81\x01\x04\xa2\x02\xff",
    "rtmp_stop": b"\x81\x01\x04\xa2\x03\xff",
    "reboot": b"\x81\x01\x04\xa4\xff",
}

# Pan/tilt drive directions (8x 01 06 01 VV WW <pan> <tilt> FF).
_PT_DIRECTIONS = {
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

# What each command changes that the camera reports, read again right after
# the camera accepts it. A move is read back too: the poll reads it again.
_READ_BACK = {
    "power_on": ("power",),
    "power_off": ("power",),
    "tracking_on": ("tracking",),
    "tracking_off": ("tracking",),
    "pt_stop": ("pan_tilt",),
    "pt_home": ("pan_tilt",),
    "pt_absolute": ("pan_tilt",),
    "zoom_stop": ("zoom_position",),
    "zoom_direct": ("zoom_position",),
    "focus_stop": ("focus_position",),
    "focus_auto": ("focus_mode",),
    "focus_manual": ("focus_mode",),
    "set_exposure_mode": ("exposure_mode",),
    "shutter_up": ("shutter_position",),
    "shutter_down": ("shutter_position",),
    "iris_up": ("iris_position",),
    "iris_down": ("iris_position",),
    "gain_up": ("gain_position",),
    "gain_down": ("gain_position",),
    "bright_up": ("bright_position",),
    "bright_down": ("bright_position",),
    "exposure_comp_up": ("exposure_comp_position",),
    "exposure_comp_down": ("exposure_comp_position",),
    "set_backlight": ("backlight",),
    "set_wdr": ("wdr",),
    "set_wb_mode": ("wb_mode",),
    "red_gain_up": ("red_gain",),
    "red_gain_down": ("red_gain",),
    "blue_gain_up": ("blue_gain",),
    "blue_gain_down": ("blue_gain",),
    "set_tally": ("tally",),
    "set_freeze": ("freeze",),
    "set_preset_freeze": ("preset_freeze",),
    "osd_menu": ("osd_menu",),
    "preset_recall": ("last_preset",),
    "preset_save": ("last_preset",),
}


class AVerTRDriver(BaseDriver):
    """AVer TR615 and TR211 / TR315 / TR335 (VISCA over IP + HTTP CGI)."""

    DRIVER_INFO = {
        "id": "aver_tr",
        "name": "AVer TR Auto Tracking Camera (TR615, TR335 / TR315 / TR211)",
        "manufacturer": "AVer",
        "category": "camera",
        "version": "1.0.0",
        # Binding the VISCA socket to Reach devices through (local_address)
        # needs 0.37.0; the contract check cannot see a method call.
        "min_platform_version": "0.37.0",
        "author": "OpenAVC",
        "description": (
            "AVer TR615 and TR211 / TR315 / TR335 AI auto tracking PTZ "
            "cameras. VISCA over IP (UDP 52381) for power, pan, tilt, zoom, "
            "focus, presets, exposure, white balance, tally, freeze and auto "
            "tracking, with every value the camera reports read back; the "
            "HTTP CGI for tracking mode, preset save and recall, saturation, "
            "contrast, flip, theme mode and factory reset."
        ),
        "source_url": (
            "https://www.averusa.com/pro-av/downloads/user-manual/"
            "TR615_UM_EN_2026.05.pdf"
        ),
        "tags": [
            "ptz", "camera", "aver", "visca", "visca-ip", "auto-tracking",
            "lecture-capture",
        ],
        "verified": False,
        "simulated": True,
        "protocols": ["visca-ip", "aver-tr-cgi"],
        "ports": [80, 52381],
        "transport": "http",
        "discovery": {
            # AVer Information's IEEE block, registered under its former name
            # AVerMedia Information. No probe: neither manual shows a reply
            # that names the camera (the Status get's reply is not given).
            "oui": ["00:18:1a"],
        },
        "compatible_models": [
            {
                "manufacturer": "AVer",
                "models": ["TR615"],
                "confidence": "untested",
                "notes": (
                    "Built from the TR615 User Manual (2026.05, firmware "
                    "0.0.0000.36), VISCA and CGI chapters. Autofocus Mode "
                    "is in this manual only."
                ),
            },
            {
                "manufacturer": "AVer",
                "models": ["TR335", "TR335N", "TR315", "TR315N", "TR211"],
                "confidence": "untested",
                "notes": (
                    "Built from the AI Auto Tracking PTZ Camera User Manual "
                    "for the TR211 / TR315 / TR315N / TR335 / TR335N "
                    "(2025.12), VISCA and CGI chapters. The Framing tracking "
                    "mode is set in this manual only."
                ),
            },
        ],
        "help": {
            "overview": (
                "AVer TR-series AI auto tracking PTZ cameras for lecture "
                "capture and presenter tracking. Power, pan, tilt, zoom, "
                "focus, presets, exposure, white balance, tally, freeze and "
                "auto tracking use VISCA over IP; tracking mode, preset "
                "save and recall, saturation, contrast, flip, theme mode and "
                "factory reset use the camera's web interface (HTTP)."
            ),
            "setup": (
                "1. Give the camera a fixed IP address.\n"
                "2. The driver uses two connections to the camera: HTTP on "
                "TCP 80 and VISCA over IP on UDP 52381. If you changed the "
                "VISCA Port Number on the camera, enter the same number "
                "here.\n"
                "3. Enter the camera's web interface username and password. "
                "The camera asks for them on tracking mode and factory "
                "reset; without them those are unavailable and the device "
                "page says so.\n"
                "4. If HTTPS Only is turned on in the camera, turn on HTTPS "
                "here and set the HTTP Port to 443."
            ),
        },
        "default_config": {
            "host": "",
            "port": 80,
            "ssl": False,
            "verify_ssl": False,
            "visca_port": 52381,
            "username": "",
            "password": "",
            "pan_speed": 12,
            "tilt_speed": 12,
            "zoom_speed": 4,
            "poll_interval": 5,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "default": 80, "label": "HTTP Port"},
            "ssl": {
                "type": "boolean",
                "default": False,
                "label": "HTTPS",
                "description": "Turn on when HTTPS Only is on in the camera.",
            },
            "verify_ssl": {
                "type": "boolean",
                "default": False,
                "label": "Verify HTTPS Certificate",
            },
            "visca_port": {
                "type": "integer",
                "default": 52381,
                "min": 1,
                "max": 65535,
                "label": "VISCA over IP Port (UDP)",
            },
            "username": {
                "type": "string",
                "default": "",
                "label": "Web Username",
            },
            "password": {
                "type": "string",
                "default": "",
                "label": "Web Password",
                "secret": True,
            },
            "pan_speed": {
                "type": "integer",
                "default": 12,
                "min": 1,
                "max": 24,
                "label": "Pan Speed (1-24)",
            },
            "tilt_speed": {
                "type": "integer",
                "default": 12,
                "min": 1,
                "max": 24,
                "label": "Tilt Speed (1-24)",
            },
            "zoom_speed": {
                "type": "integer",
                "default": 4,
                "min": 0,
                "max": 7,
                "label": "Zoom Speed (0-7)",
            },
            "poll_interval": {
                "type": "integer",
                "default": 5,
                "min": 0,
                "label": "Poll Interval (sec)",
            },
        },
        "state_variables": {
            "power": {
                "type": "enum",
                "values": ["on", "standby"],
                "label": "Power",
                "control": True,
            },
            "tracking": {"type": "boolean", "label": "Auto Tracking", "control": True},
            "tracking_mode": {
                "type": "enum",
                "values": ["presenter", "zone", "hybrid", "framing"],
                "label": "Tracking Mode",
                "control": True,
            },
            "tracking_body": {
                "type": "enum",
                "values": ["full_body", "upper_body"],
                "label": "Tracking Body Size",
            },
            "pan_position": {
                "type": "integer", "label": "Pan Position",
                "min": 0, "max": 65535, "cloud_priority": "low",
            },
            "tilt_position": {
                "type": "integer", "label": "Tilt Position",
                "min": 0, "max": 65535, "cloud_priority": "low",
            },
            "zoom_position": {
                "type": "integer", "label": "Zoom Position",
                "min": 0, "max": 65535, "cloud_priority": "low",
            },
            "focus_position": {
                "type": "integer", "label": "Focus Position",
                "min": 0, "max": 65535, "cloud_priority": "low",
            },
            "focus_mode": {
                "type": "enum", "values": ["auto", "manual"], "label": "Focus Mode",
            },
            "last_preset": {
                "type": "integer", "label": "Last Preset",
                "min": 0, "max": 255,
            },
            "exposure_mode": {
                "type": "enum",
                "values": [
                    "full_auto", "manual", "shutter_priority", "iris_priority",
                    "bright",
                ],
                "label": "Exposure Mode",
            },
            "shutter_position": {
                "type": "integer", "label": "Shutter Position", "min": 0, "max": 255,
            },
            "iris_position": {
                "type": "integer", "label": "Iris Position", "min": 0, "max": 255,
            },
            "gain_position": {
                "type": "integer", "label": "Gain Position", "min": 0, "max": 255,
            },
            "bright_position": {
                "type": "integer", "label": "Bright Position", "min": 0, "max": 255,
            },
            "exposure_comp_position": {
                "type": "integer", "label": "Exposure Compensation Position",
                "min": 0, "max": 255,
            },
            "backlight": {"type": "boolean", "label": "Back Light Compensation"},
            "wdr": {"type": "boolean", "label": "WDR"},
            "wb_mode": {
                "type": "enum",
                "values": ["auto", "indoor", "outdoor", "one_push", "atw", "manual"],
                "label": "White Balance Mode",
            },
            "red_gain": {"type": "integer", "label": "Red Gain", "min": 0, "max": 255},
            "blue_gain": {"type": "integer", "label": "Blue Gain", "min": 0, "max": 255},
            "tally": {"type": "boolean", "label": "Tally Lamp", "control": True},
            "freeze": {"type": "boolean", "label": "Freeze", "control": True},
            "preset_freeze": {"type": "boolean", "label": "Preset Freeze"},
            "osd_menu": {"type": "boolean", "label": "On-Screen Menu"},
            "usb_connected": {"type": "boolean", "label": "USB Cable Connected"},
            "uvc_streaming": {"type": "boolean", "label": "USB Video Streaming"},
            "firmware_version": {"type": "string", "label": "Firmware Version"},
            "preview_url": {"type": "string", "label": "Stream URL"},
            "preview_format": {"type": "string", "label": "Stream Format"},
            "last_error": {"type": "string", "label": "Last Error"},
        },
        # Values the camera keeps and reports back, each read again from the
        # camera after a write (and on every poll).
        "device_settings": {
            "exposure_mode": {
                "type": "enum",
                "label": "Exposure Mode",
                "help": "How the camera sets exposure.",
                "values": [
                    "full_auto", "manual", "shutter_priority", "iris_priority",
                    "bright",
                ],
                "state_key": "exposure_mode",
                "default": "full_auto",
                "setup": False,
            },
            "wb_mode": {
                "type": "enum",
                "label": "White Balance Mode",
                "help": (
                    "auto and atw follow the scene; indoor and outdoor are "
                    "fixed; one_push holds the last One Push White Balance; "
                    "manual uses the red and blue gains."
                ),
                "values": ["auto", "indoor", "outdoor", "one_push", "atw", "manual"],
                "state_key": "wb_mode",
                "default": "auto",
                "setup": False,
            },
            "focus_mode": {
                "type": "enum",
                "label": "Focus Mode",
                "help": "Autofocus, or focus held where it is set.",
                "values": ["auto", "manual"],
                "state_key": "focus_mode",
                "default": "auto",
                "setup": False,
            },
            "backlight": {
                "type": "boolean",
                "label": "Back Light Compensation",
                "help": "Brightens a presenter lit from behind.",
                "state_key": "backlight",
                "default": False,
                "setup": False,
            },
            "wdr": {
                "type": "boolean",
                "label": "WDR",
                "help": "Wide dynamic range, for a room with bright and dark areas.",
                "state_key": "wdr",
                "default": False,
                "setup": False,
            },
            "preset_freeze": {
                "type": "boolean",
                "label": "Preset Freeze",
                "help": "Holds the picture still while the camera moves to a preset.",
                "state_key": "preset_freeze",
                "default": False,
                "setup": False,
            },
            "tracking_mode": {
                "type": "enum",
                "label": "Tracking Mode",
                "help": (
                    "How auto tracking follows the presenter. Framing is "
                    "documented for the TR211 / TR315 / TR335."
                ),
                "values": ["presenter", "zone", "hybrid", "framing"],
                "state_key": "tracking_mode",
                "default": "presenter",
                "setup": False,
            },
        },
        "quick_actions": ["tracking_on", "tracking_off", "pt_home", "power_on", "power_off"],
        "commands": {
            # ── Power ──
            "power_on": {
                "label": "Power On",
                "params": {},
                "help": "Wakes the camera from standby.",
                "sets": {"power": "on"},
            },
            "power_off": {
                "label": "Power Off (Standby)",
                "params": {},
                "help": "Puts the camera in standby.",
                "sets": {"power": "standby"},
            },
            # ── Auto tracking ──
            "tracking_on": {
                "label": "Auto Tracking On",
                "params": {},
                "help": "The camera starts following the presenter.",
                "sets": {"tracking": True},
            },
            "tracking_off": {
                "label": "Auto Tracking Off",
                "params": {},
                "help": "The camera stops following the presenter and holds its view.",
                "sets": {"tracking": False},
            },
            "set_tracking_mode": {
                "label": "Set Tracking Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": ["presenter", "zone", "hybrid", "framing"],
                        "label": "Mode",
                    },
                },
                "help": (
                    "Changes how auto tracking follows the presenter. Needs "
                    "the web login. Framing is documented for the TR211 / "
                    "TR315 / TR335."
                ),
                "sets": {"tracking_mode": "{mode}"},
            },
            "set_auto_zoom": {
                "label": "Auto Zoom On/Off",
                "params": {"enabled": {"type": "boolean", "required": True, "label": "On"}},
                "help": "Turns on or off the camera zooming to keep the presenter's size while tracking.",
            },
            "set_auto_tilt": {
                "label": "Auto Tilt On/Off",
                "params": {"enabled": {"type": "boolean", "required": True, "label": "On"}},
                "help": "Turns on or off the camera tilting to follow the presenter while tracking.",
            },
            "set_effective_tracking_area": {
                "label": "Effective Tracking Area On/Off",
                "params": {"enabled": {"type": "boolean", "required": True, "label": "On"}},
                "help": "Turns on or off tracking only inside the area set in the camera.",
            },
            "set_multi_presenter": {
                "label": "Multi Presenter On/Off",
                "params": {"enabled": {"type": "boolean", "required": True, "label": "On"}},
                "help": "Turns on or off keeping several presenters in frame.",
            },
            "set_preset_affects_ptz_focus_only": {
                "label": "Preset Affects PTZ and Focus Only",
                "params": {"enabled": {"type": "boolean", "required": True, "label": "On"}},
                "help": "When on, recalling a preset moves the camera and sets focus but leaves the picture settings alone.",
            },
            "set_relative_zoom_ratio": {
                "label": "Relative Zoom Ratio On/Off",
                "params": {"enabled": {"type": "boolean", "required": True, "label": "On"}},
                "help": "Turns the camera's relative zoom ratio on or off.",
            },
            # ── Pan / tilt ──
            "pt_up": {"label": "Pan/Tilt Up", "params": {}, "help": "Tilts up until Pan/Tilt Stop."},
            "pt_down": {"label": "Pan/Tilt Down", "params": {}, "help": "Tilts down until Pan/Tilt Stop."},
            "pt_left": {"label": "Pan/Tilt Left", "params": {}, "help": "Pans left until Pan/Tilt Stop."},
            "pt_right": {"label": "Pan/Tilt Right", "params": {}, "help": "Pans right until Pan/Tilt Stop."},
            "pt_up_left": {"label": "Pan/Tilt Up-Left", "params": {}, "help": "Moves up and left until Pan/Tilt Stop."},
            "pt_up_right": {"label": "Pan/Tilt Up-Right", "params": {}, "help": "Moves up and right until Pan/Tilt Stop."},
            "pt_down_left": {"label": "Pan/Tilt Down-Left", "params": {}, "help": "Moves down and left until Pan/Tilt Stop."},
            "pt_down_right": {"label": "Pan/Tilt Down-Right", "params": {}, "help": "Moves down and right until Pan/Tilt Stop."},
            "pt_stop": {"label": "Pan/Tilt Stop", "params": {}, "help": "Stops pan and tilt."},
            "pt_home": {"label": "Pan/Tilt Home", "params": {}, "help": "Moves the camera to its home position."},
            "pt_reset": {
                "label": "Pan/Tilt Reset",
                "params": {},
                "help": "Recalibrates pan and tilt: the camera sweeps its range and returns.",
            },
            "pt_absolute": {
                "label": "Pan/Tilt Absolute",
                "params": {
                    "pan": {"type": "integer", "required": True, "min": 0, "max": 65535, "label": "Pan"},
                    "tilt": {"type": "integer", "required": True, "min": 0, "max": 65535, "label": "Tilt"},
                },
                "help": (
                    "Moves to a pan and tilt position in the camera's own "
                    "units, as Pan Position and Tilt Position report them."
                ),
                "sets": {"pan_position": "{pan}", "tilt_position": "{tilt}"},
            },
            # ── Zoom ──
            "zoom_tele": {
                "label": "Zoom In (Tele)",
                "params": {"speed": {"type": "integer", "min": 0, "max": 7, "label": "Speed"}},
                "help": "Zooms in until Zoom Stop. Speed 0-7; blank uses the Zoom Speed setting.",
            },
            "zoom_wide": {
                "label": "Zoom Out (Wide)",
                "params": {"speed": {"type": "integer", "min": 0, "max": 7, "label": "Speed"}},
                "help": "Zooms out until Zoom Stop. Speed 0-7; blank uses the Zoom Speed setting.",
            },
            "zoom_stop": {"label": "Zoom Stop", "params": {}, "help": "Stops zooming."},
            "zoom_direct": {
                "label": "Zoom Direct",
                "params": {
                    "position": {"type": "integer", "required": True, "min": 0, "max": 65535, "label": "Position"},
                },
                "help": "Zooms to a position in the camera's own units, as Zoom Position reports it.",
                "sets": {"zoom_position": "{position}"},
            },
            # ── Focus ──
            "focus_far": {"label": "Focus Far", "params": {}, "help": "Focuses farther until Focus Stop."},
            "focus_near": {"label": "Focus Near", "params": {}, "help": "Focuses nearer until Focus Stop."},
            "focus_stop": {"label": "Focus Stop", "params": {}, "help": "Stops focusing."},
            "focus_auto": {
                "label": "Focus Mode: Auto", "params": {},
                "help": "Turns on autofocus.", "sets": {"focus_mode": "auto"},
            },
            "focus_manual": {
                "label": "Focus Mode: Manual", "params": {},
                "help": "Holds focus where it is.", "sets": {"focus_mode": "manual"},
            },
            "focus_one_push": {
                "label": "Focus One Push", "params": {},
                "help": "Focuses once, then holds.",
            },
            "set_af_mode": {
                "label": "Set Autofocus Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": ["after_ptz", "continuous", "face_priority"],
                        "label": "Mode",
                    },
                },
                "help": (
                    "Autofocus after each move, continuously, or on faces "
                    "first. Documented for the TR615."
                ),
            },
            # ── Presets ──
            "preset_recall": {
                "label": "Preset Recall",
                "params": {"number": {"type": "integer", "required": True, "min": 0, "max": 255, "label": "Preset"}},
                "help": "Moves the camera to a saved preset (0-255).",
                "sets": {"last_preset": "{number}"},
            },
            "preset_save": {
                "label": "Preset Save",
                "params": {"number": {"type": "integer", "required": True, "min": 0, "max": 255, "label": "Preset"}},
                "help": "Saves the camera's current position as a preset (0-255).",
            },
            "preset_clear": {
                "label": "Preset Clear",
                "params": {"number": {"type": "integer", "required": True, "min": 0, "max": 255, "label": "Preset"}},
                "help": "Erases a saved preset.",
                "confirm": "Erases the position saved in this preset.",
            },
            "set_preset_speed": {
                "label": "Set Preset Speed",
                "params": {"value": {"type": "integer", "required": True, "min": 1, "max": 6, "label": "Speed"}},
                "help": "How fast the camera moves to a recalled preset, 1 (slowest) to 6.",
            },
            "set_preset_freeze": {
                "label": "Preset Freeze On/Off",
                "params": {"enabled": {"type": "boolean", "required": True, "label": "On"}},
                "help": "When on, the picture holds still while the camera moves to a preset.",
                "sets": {"preset_freeze": "{enabled}"},
            },
            # ── Exposure ──
            "set_exposure_mode": {
                "label": "Set Exposure Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": [
                            "full_auto", "manual", "shutter_priority",
                            "iris_priority", "bright",
                        ],
                        "label": "Mode",
                    },
                },
                "help": "Sets how the camera sets exposure.",
                "sets": {"exposure_mode": "{mode}"},
            },
            "shutter_up": {"label": "Shutter Up", "params": {}, "help": "One step faster shutter (manual or shutter priority)."},
            "shutter_down": {"label": "Shutter Down", "params": {}, "help": "One step slower shutter (manual or shutter priority)."},
            "iris_up": {"label": "Iris Up", "params": {}, "help": "Opens the iris one step (manual or iris priority)."},
            "iris_down": {"label": "Iris Down", "params": {}, "help": "Closes the iris one step (manual or iris priority)."},
            "gain_up": {"label": "Gain Up", "params": {}, "help": "One step more gain (manual exposure)."},
            "gain_down": {"label": "Gain Down", "params": {}, "help": "One step less gain (manual exposure)."},
            "bright_up": {"label": "Bright Up", "params": {}, "help": "One step brighter (bright mode)."},
            "bright_down": {"label": "Bright Down", "params": {}, "help": "One step darker (bright mode)."},
            "exposure_comp_up": {"label": "Exposure Compensation Up", "params": {}, "help": "Brightens the automatic exposure one step."},
            "exposure_comp_down": {"label": "Exposure Compensation Down", "params": {}, "help": "Darkens the automatic exposure one step."},
            "set_exposure_value": {
                "label": "Set Exposure Value",
                "params": {"value": {"type": "integer", "required": True, "min": -4, "max": 4, "label": "Value"}},
                "help": "Sets the exposure value, -4 (darker) to +4 (brighter).",
            },
            "set_backlight": {
                "label": "Back Light Compensation On/Off",
                "params": {"enabled": {"type": "boolean", "required": True, "label": "On"}},
                "help": "Brightens a presenter lit from behind.",
                "sets": {"backlight": "{enabled}"},
            },
            "set_wdr": {
                "label": "WDR On/Off",
                "params": {"enabled": {"type": "boolean", "required": True, "label": "On"}},
                "help": "Wide dynamic range, for a room with bright and dark areas.",
                "sets": {"wdr": "{enabled}"},
            },
            # ── White balance ──
            "set_wb_mode": {
                "label": "Set White Balance Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": ["auto", "indoor", "outdoor", "one_push", "atw", "manual"],
                        "label": "Mode",
                    },
                },
                "help": "Sets the white balance mode.",
                "sets": {"wb_mode": "{mode}"},
            },
            "wb_one_push_trigger": {
                "label": "One Push White Balance",
                "params": {},
                "help": "Measures white balance once (in One Push mode); point the camera at something white first.",
            },
            "red_gain_up": {"label": "Red Gain Up", "params": {}, "help": "More red (manual white balance)."},
            "red_gain_down": {"label": "Red Gain Down", "params": {}, "help": "Less red (manual white balance)."},
            "blue_gain_up": {"label": "Blue Gain Up", "params": {}, "help": "More blue (manual white balance)."},
            "blue_gain_down": {"label": "Blue Gain Down", "params": {}, "help": "Less blue (manual white balance)."},
            # ── Picture ──
            "set_saturation": {
                "label": "Set Saturation",
                "params": {"value": {"type": "integer", "required": True, "min": 0, "max": 10, "label": "Saturation"}},
                "help": "Colour saturation, 0 to 10.",
            },
            "set_contrast": {
                "label": "Set Contrast",
                "params": {"value": {"type": "integer", "required": True, "min": 0, "max": 4, "label": "Contrast"}},
                "help": "Picture contrast, 0 to 4.",
            },
            "set_flip": {
                "label": "Flip On/Off",
                "params": {"enabled": {"type": "boolean", "required": True, "label": "On"}},
                "help": "Turns the picture upside down, for a ceiling mount.",
            },
            "set_freeze": {
                "label": "Freeze On/Off",
                "params": {"enabled": {"type": "boolean", "required": True, "label": "On"}},
                "help": "Holds the picture still, or lets it run again.",
                "sets": {"freeze": "{enabled}"},
            },
            # ── Tally and menu ──
            "set_tally": {
                "label": "Set Tally Lamp",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": ["off", "red", "amber", "green"],
                        "label": "Lamp",
                    },
                },
                "help": "Lights the tally lamp red, amber or green, or turns it off.",
            },
            "osd_menu": {
                "label": "On-Screen Menu On/Off",
                "params": {},
                "help": "Shows or hides the camera's on-screen menu on the video output.",
            },
            "osd_enter": {
                "label": "On-Screen Menu Enter",
                "params": {},
                "help": "Enters the highlighted on-screen menu item.",
            },
            # ── Streaming and outputs ──
            "rtmp_start": {
                "label": "Start RTMP Stream",
                "params": {},
                "help": "Starts streaming to the RTMP server and stream key set in the camera.",
            },
            "rtmp_stop": {
                "label": "Stop RTMP Stream",
                "params": {},
                "help": "Stops the RTMP stream.",
            },
            "set_theme_mode": {
                "label": "Set Theme Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": ["standard", "zoom", "teams", "ndi", "portrait", "dante"],
                        "label": "Theme",
                    },
                },
                "help": (
                    "Sets the camera's theme mode (Standard, Zoom, Teams, NDI, "
                    "Portrait or Dante), which sets its video outputs for that "
                    "use. A model without that mode does not change."
                ),
            },
            "set_usb_transfer_mode": {
                "label": "Set USB Transfer Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": ["isochronous", "bulk"],
                        "label": "Mode",
                    },
                },
                "help": "Sets the USB video transfer mode.",
            },
            # ── System ──
            "reboot": {
                "label": "Reboot Camera",
                "params": {},
                "help": "Restarts the camera. It is offline until it has started again.",
            },
            "factory_reset": {
                "label": "Factory Reset",
                "params": {},
                "help": "Returns every setting to factory values. Needs the web login.",
                "confirm": (
                    "Returns the camera to its factory settings: DHCP "
                    "addressing and no web login, so it may come back at a "
                    "different address."
                ),
            },
        },
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._http: httpx.AsyncClient | None = None
        self._base_url = ""
        self._visca_transport: asyncio.DatagramTransport | None = None
        self._visca_target: tuple[str, int] | None = None
        self._sequence = 0
        self._visca_lock: asyncio.Lock | None = None
        self._visca_waiter: asyncio.Future[bytes] | None = None
        self._waiting_for: str = ""
        self._control_reply: asyncio.Future[None] | None = None
        self._request_id = 0
        # Why the CGI reads were skipped this connection, or "".
        self._cgi_unavailable = ""
        # The camera's refusal of this poll's CGI read, or "".
        self._cgi_refusal = ""

    # ── Connection lifecycle ──

    async def _pre_connect(self) -> None:
        if not self.config.get("host", ""):
            raise ConnectionError(f"[{self.device_id}] host is required")
        self._visca_lock = asyncio.Lock()
        self._sequence = 0
        self._cgi_unavailable = ""
        # The camera ignores a CGI request whose suffix repeats the last one
        # (2022 CGI document), so the suffix counts up from the clock, the way
        # the manuals' examples (_=1635216271678) do.
        self._request_id = int(time.time() * 1000)

    async def _create_transport(self, transport_type: str) -> None:
        """Driver-owned session: an httpx client for the CGI and a UDP socket
        for VISCA over IP. No platform transport."""
        host = self.config.get("host", "")
        port = int(self.config.get("port", 80))
        scheme = "https" if self.config.get("ssl") else "http"
        self._base_url = f"{scheme}://{host}:{port}"
        username = self.config.get("username", "") or ""
        password = self.config.get("password", "") or ""
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=5.0,
            auth=(username, password) if username else None,
            verify=bool(self.config.get("verify_ssl", False)),
            follow_redirects=False,
        )
        # Under simulation (and the catalog's connect test) the platform points
        # a driver's datagrams at the simulator with udp_redirect.
        redirect = getattr(self, "udp_redirect", None)
        target = redirect or (host, int(self.config.get("visca_port", _VISCA_PORT)))
        self._visca_target = target
        loop = asyncio.get_running_loop()
        # From the adapter Settings > Network > Reach devices through chose.
        ip = self.local_address(target[0])
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _ViscaProtocol(self._on_datagram),
            remote_addr=target,
            local_addr=(ip, 0) if ip else None,
        )
        self._visca_transport = transport

    async def _post_connect(self) -> None:
        # Sequence reset (VISCA over IP control command). Not every camera
        # answers it; the inquiry below is the real check.
        try:
            await self._control_reset()
        except asyncio.TimeoutError:
            pass
        visca_ok = await self._inquire(_INQUIRY["power"]) is not None
        try:
            http_ok = await self._read_tracking_mode()
        except ConnectionFaultError:
            raise
        except ConnectionError:
            http_ok = False
        if not visca_ok and not http_ok:
            raise ConnectionError(
                f"The camera at {self.config.get('host')} answered neither "
                f"VISCA over IP nor HTTP."
            )
        self.set_state("preview_url", f"rtsp://{self.config.get('host')}:554/live_st1")
        self.set_state("preview_format", "rtsp")

    async def _initial_sync(self) -> None:
        reply = await self._inquire(_INQUIRY["firmware_version"])
        if reply is not None:
            self._apply("firmware_version", reply)
        try:
            await self.poll()
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Initial poll failed")

    def _link_alive(self) -> bool:
        return self._http is not None

    async def _close_session(self) -> None:
        for fut in (self._visca_waiter, self._control_reply):
            if fut is not None and not fut.done():
                fut.cancel()
        self._visca_waiter = None
        self._control_reply = None
        if self._visca_transport is not None:
            self._visca_transport.close()
            self._visca_transport = None
        http, self._http = self._http, None
        if http is not None:
            await http.aclose()

    # ── VISCA over IP ──

    def _on_datagram(self, data: bytes) -> None:
        unwrapped = _unwrap(data)
        if unwrapped is None:
            return
        payload_type, sequence, payload = unwrapped
        if payload_type == PAYLOAD_CONTROL_REPLY:
            if self._control_reply is not None and not self._control_reply.done():
                self._control_reply.set_result(None)
            return
        if payload_type != PAYLOAD_VISCA_REPLY or len(payload) < 3 or payload[0] != 0x90:
            return
        waiter = self._visca_waiter
        if waiter is None or waiter.done():
            return
        kind = payload[1]
        if self._waiting_for == "inquiry":
            # An inquiry's answer is exactly 50 with data, or an error (6y).
            # A command's late completion (5y, y = socket) is not it.
            if kind == 0x50 or kind & 0xF0 == 0x60:
                waiter.set_result(payload)
        elif kind & 0xF0 in (0x40, 0x60) or (
            kind & 0xF0 == 0x50 and sequence == self._sequence
        ):
            # A command waits for its acknowledgement or the error that
            # refused it. A completion counts only when it carries this
            # command's sequence number: the previous command's completion
            # arrives after its acknowledgement, and taken as this command's
            # answer it would report this one done before the camera had it
            # (found in simulation, where the two arrive back to back).
            waiter.set_result(payload)

    def _send(self, payload: bytes, payload_type: int) -> None:
        if self._visca_transport is None:
            raise ConnectionError("VISCA over IP is not open")
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        self._visca_transport.sendto(_wrap(payload, payload_type, self._sequence))

    async def _control_reset(self) -> None:
        if self._visca_transport is None:
            return
        self._control_reply = asyncio.get_running_loop().create_future()
        try:
            self._visca_transport.sendto(_wrap(b"\x01", PAYLOAD_CONTROL_CMD, 0))
            await asyncio.wait_for(self._control_reply, timeout=1.0)
            self._sequence = 0
        finally:
            self._control_reply = None

    async def _exchange(self, payload: bytes, kind: str, timeout: float) -> bytes | None:
        """One VISCA message and its first reply, or None on silence."""
        if self._visca_lock is None or self._visca_transport is None:
            return None
        async with self._visca_lock:
            self._visca_waiter = asyncio.get_running_loop().create_future()
            self._waiting_for = kind
            try:
                self._send(
                    payload,
                    PAYLOAD_VISCA_INQUIRY if kind == "inquiry" else PAYLOAD_VISCA_COMMAND,
                )
                return await asyncio.wait_for(self._visca_waiter, timeout=timeout)
            except asyncio.TimeoutError:
                return None
            finally:
                self._visca_waiter = None

    async def _inquire(self, payload: bytes) -> bytes | None:
        """An inquiry's reply; b"" when the camera answered an error (it does
        not support it, or not in this state); None when nothing answered."""
        reply = await self._exchange(payload, "inquiry", 1.0)
        if reply is None:
            return None
        return b"" if reply[1] & 0xF0 == 0x60 else reply

    async def _visca_command(self, payload: bytes) -> None:
        reply = await self._exchange(payload, "command", 1.0)
        if reply is None:
            raise ConnectionError(
                "The camera did not answer the command over VISCA over IP."
            )
        if reply[1] & 0xF0 == 0x60:
            code = reply[2]
            if code == 0x02:
                raise CommandRefusedError("The camera does not support this command.")
            if code == 0x41:
                raise CommandRefusedError("The camera cannot do this in its current state.")
            raise CommandRefusedError(f"The camera refused the command (VISCA error {code:02X}).")

    def _apply(self, name: str, reply: bytes) -> None:
        """Write one inquiry's answer into state. An error reply writes nothing."""
        if not reply:
            return
        code = reply[2]
        if name == "power":
            if code in _ON_OFF:
                self.set_state("power", "on" if _ON_OFF[code] else "standby")
        elif name in ("tracking", "usb_connected", "uvc_streaming"):
            if code in (0x00, 0x01):
                self.set_state(name, code == 0x01)
        elif name in ("tally", "freeze", "preset_freeze", "osd_menu", "backlight", "wdr"):
            if code in _ON_OFF:
                self.set_state(name, _ON_OFF[code])
        elif name == "focus_mode":
            if code in _ON_OFF:
                self.set_state("focus_mode", "auto" if _ON_OFF[code] else "manual")
        elif name == "exposure_mode":
            if code in _EXPOSURE_MODES:
                self.set_state("exposure_mode", _EXPOSURE_MODES[code])
        elif name == "wb_mode":
            if code in _WB_MODES:
                self.set_state("wb_mode", _WB_MODES[code])
        elif name == "tracking_body":
            if code in (0x01, 0x02):
                self.set_state("tracking_body", "full_body" if code == 0x01 else "upper_body")
        elif name == "last_preset":
            if len(reply) == 4:
                self.set_state("last_preset", code)
        elif name == "pan_tilt":
            if len(reply) == 11:
                self.set_state("pan_position", _nibbles(reply[2:6]))
                self.set_state("tilt_position", _nibbles(reply[6:10]))
        elif name in ("zoom_position", "focus_position"):
            if len(reply) == 7:
                self.set_state(name, _nibbles(reply[2:6]))
        elif name in (
            "shutter_position", "iris_position", "gain_position", "bright_position",
            "exposure_comp_position", "red_gain", "blue_gain",
        ):
            # y0 50 00 00 0p 0q FF
            if len(reply) == 7:
                self.set_state(name, _nibbles(reply[4:6]))
        elif name == "firmware_version":
            # y0 50 0p 0q 0r 0s 0t 0u 0v 0w FF -> p.q.rstu.vw
            if len(reply) == 11:
                d = [f"{b & 0x0F:X}" for b in reply[2:10]]
                self.set_state(
                    "firmware_version",
                    f"{d[0]}.{d[1]}.{''.join(d[2:6])}.{''.join(d[6:8])}",
                )

    async def _read_back(self, names: tuple[str, ...]) -> None:
        for name in names:
            reply = await self._inquire(_INQUIRY[name])
            if reply is None:
                return
            self._apply(name, reply)

    # ── HTTP CGI ──

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _cgi(self, query: str) -> str:
        """GET /cgi-bin?<query>. Returns the body of a 200; raises otherwise."""
        if self._http is None:
            raise ConnectionError("Not connected")
        try:
            resp = await self._http.get(f"/cgi-bin?{query}")
        except httpx.TransportError as exc:
            raise ConnectionError(
                f"The camera at {self._base_url} is not answering HTTP: {exc}"
            ) from exc
        if resp.status_code == 401:
            if self.config.get("username"):
                raise ConnectionFaultError(
                    "The camera refused the web username and password.",
                    code="auth_failed",
                )
            raise _LoginRequired()
        if resp.status_code != 200:
            raise CommandRefusedError(
                f"The camera answered HTTP {resp.status_code} to that request."
            )
        return resp.text

    async def _cgi_set(self, setting: str) -> None:
        # Set=<key>,3,<value>&(<id>): the 2022 document's example form.
        await self._cgi(f"Set={setting}&({self._next_id()})")

    async def _read_tracking_mode(self) -> bool:
        """Read trk_mode over the CGI. True when the camera answered it.

        A camera with a web login answers 401 here when none is entered: that
        is a missing login, not a dead camera, so the CGI reads stop for this
        connection and Last Error says why; VISCA keeps working."""
        self._cgi_refusal = ""
        if self._cgi_unavailable:
            return True
        try:
            text = await self._cgi(f"Get=trk_mode,3&_={self._next_id()}")
        except _LoginRequired:
            self._cgi_unavailable = (
                "The camera asks for its web login, which is not entered, so "
                "Tracking Mode is not read and Factory Reset is refused."
            )
            return True
        except CommandRefusedError as exc:
            # The camera answered (a 403, say, for an account that may not
            # read it): it is up, so this is reported, not counted as silence.
            self._cgi_refusal = f"Tracking mode could not be read: {exc}"
            return True
        match = re.search(r"trk_mode,3=(\d+)", text)
        if match:
            mode = _TRACKING_MODES.get(int(match.group(1)))
            if mode:
                self.set_state("tracking_mode", mode)
        return True

    # ── Commands ──

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if self._http is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        try:
            await self._dispatch(command, params)
        except _LoginRequired:
            raise CommandRefusedError(
                "The camera asks for its web login for this command, and none is entered."
            ) from None
        names = _READ_BACK.get(command)
        if names:
            await self._read_back(names)

    async def _dispatch(self, command: str, params: dict[str, Any]) -> None:
        if command in _SIMPLE_COMMANDS:
            await self._visca_command(_SIMPLE_COMMANDS[command])
            return
        if command in _PT_DIRECTIONS:
            pan_dir, tilt_dir = _PT_DIRECTIONS[command]
            await self._visca_command(
                b"\x81\x01\x06\x01"
                + bytes([self._speed("pan_speed"), self._speed("tilt_speed"), pan_dir, tilt_dir])
                + b"\xff"
            )
            return
        if command in _ON_OFF_COMMANDS:
            code = _ON_OFF_COMMANDS[command]
            await self._visca_command(
                bytes([0x81, 0x01, 0x04, code, 0x02 if params["enabled"] else 0x03, 0xFF])
            )
            return
        match command:
            case "pt_absolute":
                await self._visca_command(
                    b"\x81\x01\x06\x02"
                    + bytes([self._speed("pan_speed"), self._speed("tilt_speed")])
                    + _to_nibbles(int(params["pan"]))
                    + _to_nibbles(int(params["tilt"]))
                    + b"\xff"
                )
            case "zoom_tele" | "zoom_wide":
                speed = params.get("speed")
                if speed is None or speed == "":
                    speed = self.config.get("zoom_speed", 4)
                speed = max(0, min(7, int(speed)))
                base = 0x20 if command == "zoom_tele" else 0x30
                await self._visca_command(bytes([0x81, 0x01, 0x04, 0x07, base | speed, 0xFF]))
            case "zoom_direct":
                await self._visca_command(
                    b"\x81\x01\x04\x47" + _to_nibbles(int(params["position"])) + b"\xff"
                )
            case "focus_auto":
                await self._visca_command(b"\x81\x01\x04\x38\x02\xff")
            case "focus_manual":
                await self._visca_command(b"\x81\x01\x04\x38\x03\xff")
            case "set_af_mode":
                await self._visca_command(
                    bytes([0x81, 0x01, 0x04, 0x57, _AF_MODE_CODES[str(params["mode"])], 0xFF])
                )
            case "preset_clear":
                await self._visca_command(
                    bytes([0x81, 0x01, 0x04, 0x3F, 0x00, int(params["number"]), 0xFF])
                )
            case "preset_recall":
                # The CGI, not VISCA 3F 02: the camera can be set to give some
                # VISCA preset numbers special meanings (VISCA Customized
                # Functions); the CGI's are plain presets.
                await self._cgi(f"ActPreset=0,{int(params['number'])}&({self._next_id()})")
            case "preset_save":
                await self._cgi(f"ActPreset=1,{int(params['number'])}&({self._next_id()})")
            case "set_preset_speed":
                await self._cgi_set(f"preset_speed,3,{int(params['value'])}")
            case "set_preset_freeze":
                await self._visca_command(
                    b"\x81\x01\x04\x62" + (b"\x22" if params["enabled"] else b"\x23") + b"\xff"
                )
            case "set_freeze":
                await self._visca_command(
                    b"\x81\x01\x04\x62" + (b"\x02" if params["enabled"] else b"\x03") + b"\xff"
                )
            case "set_exposure_mode":
                await self._visca_command(
                    bytes([0x81, 0x01, 0x04, 0x39, _EXPOSURE_MODE_CODES[str(params["mode"])], 0xFF])
                )
            case "set_exposure_value":
                # The CGI's 1-9 is the camera's -4..+4 (2022 CGI document).
                await self._cgi_set(f"img_expo_expo,3,{int(params['value']) + 5}")
            case "set_wb_mode":
                await self._visca_command(
                    bytes([0x81, 0x01, 0x04, 0x35, _WB_MODE_CODES[str(params["mode"])], 0xFF])
                )
            case "set_saturation":
                await self._cgi_set(f"img_saturation,3,{int(params['value'])}")
            case "set_contrast":
                await self._cgi_set(f"img_contrast,3,{int(params['value'])}")
            case "set_flip":
                await self._cgi_set(f"img_flip,3,{1 if params['enabled'] else 0}")
            case "set_tally":
                await self._visca_command(
                    bytes([0x81, 0x01, 0x7E, 0x01, 0x0A, 0x00, _TALLY_CODES[str(params["mode"])], 0xFF])
                )
            case "set_tracking_mode":
                code = _TRACKING_MODE_CODES[str(params["mode"])]
                # trk_mode,3,N&X: the Mode rows' own form.
                await self._cgi(f"Set=trk_mode,3,{code}&{self._next_id()}")
                await self._read_tracking_mode()
            case "set_theme_mode":
                await self._cgi_set(f"sys_theme_mode,3,{_THEME_MODE_CODES[str(params['mode'])]}")
            case "set_usb_transfer_mode":
                await self._cgi_set(
                    f"usb_bulk_mode_en,3,{1 if params['mode'] == 'bulk' else 0}"
                )
            case "factory_reset":
                await self._cgi(f"OnePush=d&_={self._next_id()}")
            case _:
                raise ValueError(f"Unknown command: {command}")

    # ── Device settings ──

    _SETTING_COMMANDS = {
        "exposure_mode": ("set_exposure_mode", "mode"),
        "wb_mode": ("set_wb_mode", "mode"),
        "backlight": ("set_backlight", "enabled"),
        "wdr": ("set_wdr", "enabled"),
        "preset_freeze": ("set_preset_freeze", "enabled"),
        "tracking_mode": ("set_tracking_mode", "mode"),
    }

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if key == "focus_mode":
            await self.send_command("focus_auto" if str(value) == "auto" else "focus_manual")
            return
        spec = self._SETTING_COMMANDS.get(key)
        if spec is None:
            raise ValueError(f"Unknown device setting: {key}")
        command, param = spec
        if self.DRIVER_INFO["device_settings"][key]["type"] == "boolean":
            value = value if isinstance(value, bool) else str(value).strip().lower() in (
                "1", "true", "on", "yes",
            )
        await self.send_command(command, {param: value})

    # ── Polling ──

    async def poll(self) -> None:
        if self._http is None:
            return
        problems: list[str] = []
        # The CGI first: a transport error here raises, which is what takes a
        # camera that stopped answering offline.
        await self._read_tracking_mode()
        if self._cgi_unavailable:
            problems.append(self._cgi_unavailable)
        if self._cgi_refusal:
            problems.append(self._cgi_refusal)
        # VISCA: stop at the first inquiry nothing answers. The camera answers
        # one it does not support with an error, so silence means VISCA over
        # IP is not reaching it, and waiting out the rest would only make the
        # poll run long.
        for name, payload in _INQUIRIES:
            reply = await self._inquire(payload)
            if reply is None:
                target = self._visca_target or ("", 0)
                problems.append(
                    f"The camera did not answer VISCA over IP on UDP port {target[1]}."
                )
                break
            self._apply(name, reply)
        # A standing condition: written again on every poll while it holds,
        # since the platform clears Last Error after a clean poll.
        if problems:
            self.set_state("last_error", " ".join(problems))

    def _speed(self, key: str) -> int:
        try:
            value = int(self.config.get(key, 12))
        except (TypeError, ValueError):
            value = 12
        return max(1, min(24, value))


class _LoginRequired(Exception):
    """The camera answered 401 and no web login is entered."""


class _ViscaProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_data) -> None:
        self._on_data = on_data

    def datagram_received(self, data, addr) -> None:
        try:
            self._on_data(data)
        except Exception:
            log.exception("VISCA datagram handler error")
