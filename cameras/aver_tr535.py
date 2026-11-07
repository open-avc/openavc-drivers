"""
OpenAVC AVer TR535 / TR535N Dual-Lens Auto Tracking Camera Driver.

The TR535 and TR535N pair a PTZ camera with a fixed wide-angle lens (110
degrees) that the tracking modes use to find the presenter. Two control
surfaces, both documented in the TR535 / TR535N User Manual:

  * VISCA over IP (UDP 52381 by default, the camera's "Visca Port Number"
    setting; Sony's 8-byte header): power, pan / tilt / zoom / focus, preset
    clear, exposure and white balance, tally, freeze, auto tracking and the
    tracking options, theme mode, the HDMI 1 and HDMI 2 sources (PTZ, wide
    angle or PIP / PBP) and the PIP / PBP layout, and an inquiry for nearly
    every one of them.
  * HTTP CGI (TCP 80, ``/cgi-bin?Set=...`` / ``?Get=...`` / ``?ActPreset=``):
    preset save and recall, exposure value, saturation, contrast, factory
    reset, and the RTMP stream: server URL and stream key saved and read
    back, and the stream's status.

Not the same camera as the TR615 and TR211 / TR315 / TR335 (``aver_tr``):
the theme codes differ (A3 00 IP to 05 NDI), the tally codes for green and
amber are the other way round, the tracking modes include Segment and AI
Patrol, the HDMI outputs choose a lens, and the CGI list is a different
one (no tracking mode, theme, flip or USB rows; RTMP rows instead).

Push vs poll
------------
Neither surface pushes. VISCA over IP and the CGI are request / response,
so the driver polls: the VISCA inquiries for everything the camera reports
that way, the CGI for the RTMP stream's status and server URL (the values
VISCA does not report).

Why Python
----------
Two transports at once (VISCA over UDP beside the HTTP CGI) from one
driver, with VISCA's binary framing and ACK / completion / error replies,
and a unique suffix the camera requires on the CGI rows that print one.
The same shape as ``aver_tr`` and ``aver_ptz``.

The request suffix
------------------
The TR535's CGI table prints ``&(random)`` on its movement, preset and
picture rows and ``&_=X`` on reboot, factory reset and the tracking read,
and prints nothing on the RTMP rows. The driver sends each row in the form
printed: firmware 0.0.0000.43 and .46 note "enhanced CGI input handling",
so a parameter the table does not show could be refused, and on the RTMP
save rows anything after the ``=`` is the value being saved. (``aver_tr``
adds a suffix to every row, from AVer's 2022 TR3XX CGI document; that
document does not cover this camera.)

Setting the tracking mode
-------------------------
The CGI has no tracking mode row, and VISCA has no plain command for it:
the manual's only way is a "CAM_Memory Special" preset number (Set A4
Presenter, A5 Zone, A6 Hybrid, AF Segment, A7 the next mode; A0 / A1 the
tracking body size), which the camera's VISCA Customized Function setting
can turn back into a normal preset. So these are commands, read back
through the mode inquiry, and refused when the mode did not change; never
device settings, which the platform writes again on every reconnect.

Sources (archived as ``driver-roadmap/reference-docs/aver-tr535-*``)
--------------------------------------------------------------------
* TR535 / TR535N User Manual (2026.05, firmware 0.0.0000.33): VISCA pp.
  67-73, CGI Commands pp. 74-75.
* TR535 / TR535N firmware release notes to 0.0.0000.46.
"""

from __future__ import annotations

import asyncio
import re
import struct
import time
from typing import Any
from urllib.parse import quote

import httpx

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.utils.logger import get_logger

try:
    from openavc.drivers.base import CommandRefusedError
except ImportError:  # OpenAVC before 0.37.1
    CommandRefusedError = RuntimeError

log = get_logger(__name__)


# ── VISCA over IP framing (the manual's "VISCA over IP Settings" page) ──

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
# Power first: it is the one the camera answers in every state, so a silent
# power inquiry means VISCA over IP is not reaching the camera at all.
_INQUIRIES: tuple[tuple[str, bytes], ...] = (
    ("power", b"\x81\x09\x04\x00\xff"),
    ("tracking", b"\x81\x09\x36\x69\x02\xff"),
    ("tracking_mode", b"\x81\x09\x36\x69\x01\xff"),
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
    ("hdmi1_source", b"\x81\x09\x36\x69\x07\x01\xff"),
    ("hdmi2_source", b"\x81\x09\x36\x69\x07\x02\xff"),
    ("pip_pbp_mode", b"\x81\x09\x36\x69\x08\xff"),
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
# Tally Lamp: 04 is green and 05 amber on this camera (the TR615's are the
# other way round).
_TALLY_CODES = {"red": 0x02, "off": 0x03, "green": 0x04, "amber": 0x05}
# sys_theme_mode, VISCA 8x 01 04 A3 0p FF.
_THEME_MODE_CODES = {"ip": 0, "hdmi": 1, "usb": 2, "zoom": 3, "teams": 4, "ndi": 5}
# CAM_Tracking_mode inquiry (8x 09 36 69 01 FF). A mode the table does not
# number (Framing, from firmware 0.0.0000.46) reads as "other".
_TRACKING_MODES = {1: "presenter", 2: "zone", 3: "hybrid", 4: "segment", 5: "ai_patrol"}
# CAM_Memory Special: the preset number whose Set selects the mode.
_TRACKING_SPECIALS = {"presenter": 0xA4, "zone": 0xA5, "hybrid": 0xA6, "segment": 0xAF}
_BODY_SPECIALS = {"full_body": 0xA0, "upper_body": 0xA1}
_NEXT_MODE_SPECIAL = 0xA7
# HDMI 1 / HDMI 2 source (8x 01 36 69 07 0n ss FF and its inquiry).
_HDMI_SOURCES = {0: "ptz", 1: "wide_angle", 2: "pip_pbp"}
_HDMI_SOURCE_CODES = {v: k for k, v in _HDMI_SOURCES.items()}
# Inquiry for RTMP status: 2 streaming, 0 stopped.
_RTMP_STATUS = {0: "stopped", 2: "streaming"}

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
    "focus_auto": b"\x81\x01\x04\x38\x02\xff",
    "focus_manual": b"\x81\x01\x04\x38\x03\xff",
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

# What each command changes that VISCA reports, read again right after the
# camera accepts it. A move is read back too: the poll reads it again.
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
    "set_hdmi1_source": ("hdmi1_source",),
    "set_hdmi2_source": ("hdmi2_source",),
    "set_pip_pbp_mode": ("pip_pbp_mode",),
}

# How long a CAM_Memory Special is given to show in the mode inquiry.
_SPECIAL_CONFIRM_TRIES = 4
_SPECIAL_CONFIRM_GAP_S = 0.5

# Polls between reads of the RTMP server URL (it changes only when someone
# saves another; a save through the driver reads it at once).
_RTMP_URL_EVERY = 12

# The poll's CGI reads wait less than a command does, so a poll with the web
# interface silent (and VISCA answering) still fits the 5 s poll interval.
_POLL_HTTP_TIMEOUT_S = 3.0


class AVerTR535Driver(BaseDriver):
    """AVer TR535 / TR535N (VISCA over IP + HTTP CGI)."""

    DRIVER_INFO = {
        "id": "aver_tr535",
        "name": "AVer TR535 / TR535N Dual-Lens Auto Tracking Camera",
        "manufacturer": "AVer",
        "category": "camera",
        "version": "1.0.0",
        # Binding the VISCA socket to Reach devices through (local_address)
        # needs 0.37.0; the contract check cannot see a method call.
        "min_platform_version": "0.37.0",
        "author": "OpenAVC",
        "description": (
            "AVer TR535 and TR535N dual-lens AI auto tracking cameras. VISCA "
            "over IP (UDP 52381) for power, pan, tilt, zoom, focus, exposure, "
            "white balance, tally, freeze, auto tracking and tracking mode, "
            "theme mode, the HDMI 1 and HDMI 2 sources and the PIP / PBP "
            "layout, with every value the camera reports read back; the HTTP "
            "CGI for preset save and recall, saturation, contrast, factory "
            "reset and the RTMP stream's server, key and status. Both lenses' "
            "streams are offered for preview."
        ),
        "source_url": (
            "https://www.averusa.com/pro-av/downloads/user-manual/"
            "TR535%2C%20TR535N_UM_EN_2026.05.pdf"
        ),
        "tags": [
            "ptz", "camera", "aver", "visca", "visca-ip", "auto-tracking",
            "lecture-capture", "dual-lens",
        ],
        "verified": False,
        "simulated": True,
        "protocols": ["visca-ip", "aver-tr-cgi"],
        "ports": [80, 52381],
        "transport": "http",
        "discovery": {
            # AVer Information's IEEE block, registered under its former name
            # AVerMedia Information. No probe: the manual shows no reply that
            # names the camera.
            "oui": ["00:18:1a"],
        },
        "compatible_models": [
            {
                "manufacturer": "AVer",
                "models": ["TR535", "TR535N"],
                "confidence": "untested",
                "notes": (
                    "Built from the TR535 / TR535N User Manual (2026.05, "
                    "firmware 0.0.0000.33), VISCA and CGI chapters, and the "
                    "firmware notes to 0.0.0000.46."
                ),
            },
        ],
        "help": {
            "overview": (
                "AVer TR535 / TR535N dual-lens auto tracking cameras for "
                "lecture capture and presenter tracking. Power, pan, tilt, "
                "zoom, focus, exposure, white balance, tally, freeze, auto "
                "tracking, theme mode and the HDMI outputs use VISCA over IP; "
                "preset save and recall, saturation, contrast, factory reset "
                "and the RTMP stream use the camera's web interface (HTTP)."
            ),
            "setup": (
                "1. Give the camera a fixed IP address.\n"
                "2. The driver uses two connections to the camera: HTTP on "
                "TCP 80 and VISCA over IP on UDP 52381. If you changed the "
                "Visca Port Number on the camera (Network page), enter the "
                "same number here.\n"
                "3. If the camera has a web login, enter its username and "
                "password. Without them, the commands the camera asks the "
                "login for are refused and the device page says so.\n"
                "4. If the camera answers only HTTPS, turn on HTTPS here and "
                "set the HTTP Port to 443.\n"
                "5. Set Tracking Mode and Set Tracking Body Size use the "
                "camera's VISCA special preset numbers. Leave System > VISCA "
                "Customized Function at its factory setting for them to work."
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
                "description": "Turn on when the camera answers only HTTPS.",
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
                "values": ["presenter", "zone", "hybrid", "segment", "ai_patrol", "other"],
                "label": "Tracking Mode",
                "help": (
                    "Other is a mode the manual's VISCA table gives no number "
                    "for, such as Framing (firmware 0.0.0000.46)."
                ),
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
            "hdmi1_source": {
                "type": "enum",
                "values": ["ptz", "wide_angle", "pip_pbp"],
                "label": "HDMI 1 Source",
                "control": True,
            },
            "hdmi2_source": {
                "type": "enum",
                "values": ["ptz", "wide_angle", "pip_pbp"],
                "label": "HDMI 2 Source",
                "control": True,
            },
            "pip_pbp_mode": {
                "type": "integer",
                "label": "PIP / PBP Layout",
                "min": 1,
                "max": 8,
                "help": (
                    "The layout an output set to PIP / PBP shows, 1 to 8. The "
                    "camera's menu lists PBP-1, PBP-2 and PIP-1 to PIP-6; the "
                    "manual does not say which number is which."
                ),
            },
            "rtmp_status": {
                "type": "enum",
                "values": ["stopped", "streaming", "other"],
                "label": "RTMP Stream",
                "help": "Other is a status number the manual does not list.",
                "control": True,
            },
            "rtmp_url": {"type": "string", "label": "RTMP Server URL"},
            "firmware_version": {"type": "string", "label": "Firmware Version"},
            "preview_url": {"type": "string", "label": "Stream URL"},
            "preview_format": {"type": "string", "label": "Stream Format"},
            "mjpeg_url": {"type": "string", "label": "MJPEG URL (1280x720)"},
            "last_error": {"type": "string", "label": "Last Error"},
        },
        # The wide-angle lens, so its stream is a preview source of its own
        # beside the PTZ camera's (the device's own preview_url).
        "child_entity_types": {
            "lens": {
                "label": "Lens",
                "label_plural": "Lenses",
                "id_format": {"type": "string"},
                "label_field": "name",
                "summary_fields": ["name", "preview_url"],
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "preview_url": {"type": "string", "label": "Stream URL"},
                    "preview_format": {"type": "string", "label": "Stream Format"},
                },
            },
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
            "hdmi1_source": {
                "type": "enum",
                "label": "HDMI 1 Source",
                "help": "What HDMI 1 shows: the PTZ camera, the wide-angle lens, or both as PIP / PBP.",
                "values": ["ptz", "wide_angle", "pip_pbp"],
                "state_key": "hdmi1_source",
                "default": "ptz",
                "setup": False,
            },
            "hdmi2_source": {
                "type": "enum",
                "label": "HDMI 2 Source",
                "help": "What HDMI 2 shows: the PTZ camera, the wide-angle lens, or both as PIP / PBP.",
                "values": ["ptz", "wide_angle", "pip_pbp"],
                "state_key": "hdmi2_source",
                "default": "ptz",
                "setup": False,
            },
            "pip_pbp_mode": {
                "type": "integer",
                "label": "PIP / PBP Layout",
                "help": (
                    "The layout an output set to PIP / PBP shows, 1 to 8. The "
                    "camera's menu lists PBP-1, PBP-2 and PIP-1 to PIP-6; the "
                    "manual does not say which number is which."
                ),
                "min": 1,
                "max": 8,
                "state_key": "pip_pbp_mode",
                "default": 1,
                "setup": False,
            },
            "rtmp_url": {
                "type": "string",
                "label": "RTMP Server URL",
                "help": (
                    "Where the camera streams when RTMP is started, from the "
                    "video platform (for example rtmp://a.rtmp.youtube.com/live2). "
                    "The stream key is set with Save RTMP Stream Key."
                ),
                "state_key": "rtmp_url",
                "default": "",
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
                "help": (
                    "Puts the camera in standby. The camera's Power Mode "
                    "(System page) decides what wakes it: at the factory "
                    "setting Power On does; in Full Shutdown Power On does, and "
                    "the web interface is off until then; in ERP only the "
                    "remote control does."
                ),
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
                        "values": ["presenter", "zone", "hybrid", "segment"],
                        "label": "Mode",
                    },
                },
                "help": (
                    "Changes how auto tracking follows the presenter. Uses the "
                    "camera's VISCA special preset numbers (A4 Presenter, A5 "
                    "Zone, A6 Hybrid, AF Segment), so System > VISCA "
                    "Customized Function must be at its factory setting. AI "
                    "Patrol is set on the camera's web page."
                ),
                "sets": {"tracking_mode": "{mode}"},
            },
            "next_tracking_mode": {
                "label": "Next Tracking Mode",
                "params": {},
                "help": (
                    "Switches auto tracking to the camera's next tracking "
                    "mode (VISCA special preset A7)."
                ),
            },
            "set_tracking_body": {
                "label": "Set Tracking Body Size",
                "params": {
                    "size": {
                        "type": "enum",
                        "required": True,
                        "values": ["full_body", "upper_body"],
                        "label": "Size",
                    },
                },
                "help": (
                    "Frames the presenter's full body or upper body while "
                    "tracking (VISCA special presets A0 and A1)."
                ),
                "sets": {"tracking_body": "{size}"},
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
                "help": "Turns on or off the camera moving to its Multi-Presenter preset when it sees several presenters.",
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
                # 255 would put FF, the byte that ends a VISCA packet, inside
                # 8x 01 04 3F 00 pp FF.
                "params": {"number": {"type": "integer", "required": True, "min": 0, "max": 254, "label": "Preset"}},
                "help": "Erases a saved preset (0-254).",
                "confirm": "Erases the position saved in this preset.",
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
                "params": {"value": {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Value"}},
                "help": "Fine-tunes overall brightness, 1 to 9 as the camera numbers it (5 is the middle).",
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
            "set_freeze": {
                "label": "Freeze On/Off",
                "params": {"enabled": {"type": "boolean", "required": True, "label": "On"}},
                "help": "Holds the picture still, or lets it run again.",
                "sets": {"freeze": "{enabled}"},
            },
            # ── Outputs ──
            "set_hdmi1_source": {
                "label": "Set HDMI 1 Source",
                "params": {
                    "source": {
                        "type": "enum",
                        "required": True,
                        "values": ["ptz", "wide_angle", "pip_pbp"],
                        "label": "Source",
                    },
                },
                "help": "Shows the PTZ camera, the wide-angle lens, or both (PIP / PBP) on HDMI 1.",
                "sets": {"hdmi1_source": "{source}"},
            },
            "set_hdmi2_source": {
                "label": "Set HDMI 2 Source",
                "params": {
                    "source": {
                        "type": "enum",
                        "required": True,
                        "values": ["ptz", "wide_angle", "pip_pbp"],
                        "label": "Source",
                    },
                },
                "help": "Shows the PTZ camera, the wide-angle lens, or both (PIP / PBP) on HDMI 2.",
                "sets": {"hdmi2_source": "{source}"},
            },
            "set_pip_pbp_mode": {
                "label": "Set PIP / PBP Layout",
                "params": {
                    "mode": {"type": "integer", "required": True, "min": 1, "max": 8, "label": "Layout"},
                },
                "help": (
                    "Changes the layout an output set to PIP / PBP shows, 1 to "
                    "8. The camera's menu lists PBP-1, PBP-2 and PIP-1 to "
                    "PIP-6; the manual does not say which number is which."
                ),
                "sets": {"pip_pbp_mode": "{mode}"},
            },
            "set_theme_mode": {
                "label": "Set Theme Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": ["ip", "hdmi", "usb", "zoom", "teams", "ndi"],
                        "label": "Theme",
                    },
                },
                "help": (
                    "Sets the camera's theme mode (IP, HDMI, USB, Zoom, Teams "
                    "or NDI), which sets its video outputs for that use. In "
                    "Teams only USB carries video. Dante is set on the "
                    "camera's web page."
                ),
            },
            # ── Tally and menu ──
            "set_tally": {
                "label": "Set Tally Lamp",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": ["off", "red", "green", "amber"],
                        "label": "Lamp",
                    },
                },
                "help": "Lights the tally lamp red, green or amber, or turns it off.",
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
            # ── RTMP streaming ──
            "rtmp_start": {
                "label": "Start RTMP Stream",
                "params": {},
                "help": "Starts streaming to the RTMP server and stream key saved in the camera.",
                "sets": {"rtmp_status": "streaming"},
            },
            "rtmp_stop": {
                "label": "Stop RTMP Stream",
                "params": {},
                "help": "Stops the RTMP stream.",
                "sets": {"rtmp_status": "stopped"},
            },
            "save_rtmp_url": {
                "label": "Save RTMP Server URL",
                "params": {
                    "url": {"type": "string", "label": "Server URL"},
                },
                "help": (
                    "Saves where the camera streams when RTMP is started, from "
                    "the video platform. Blank clears it."
                ),
                "sets": {"rtmp_url": "{url}"},
            },
            "save_rtmp_key": {
                "label": "Save RTMP Stream Key",
                "params": {
                    "key": {"type": "string", "label": "Stream Key", "secret": True},
                },
                "help": (
                    "Saves the stream key from the video platform in the "
                    "camera. Blank clears it. The key is never shown."
                ),
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
                "help": "Returns every setting to factory values.",
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
        self._poll_count = 0
        # Why the CGI reads were skipped this connection, or "".
        self._cgi_unavailable = ""
        # The camera's refusal of this poll's CGI read, or "".
        self._cgi_refusal = ""
        # A reply this connection could not read, logged once.
        self._unread_logged: set[str] = set()

    # ── Connection lifecycle ──

    async def _pre_connect(self) -> None:
        if not self.config.get("host", ""):
            raise ConnectionError(f"[{self.device_id}] host is required")
        self._visca_lock = asyncio.Lock()
        self._sequence = 0
        self._poll_count = 0
        self._cgi_unavailable = ""
        self._unread_logged = set()
        # Rows that print &(random) or &_=X take a number the camera has not
        # seen just before ("cannot be the same, otherwise the camera will
        # ignore this command"), so it counts up from the clock, the way the
        # manual's examples look.
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
            http_ok = await self._read_cgi(url=True)
        except ConnectionFaultError:
            raise
        except ConnectionError:
            http_ok = False
        if not visca_ok and not http_ok:
            raise ConnectionError(
                f"The camera at {self.config.get('host')} answered neither "
                f"VISCA over IP nor HTTP."
            )
        host = self.config.get("host")
        self.set_state("preview_url", f"rtsp://{host}:554/live_st1")
        self.set_state("preview_format", "rtsp")
        self.set_state("mjpeg_url", f"{self._base_url}/snapshot/snapshot?action=get")
        # The wide-angle lens's stream (RTSP Security off; with it on, a
        # player needs the web login in the URL, which is not published).
        wide = {
            "name": "Wide-Angle Lens",
            "preview_url": f"rtsp://{host}:8554/live_st2",
            "preview_format": "rtsp",
        }
        self.register_child("lens", "wide", initial_state=wide)
        self.set_child_state_batch("lens", "wide", wide)

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
            # (found in aver_tr's simulation, where the two arrive back to
            # back).
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
        elif name == "tracking_mode":
            if len(reply) == 4:
                self.set_state("tracking_mode", _TRACKING_MODES.get(code, "other"))
        elif name == "tracking_body":
            if code in (0x01, 0x02):
                self.set_state("tracking_body", "full_body" if code == 0x01 else "upper_body")
        elif name in ("hdmi1_source", "hdmi2_source"):
            if code in _HDMI_SOURCES:
                self.set_state(name, _HDMI_SOURCES[code])
        elif name == "pip_pbp_mode":
            if len(reply) == 4 and 1 <= code <= 8:
                self.set_state("pip_pbp_mode", code)
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

    async def _special(self, code: int, read: str, expect: str | None) -> None:
        """Set a CAM_Memory Special preset and confirm the change it makes.

        ``expect`` is the value ``read`` should show, or None for "anything
        but what it showed before" (Next Tracking Mode). A camera whose VISCA
        Customized Function has made the number a normal preset saves its
        position there instead and changes nothing, which is refused."""
        before = self.get_state(read)
        await self._visca_command(bytes([0x81, 0x01, 0x04, 0x3F, 0x01, code, 0xFF]))
        if expect is not None and before == expect:
            return  # already there: nothing to tell a change from
        answered = False
        for attempt in range(_SPECIAL_CONFIRM_TRIES):
            if attempt:
                await asyncio.sleep(_SPECIAL_CONFIRM_GAP_S)
            reply = await self._inquire(_INQUIRY[read])
            if reply is None:
                return  # VISCA went quiet: the poll reports it
            if not reply:
                continue
            answered = True
            self._apply(read, reply)
            now = self.get_state(read)
            if (now == expect) if expect is not None else (now != before):
                return
        if answered:
            raise CommandRefusedError(
                f"The camera did not change it. If its VISCA Customized "
                f"Function setting makes {code} (0x{code:02X}) a normal "
                f"preset, the camera saved its position as preset {code} "
                f"instead."
            )

    # ── HTTP CGI ──

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _cgi(self, query: str, timeout: float | None = None) -> str:
        """GET /cgi-bin?<query>. Returns the body of a 200; raises otherwise."""
        if self._http is None:
            raise ConnectionError("Not connected")
        try:
            if timeout is None:
                resp = await self._http.get(f"/cgi-bin?{query}")
            else:
                resp = await self._http.get(f"/cgi-bin?{query}", timeout=timeout)
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
        # Set=<key>,3,<value>&(<id>): the form the table prints.
        await self._cgi(f"Set={setting}&({self._next_id()})")

    def _unread(self, what: str, text: str) -> None:
        if what not in self._unread_logged:
            self._unread_logged.add(what)
            log.warning(f"[{self.device_id}] {what} reply not understood: {text[:80]!r}")

    async def _read_rtmp_status(self) -> None:
        # Printed as Get=vdo_rtmp_status, with no suffix and no reply shown
        # beyond "2: Streaming, 0: Stopped": read the number whether the
        # camera answers it alone or as name=value, as the tracking read does.
        text = await self._cgi("Get=vdo_rtmp_status", timeout=_POLL_HTTP_TIMEOUT_S)
        match = re.search(r"vdo_rtmp_status(?:,\d+)?\s*=\s*(\d+)", text) or re.fullmatch(
            r"\s*(\d+)\s*", text
        )
        if match is None:
            self._unread("RTMP status", text)
            return
        self.set_state("rtmp_status", _RTMP_STATUS.get(int(match.group(1)), "other"))

    @staticmethod
    def _string_reply(text: str) -> str:
        """A string read's value: the body itself, or what follows name= ."""
        value = text.strip()
        match = re.fullmatch(r"[A-Za-z_]\w*(?:,\d+)?=(.*)", value, re.S)
        return (match.group(1) if match else value).strip()

    async def _read_rtmp_url(self) -> str:
        url = self._string_reply(await self._cgi("GetRtmpUrl", timeout=_POLL_HTTP_TIMEOUT_S))
        self.set_state("rtmp_url", url)
        return url

    async def _read_cgi(self, url: bool) -> bool:
        """The poll's CGI reads. True when the camera answered.

        A camera with a web login answers 401 here when none is entered: that
        is a missing login, not a dead camera, so Last Error says why and
        VISCA keeps working. The read is still made on every poll, because
        the 401 is what shows the camera is still there."""
        self._cgi_refusal = ""
        self._cgi_unavailable = ""
        try:
            await self._read_rtmp_status()
            if url:
                await self._read_rtmp_url()
        except _LoginRequired:
            self._cgi_unavailable = (
                "The camera asks for its web login, which is not entered, so "
                "RTMP Stream and the RTMP Server URL are not read."
            )
        except CommandRefusedError as exc:
            # The camera answered (a 403, say, for an account that may not
            # read it): it is up, so this is reported, not counted as silence.
            self._cgi_refusal = f"The RTMP stream could not be read: {exc}"
        return True

    def _http_silent(self) -> str:
        port = int(self.config.get("port", 80))
        if self.get_state("power") == "standby":
            return (
                f"The camera's web interface (TCP {port}) is not answering in "
                f"standby, as it does when the camera's Power Mode is Full "
                f"Shutdown: presets, picture settings, RTMP and Factory Reset "
                f"are unavailable until it is powered on."
            )
        return (
            f"The camera's web interface (TCP {port}) is not answering, though "
            f"VISCA over IP is: presets, picture settings, RTMP and Factory "
            f"Reset are unavailable. Reboot Camera restarts it."
        )

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
            if command in ("rtmp_start", "rtmp_stop"):
                await self._read_rtmp_status_quietly()
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
            case "set_tracking_mode":
                mode = str(params["mode"])
                await self._special(_TRACKING_SPECIALS[mode], "tracking_mode", mode)
            case "next_tracking_mode":
                await self._special(_NEXT_MODE_SPECIAL, "tracking_mode", None)
            case "set_tracking_body":
                size = str(params["size"])
                await self._special(_BODY_SPECIALS[size], "tracking_body", size)
            case "preset_clear":
                number = int(params["number"])
                if not 0 <= number <= 254:
                    raise ValueError("Preset Clear takes 0-254.")
                await self._visca_command(bytes([0x81, 0x01, 0x04, 0x3F, 0x00, number, 0xFF]))
            case "preset_recall":
                # The CGI, not VISCA 3F 02: the camera can be set to give some
                # VISCA preset numbers special meanings (VISCA Customized
                # Function); the CGI's are plain presets.
                await self._cgi(f"ActPreset=0,{int(params['number'])}&({self._next_id()})")
            case "preset_save":
                await self._cgi(f"ActPreset=1,{int(params['number'])}&({self._next_id()})")
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
                await self._cgi_set(f"img_expo_expo,3,{int(params['value'])}")
            case "set_wb_mode":
                await self._visca_command(
                    bytes([0x81, 0x01, 0x04, 0x35, _WB_MODE_CODES[str(params["mode"])], 0xFF])
                )
            case "set_saturation":
                await self._cgi_set(f"img_saturation,3,{int(params['value'])}")
            case "set_contrast":
                await self._cgi_set(f"img_contrast,3,{int(params['value'])}")
            case "set_tally":
                await self._visca_command(
                    bytes([0x81, 0x01, 0x7E, 0x01, 0x0A, 0x00, _TALLY_CODES[str(params["mode"])], 0xFF])
                )
            case "set_hdmi1_source" | "set_hdmi2_source":
                output = 0x01 if command == "set_hdmi1_source" else 0x02
                await self._visca_command(
                    bytes([0x81, 0x01, 0x36, 0x69, 0x07, output,
                           _HDMI_SOURCE_CODES[str(params["source"])], 0xFF])
                )
            case "set_pip_pbp_mode":
                mode = int(params["mode"])
                if not 1 <= mode <= 8:
                    raise ValueError("PIP / PBP Layout takes 1-8.")
                await self._visca_command(bytes([0x81, 0x01, 0x36, 0x69, 0x08, mode, 0xFF]))
            case "set_theme_mode":
                await self._visca_command(
                    bytes([0x81, 0x01, 0x04, 0xA3, _THEME_MODE_CODES[str(params["mode"])], 0xFF])
                )
            case "save_rtmp_url":
                url = str(params.get("url") or "")
                await self._cgi(f"SaveRtmpUrl={quote(url, safe=':/')}")
                kept = await self._read_rtmp_url()
                if kept != url:
                    raise CommandRefusedError(
                        "The camera did not keep that RTMP server URL."
                    )
            case "save_rtmp_key":
                key = str(params.get("key") or "")
                await self._cgi(f"SaveRtmpKey={quote(key, safe='')}")
                # Read back only to confirm: the key is never put in state,
                # which reaches the cloud and the logs.
                kept = self._string_reply(await self._cgi("GetRtmpKey"))
                if kept != key:
                    raise CommandRefusedError("The camera did not keep that stream key.")
            case "factory_reset":
                await self._cgi(f"OnePush=d&_={self._next_id()}")
            case _:
                raise ValueError(f"Unknown command: {command}")

    async def _read_rtmp_status_quietly(self) -> None:
        """RTMP Start / Stop are VISCA; the stream's status is the CGI's. A
        camera whose web interface is not answering just leaves it to the
        poll."""
        try:
            await self._read_rtmp_status()
        except (ConnectionError, _LoginRequired, CommandRefusedError):
            pass

    # ── Device settings ──

    _SETTING_COMMANDS = {
        "exposure_mode": ("set_exposure_mode", "mode"),
        "wb_mode": ("set_wb_mode", "mode"),
        "backlight": ("set_backlight", "enabled"),
        "wdr": ("set_wdr", "enabled"),
        "preset_freeze": ("set_preset_freeze", "enabled"),
        "hdmi1_source": ("set_hdmi1_source", "source"),
        "hdmi2_source": ("set_hdmi2_source", "source"),
        "pip_pbp_mode": ("set_pip_pbp_mode", "mode"),
        "rtmp_url": ("save_rtmp_url", "url"),
    }

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if key == "focus_mode":
            await self.send_command("focus_auto" if str(value) == "auto" else "focus_manual")
            return
        spec = self._SETTING_COMMANDS.get(key)
        if spec is None:
            raise ValueError(f"Unknown device setting: {key}")
        command, param = spec
        kind = self.DRIVER_INFO["device_settings"][key]["type"]
        if kind == "boolean":
            value = value if isinstance(value, bool) else str(value).strip().lower() in (
                "1", "true", "on", "yes",
            )
        elif kind == "integer":
            value = int(value)
        await self.send_command(command, {param: value})

    # ── Polling ──

    async def poll(self) -> None:
        if self._http is None:
            return
        self._poll_count += 1
        problems: list[str] = []
        # VISCA: stop at the first inquiry nothing answers. The camera answers
        # one it does not support with an error, so silence means VISCA over
        # IP is not reaching it, and waiting out the rest would only make the
        # poll run long.
        visca_answered = False
        for name, payload in _INQUIRIES:
            reply = await self._inquire(payload)
            if reply is None:
                target = self._visca_target or ("", 0)
                problems.append(
                    f"The camera did not answer VISCA over IP on UDP port {target[1]}."
                )
                break
            visca_answered = True
            self._apply(name, reply)
        # The CGI. With VISCA silent too, a transport error raises: that is
        # what takes a camera that stopped answering offline. With VISCA
        # answering, the camera is up and only its web interface is not (Full
        # Shutdown standby; firmware 0.0.0000.43 added a VISCA restart for a
        # web server that stops), so it is reported instead.
        try:
            await self._read_cgi(url=self._poll_count % _RTMP_URL_EVERY == 0)
        except ConnectionFaultError:
            raise
        except ConnectionError:
            if not visca_answered:
                raise
            problems.append(self._http_silent())
        if self._cgi_unavailable:
            problems.append(self._cgi_unavailable)
        if self._cgi_refusal:
            problems.append(self._cgi_refusal)
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
