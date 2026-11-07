"""
OpenAVC Sony SRG-A40 / SRG-A12 PTZ Auto Framing Camera Driver.

Sony's AI auto framing PTZ cameras: the SRG-A40 (20x optical, Clear Image
Zoom and digital zoom to 240x) and the SRG-A12 (12x optical). Two control
surfaces, both in Sony's "VISCA Command List / CGI Command List" for the
SRG-A40 / A12, Software Version 4.0:

  * VISCA over IP (UDP 52381; Sony's 8-byte header): power, pan / tilt /
    zoom / focus, presets 1-64, exposure, white balance, picture, tally, PTZ
    Auto Framing start and stop, the on-screen menu, and an inquiry for
    nearly every one of them. Needs no login.
  * HTTP CGI (TCP 80, ``/command/<cgi>?...`` and ``/analytics/<cgi>?...``,
    read through ``/command/inquiry.cgi?inq=<group>``; HTTP Digest sign-in):
    presets 1-256 with names, every PTZ Auto Framing setting and action and
    its tracking status, the streaming mode, RTMP / SRT / NDI, the tally
    level, model, serial, software version, the temperature warning, reboot
    and factory reset.

Not the camera ``sony_visca`` was built for (the SRG-X400 / X120 and BRC-X400
command list): the A40 / A12 list has no picture profiles, color matrix,
PRESET MODE or PTZ TRACE, and has the auto framing, detail, noise reduction,
tele convert, preset speed and video format commands and the whole CGI.

Push vs poll
------------
Neither surface pushes to a controller. VISCA over IP is request / response.
The CGI list has ``subscribe.cgi``, ``pullinquiry.cgi`` and
``unsubscribe.cgi``, each marked "Displays for Web App. Do not set this.",
so the driver does not use them and polls: the VISCA inquiries for
everything the camera reports that way (a short list every poll, the
picture settings every sixth), and ``inquiry.cgi`` for the auto framing
status and settings every poll and the rest less often.

Why Python
----------
Two transports at once (VISCA over UDP beside the HTTP CGI) from one driver,
VISCA's binary framing and ACK / completion / error replies, and the
tally's 15-second expiry, which needs a timer of the driver's own. The same
shape as ``aver_tr``.

What the document asks of a controller
---------------------------------------
* The tally lamp "is turned off if not receiving an ON command from any
  controller for 15 seconds" (VISCA over IP limitations), so Tally On is
  sent again every 5 s until Tally Off.
* "Do not transmit the command ... when any menu is displayed on the
  screen": a command the camera refuses as not executable while the menu
  is open is reported as that.
* The camera answers a sequence number it has already seen with a control
  ERROR (0F 01); the driver resets the sequence and sends once more.
* The CGI checks the Referer header by default and signs in with HTTP
  Digest; repeated refusals are treated as a brute force attack (8 by
  default), so a refused password is ``auth_failed`` and never retried.

Sources (archived as ``driver-roadmap/reference-docs/sony-srg-a40-a12-*``)
------------------------------------------------------------------------
* VISCA Command List / CGI Command List, SRG-A40 / A12, Software Version 4.0
  (5-053-155-16 (1), released 2026-06-09).
* SRG-A40 / A12 Operating Instructions (released 2026-04-13): the CAMERA
  SETUP switches, user roles, Referer Check and brute force defaults.
"""

from __future__ import annotations

import asyncio
import json
import re
import struct
import time
from typing import Any, Callable
from urllib.parse import quote

import httpx

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.utils.logger import get_logger

try:
    from openavc.drivers.base import CommandRefusedError
except ImportError:  # OpenAVC before 0.37.1
    CommandRefusedError = RuntimeError

log = get_logger(__name__)

# --- tables ---

# ── VISCA over IP framing ("Communication Method of VISCA over IP") ──

_VISCA_PORT = 52381

PAYLOAD_VISCA_COMMAND = 0x0100
PAYLOAD_VISCA_INQUIRY = 0x0110
PAYLOAD_VISCA_REPLY = 0x0111
PAYLOAD_CONTROL_CMD = 0x0200
PAYLOAD_CONTROL_REPLY = 0x0201

# On/off settings: command -> (state key, packet before the code, on, off).
# The inquiry for each is the same packet with 01 made 09 (the list's
# Inquiry Command List).
_VISCA_SWITCHES: dict[str, tuple[str, bytes, int, int]] = {
    "set_backlight": ("backlight", b"\x81\x01\x04\x33", 0x02, 0x03),
    "set_spotlight": ("spotlight", b"\x81\x01\x04\x3a", 0x02, 0x03),
    "set_high_sensitivity": ("high_sensitivity", b"\x81\x01\x04\x5e", 0x02, 0x03),
    "set_exposure_comp": ("exposure_comp", b"\x81\x01\x04\x3e", 0x02, 0x03),
    "set_auto_slow_shutter": ("auto_slow_shutter", b"\x81\x01\x04\x5a", 0x02, 0x03),
    # Visibility Enhancer is on with 6, not 2.
    "set_visibility_enhancer": ("visibility_enhancer", b"\x81\x01\x04\x3d", 0x06, 0x03),
    "set_low_light": ("low_light", b"\x81\x01\x05\x39", 0x02, 0x03),
    "set_flicker_cancel": ("flicker_cancel", b"\x81\x01\x04\x32", 0x02, 0x03),
    "set_image_stabilizer": ("image_stabilizer", b"\x81\x01\x04\x34", 0x02, 0x03),
    "set_high_resolution": ("high_resolution", b"\x81\x01\x04\x52", 0x02, 0x03),
    "set_tele_convert": ("tele_convert", b"\x81\x01\x7e\x04\x36", 0x02, 0x03),
    "set_image_flip": ("image_flip", b"\x81\x01\x04\x66", 0x02, 0x03),
    "set_ir_receive": ("ir_receive", b"\x81\x01\x06\x08", 0x02, 0x03),
    "set_pan_tilt_slow": ("pan_tilt_slow", b"\x81\x01\x06\x44", 0x02, 0x03),
}

# One-code settings: command -> (state key, packet before the code, value -> code).
_VISCA_ENUMS: dict[str, tuple[str, bytes, dict[str, int]]] = {
    "set_exposure_mode": (
        "exposure_mode",
        b"\x81\x01\x04\x39",
        {
            "full_auto": 0x00,
            "manual": 0x03,
            "shutter_priority": 0x0A,
            "iris_priority": 0x0B,
        },
    ),
    "set_wb_mode": (
        "wb_mode",
        b"\x81\x01\x04\x35",
        {
            "auto1": 0x00,
            "indoor": 0x01,
            "outdoor": 0x02,
            "one_push": 0x03,
            "auto2": 0x04,
            "manual": 0x05,
        },
    ),
    "set_picture_effect": (
        "picture_effect",
        b"\x81\x01\x04\x63",
        {
            "off": 0x00,
            "black_white": 0x04,
        },
    ),
    "set_zoom_mode": (
        "zoom_mode",
        b"\x81\x01\x04\x06",
        {
            "optical": 0x03,
            "clear_image": 0x04,
            "digital": 0x02,
        },
    ),
    "set_af_mode": (
        "af_mode",
        b"\x81\x01\x04\x57",
        {
            "normal": 0x00,
            "interval": 0x01,
            "zoom_trigger": 0x02,
        },
    ),
    "set_af_sensitivity": (
        "af_sensitivity",
        b"\x81\x01\x04\x58",
        {"normal": 0x02, "low": 0x03},
    ),
    "set_ir_correction": (
        "ir_correction",
        b"\x81\x01\x04\x11",
        {"standard": 0x00, "ir_light": 0x01},
    ),
    "set_standby_mode": (
        "standby_mode",
        b"\x81\x01\x7e\x04\x50",
        {"side": 0x02, "neutral": 0x03},
    ),
    "set_preset_call_mode": (
        "preset_call_mode",
        b"\x81\x01\x7e\x04\x3b",
        {
            "freeze": 0x02,
            "normal": 0x03,
        },
    ),
    "set_hdmi_color": (
        "hdmi_color",
        b"\x81\x01\x7e\x01\x03\x00",
        {"ycbcr": 0x00, "rgb": 0x01},
    ),
}

# Numeric settings sent as one 0p nibble: command -> (state key, packet, min, max).
_VISCA_NIBBLE_LEVELS: dict[str, tuple[str, bytes, int, int]] = {
    "set_wb_speed": ("wb_speed", b"\x81\x01\x04\x56", 1, 5),
    "set_chroma_suppress": ("chroma_suppress", b"\x81\x01\x04\x5f", 0, 3),
}

# Numeric settings sent as 00 00 0p 0q (an 8-bit value in two nibbles):
# command -> (state key, packet, min, max).
_VISCA_BYTE_LEVELS: dict[str, tuple[str, bytes, int, int]] = {
    "set_red_gain": ("red_gain", b"\x81\x01\x04\x43", 0, 255),
    "set_blue_gain": ("blue_gain", b"\x81\x01\x04\x44", 0, 255),
    "set_detail_level": ("detail_level", b"\x81\x01\x04\x42", 0, 15),
    "set_shutter": ("shutter", b"\x81\x01\x04\x4a", 6, 33),
}

# Iris: the F-number the VISCA code stands for (VISCA/CGI Command Setting
# Values, IRIS).
_IRIS_CODES: dict[str, int] = {
    "F2.0": 0x19,
    "F2.2": 0x18,
    "F2.4": 0x17,
    "F2.6": 0x16,
    "F2.8": 0x15,
    "F3.1": 0x14,
    "F3.4": 0x13,
    "F3.7": 0x12,
    "F4.0": 0x11,
    "F4.4": 0x10,
    "F4.8": 0x0F,
    "F5.2": 0x0E,
    "F5.6": 0x0D,
    "F6.2": 0x0C,
    "F6.8": 0x0B,
    "F7.3": 0x0A,
    "F8.0": 0x09,
    "F8.7": 0x08,
    "F9.6": 0x07,
    "F10": 0x06,
    "F11": 0x05,
    "close": 0x00,
}
_IRIS_NAMES = {v: k for k, v in _IRIS_CODES.items()}

# Noise reduction (8x 01 04 53 pp FF): 0 off to 5 strong; 7F means the 2D
# and 3D levels are set separately.
_NR_NAMES = {0: "off", 1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 0x7F: "2d_3d"}
_NR_CODES = {v: k for k, v in _NR_NAMES.items() if k != 0x7F}

# Parameterless VISCA commands.
_SIMPLE_COMMANDS: dict[str, bytes] = {
    "power_on": b"\x81\x01\x04\x00\x02\xff",
    "power_off": b"\x81\x01\x04\x00\x03\xff",
    "pt_home": b"\x81\x01\x06\x04\xff",
    "pt_reset": b"\x81\x01\x06\x05\xff",
    "zoom_stop": b"\x81\x01\x04\x07\x00\xff",
    "focus_stop": b"\x81\x01\x04\x08\x00\xff",
    "focus_auto": b"\x81\x01\x04\x38\x02\xff",
    "focus_manual": b"\x81\x01\x04\x38\x03\xff",
    "focus_one_push": b"\x81\x01\x04\x18\x01\xff",
    "focus_infinity": b"\x81\x01\x04\x18\x02\xff",
    "auto_framing_start": b"\x81\x01\x7e\x04\x3a\x01\xff",
    "auto_framing_stop": b"\x81\x01\x7e\x04\x3a\x00\xff",
    "wb_one_push_trigger": b"\x81\x01\x04\x10\x05\xff",
    "red_gain_up": b"\x81\x01\x04\x03\x02\xff",
    "red_gain_down": b"\x81\x01\x04\x03\x03\xff",
    "blue_gain_up": b"\x81\x01\x04\x04\x02\xff",
    "blue_gain_down": b"\x81\x01\x04\x04\x03\xff",
    "iris_up": b"\x81\x01\x04\x0b\x02\xff",
    "iris_down": b"\x81\x01\x04\x0b\x03\xff",
    "gain_up": b"\x81\x01\x04\x0c\x02\xff",
    "gain_down": b"\x81\x01\x04\x0c\x03\xff",
    "shutter_up": b"\x81\x01\x04\x0a\x02\xff",
    "shutter_down": b"\x81\x01\x04\x0a\x03\xff",
    "exposure_comp_up": b"\x81\x01\x04\x0e\x02\xff",
    "exposure_comp_down": b"\x81\x01\x04\x0e\x03\xff",
    "osd_menu": b"\x81\x01\x06\x06\x10\xff",
    "osd_enter": b"\x81\x01\x7e\x01\x02\x00\x01\xff",
}

# Pan / tilt drive directions (8x 01 06 01 VV WW <pan> <tilt> FF).
_PT_DIRECTIONS: dict[str, tuple[int, int]] = {
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

# Inquiries read on every poll, power first: the camera answers it in every
# state, so silence there means VISCA over IP is not reaching it.
_FAST_INQUIRIES: tuple[tuple[str, bytes], ...] = (
    ("power", b"\x81\x09\x04\x00\xff"),
    ("auto_framing", b"\x81\x09\x7e\x04\x3a\xff"),
    ("pan_tilt", b"\x81\x09\x06\x12\xff"),
    ("zoom_position", b"\x81\x09\x04\x47\xff"),
    ("focus_position", b"\x81\x09\x04\x48\xff"),
    ("focus_mode", b"\x81\x09\x04\x38\xff"),
    ("last_preset", b"\x81\x09\x04\x3f\xff"),
    ("tally", b"\x81\x09\x7e\x01\x0a\xff"),
    ("osd_menu", b"\x81\x09\x06\x06\xff"),
)

# Read every sixth poll (30 s at the default interval) and after a command
# that changes one of them.
_SLOW_INQUIRIES: tuple[tuple[str, bytes], ...] = (
    ("exposure_mode", b"\x81\x09\x04\x39\xff"),
    ("iris", b"\x81\x09\x04\x4b\xff"),
    ("gain", b"\x81\x09\x04\x4c\xff"),
    ("shutter", b"\x81\x09\x04\x4a\xff"),
    ("exposure_comp_level", b"\x81\x09\x04\x4e\xff"),
    ("wb_mode", b"\x81\x09\x04\x35\xff"),
    ("red_gain", b"\x81\x09\x04\x43\xff"),
    ("blue_gain", b"\x81\x09\x04\x44\xff"),
    ("wb_speed", b"\x81\x09\x04\x56\xff"),
    ("chroma_suppress", b"\x81\x09\x04\x5f\xff"),
    ("detail_level", b"\x81\x09\x04\x42\xff"),
    ("noise_reduction", b"\x81\x09\x04\x53\xff"),
    ("defog", b"\x81\x09\x04\x37\xff"),
    ("picture_effect", b"\x81\x09\x04\x63\xff"),
    ("zoom_mode", b"\x81\x09\x04\x06\xff"),
    ("af_mode", b"\x81\x09\x04\x57\xff"),
    ("af_sensitivity", b"\x81\x09\x04\x58\xff"),
    ("ir_correction", b"\x81\x09\x04\x11\xff"),
    ("standby_mode", b"\x81\x09\x7e\x04\x50\xff"),
    ("preset_call_mode", b"\x81\x09\x7e\x04\x3b\xff"),
    ("hdmi_color", b"\x81\x09\x7e\x01\x03\xff"),
    ("video_format", b"\x81\x09\x7e\x04\x33\xff"),
) + tuple(
    (state, b"\x81\x09" + prefix[2:] + b"\xff")
    for state, prefix, _on, _off in _VISCA_SWITCHES.values()
)

_INQUIRY: dict[str, bytes] = dict(_FAST_INQUIRIES + _SLOW_INQUIRIES)

# VIDEO OUTPUT FORMAT (the VISCA parameter and the format it stands for).
_VIDEO_FORMATS: dict[int, str] = {
    0x00: "720/59.94p (HDMI 640x480)",
    0x01: "720/59.94p",
    0x02: "1080/29.97p",
    0x03: "1080/59.94i",
    0x04: "1080/59.94p Level A",
    0x05: "1080/59.94p Level B",
    0x06: "2160/29.97p",
    0x11: "720/50p",
    0x12: "1080/25p",
    0x13: "1080/50i",
    0x14: "1080/50p Level A",
    0x15: "1080/50p Level B",
    0x16: "2160/25p",
    0x21: "1080/23.98p",
    0x22: "2160/23.98p",
}

_ON_OFF = {0x02: True, 0x03: False}

# The CGI settings: command -> (path, parameter, state key, kind, choices,
# the inquiry group that reads it back). An enum's choices map the driver's
# value to the camera's word.
_CGI_SETTINGS: dict[str, tuple[str, str, str, str, Any, str]] = {
    "set_framing_mode": (
        "analytics/ptzautoframing.cgi",
        "PtzAutoFramingFramingMode",
        "framing_mode",
        "enum",
        {"person": "person", "ball_sports": "ball_sports"},
        "ptzautoframing",
    ),
    "set_auto_framing_trigger": (
        "analytics/ptzautoframing.cgi",
        "PtzAutoFramingTriggerType",
        "auto_framing_trigger",
        "enum",
        {"auto": "auto", "manual": "manual"},
        "ptzautoframing",
    ),
    "set_auto_framing_auto_start": (
        "analytics/ptzautoframing.cgi",
        "PtzAutoFramingAutoStartEnable",
        "auto_framing_auto_start",
        "bool",
        None,
        "ptzautoframing",
    ),
    "set_multi_tracking": (
        "analytics/ptzautoframing.cgi",
        "PtzAutoFramingMultiTrackingEnable",
        "multi_tracking",
        "bool",
        None,
        "ptzautoframing",
    ),
    "set_multi_tracking_targets": (
        "analytics/ptzautoframing.cgi",
        "PtzAutoFramingMultiTrackingTargetNum",
        "multi_tracking_targets",
        "int",
        (2, 8),
        "ptzautoframing",
    ),
    "set_multi_tracking_wait": (
        "analytics/ptzautoframing.cgi",
        "PtzAutoFramingMultiTrackingWaitTime",
        "multi_tracking_wait",
        "int",
        (0, 60),
        "ptzautoframing",
    ),
    "set_lead_room": (
        "analytics/ptzautoframing.cgi",
        "PtzAutoFramingLeadRoomLevel",
        "lead_room",
        "enum",
        {"off": "off", "low": "low", "middle": "middle", "high": "high"},
        "ptzautoframing",
    ),
    "set_face_indicator": (
        "analytics/ptzautoframing.cgi",
        "PtzAutoFramingFaceIndicatorEnable3",
        "face_indicator",
        "bool",
        None,
        "ptzautoframing",
    ),
    "set_individual_identification": (
        "analytics/ptzautoframing.cgi",
        "PtzAutoFramingIndividualIdentification",
        "individual_identification",
        "bool",
        None,
        "ptzautoframing",
    ),
    "set_registered_face_tracking": (
        "analytics/ptzautoframing.cgi",
        "PtzAutoFramingRegisteredFaceTracking",
        "registered_face_tracking",
        "bool",
        None,
        "ptzautoframing",
    ),
    "set_lost_wait_time": (
        "analytics/ptzautoframing.cgi",
        "PtzAutoFramingLostWaitTime",
        "lost_wait_time",
        "int",
        (1, 60),
        "ptzautoframing",
    ),
    "set_face_lost_wait_time": (
        "analytics/ptzautoframing.cgi",
        "PtzAutoFramingFaceLostWaitTime",
        "face_lost_wait_time",
        "int",
        (0, 60),
        "ptzautoframing",
    ),
    "set_detection_restart_time": (
        "analytics/ptzautoframing.cgi",
        "PtzAutoFramingDetectionRestartTime",
        "detection_restart_time",
        "int",
        (0, 600),
        "ptzautoframing",
    ),
    "set_tally_level": (
        "command/tally.cgi",
        "TallyLevel",
        "tally_level",
        "enum",
        {"off": "off", "low": "low", "high": "high"},
        "tally",
    ),
    "set_tally_follows_auto_framing": (
        "command/tally.cgi",
        "TallyIndicationPtzAutoFraming",
        "tally_follows_auto_framing",
        "bool",
        None,
        "tally",
    ),
    "set_stream_mode": (
        "command/stream.cgi",
        "StreamMode",
        "stream_mode",
        "enum",
        {
            "rtsp": "rtsp",
            "rtmp": "rtmp",
            "srt_caller": "srt-caller",
            "srt_listener": "srt-listener",
            "ndi_hx": "ndi_hx",
            "off": "off",
        },
        "stream",
    ),
    "set_ndi_source_name": (
        "command/ndi.cgi",
        "NdiSourceName",
        "ndi_source_name",
        "text",
        (0, 64),
        "ndi",
    ),
}

# What each inquiry group's reply sets: camera parameter -> (state key, kind,
# choices). Bool words are on / off; an enum maps the camera's word back.
_CGI_READS: dict[str, tuple[str, str, Any]] = {
    spec[1]: (spec[2], spec[3], spec[4]) for spec in _CGI_SETTINGS.values()
}
_CGI_READS.update(
    {
        "PtzAutoFramingTrackingStatus": (
            "auto_framing_status",
            "enum",
            {
                w: w
                for w in (
                    "idle",
                    "missing",
                    "preparing",
                    "searching",
                    "tracking",
                    "waiting",
                    "fixed_angle",
                )
            },
        ),
        "RtmpServerUrl1": ("rtmp_url", "text", None),
        "SrtServerUrl1": ("srt_url", "text", None),
        "StreamStatus": (
            "stream_status",
            "enum",
            {
                "invalid": "invalid",
                "off": "off",
                "streaming": "streaming",
                "ready": "ready",
                "ready-ssl": "ready_ssl",
                "streaming-ssl": "streaming_ssl",
            },
        ),
        "TemperatureWarning": ("temperature_warning", "flag", None),
        "ModelName": ("model", "text", None),
        "Serial": ("serial_number", "text", None),
        "SoftVersion": ("software_version", "text", None),
    }
)

# The inquiry groups each poll reads: every poll, every sixth, every twelfth.
_CGI_EVERY_POLL = ("ptzautoframing",)
_CGI_EVERY_6TH = ("tally", "stream", "status", "rtmp", "srt", "ndi")
_CGI_EVERY_12TH = ("presetposition",)
_CGI_AT_CONNECT = ("system", "sysinfo")

# Characters the camera refuses in a preset or composition preset name, and
# those it reads as separators in any value (%26 is read as &, %2C as ,).
_NAME_FORBIDDEN = set('"%&,:~')
_VALUE_SEPARATORS = set("&,")

_PRESETS_OVER_VISCA = 64  # VISCA PRESET pp 00-63 is presets 1-64

# --- end tables ---

# How often Tally On is sent again (the camera turns it off after 15 s).
_TALLY_REFRESH_S = 5.0
# A tally that reads off this soon after the driver lit it was turned off by
# another controller, not by the expiry: the driver stops holding it on.
_TALLY_OVERRIDE_S = 12.0
# "Wait at least 10 seconds if you want to turn on the camera again after
# putting it in the standby mode." (Operating Instructions p. 27)
_POWER_ON_GAP_S = 10.0

_SLOW_EVERY = 6
_POLL_HTTP_TIMEOUT_S = 3.0

# Zoom position ranges (VISCA/CGI Command Setting Values, ZOOM MAGNIFICATION).
_ZOOM_MAX = {"SRG-A40": 0x7AC0, "SRG-A12": 0x4000}


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


def _signed16(value: int) -> int:
    return value - 0x10000 if value & 0x8000 else value


def _parse_inquiry(text: str) -> dict[str, str]:
    """``name=value&name=value`` (the standard format). Split only where a
    new ``Name=`` starts, so a value holding ``&`` stays whole."""
    out: dict[str, str] = {}
    for part in re.split(r"&(?=[A-Za-z][A-Za-z0-9]*=)", text.strip()):
        name, sep, value = part.partition("=")
        if sep:
            out[name.strip()] = value.strip()
    return out


def _pairs(value: str) -> list[tuple[int, str]]:
    """``1,Lectern,2,Wide`` -> [(1, "Lectern"), (2, "Wide")]."""
    fields = value.split(",") if value else []
    pairs = []
    for i in range(0, len(fields) - 1, 2):
        try:
            pairs.append((int(fields[i]), fields[i + 1]))
        except ValueError:
            continue
    return pairs


def _options(value: str) -> str:
    """A picker list from ``1,Lectern,2,Wide`` (a preset with no name shows
    its number alone)."""
    return json.dumps(
        [
            {"value": n, "label": f"{n}: {label}" if label else str(n)}
            for n, label in _pairs(value)
        ]
    )


def _settle(future: asyncio.Future | None, value: Any) -> None:
    """Give a waiting future its result; one already settled is left alone."""
    if future is not None and not future.done():
        future.set_result(value)


# ── Values a command takes ──


def _bounded(value: Any, low: int, high: int) -> int:
    number = int(value)
    if not low <= number <= high:
        raise ValueError(f"{number} is outside {low}-{high}.")
    return number


def _preset_number(params: dict[str, Any]) -> int:
    number = int(params["number"])
    if not 1 <= number <= 256:
        raise ValueError("Presets are numbered 1-256.")
    return number


def _name_value(name: str, what: str) -> str:
    bad = sorted(set(name) & _NAME_FORBIDDEN)
    if bad:
        raise ValueError(f"A {what} cannot contain {' '.join(bad)}.")
    if len(name.encode("utf-8")) > 32:
        raise ValueError(f"A {what} is at most 32 bytes.")
    return quote(name, safe="")


def _text_value(text: str, what: str, safe: str = "") -> str:
    bad = sorted(set(text) & _VALUE_SEPARATORS)
    if bad:
        raise ValueError(
            f"The camera reads {' and '.join(bad)} as a separator, so a {what} "
            f"cannot contain it."
        )
    return quote(text, safe=safe)


def _url_value(url: str, schemes: tuple[str, ...]) -> str:
    if url and not url.lower().startswith(schemes):
        raise ValueError(f"The URL starts with {' or '.join(schemes)}.")
    if len(url.encode("utf-8")) > 512:
        raise ValueError("The URL is at most 512 bytes.")
    return _text_value(url, "URL", safe=":/")


def _one_of(value: str, choices) -> str:
    if value not in choices:
        raise ValueError(f"{value!r} is not one of {', '.join(choices)}.")
    return value


# A CGI setting's value as the camera's word, by the setting's kind.


def _encode_bool(value: Any, _choices: Any) -> str:
    return "on" if value else "off"


def _encode_int(value: Any, choices: tuple[int, int]) -> str:
    low, high = choices
    return str(_bounded(value, low, high))


def _encode_enum(value: Any, choices: dict[str, str]) -> str:
    if str(value) not in choices:
        raise ValueError(f"{value!r} is not one of {', '.join(choices)}.")
    return choices[str(value)]


def _encode_text(value: Any, choices: tuple[int, int]) -> str:
    low, high = choices
    text = str(value or "")
    if not low <= len(text.encode("utf-8")) <= high:
        raise ValueError(f"Up to {high} bytes.")
    return _text_value(text, "name")


_CGI_ENCODERS: dict[str, Callable[[Any, Any], str]] = {
    "bool": _encode_bool,
    "int": _encode_int,
    "enum": _encode_enum,
}


# ── Values the camera reports ──

# An inquiry reply as the state it sets. Each reader takes the whole reply
# (90 50 ... FF) and returns the values to write, nothing for a reply it
# cannot read.
_Reader = Callable[[bytes], dict[str, Any]]


def _sized(length: int, read: _Reader) -> _Reader:
    """Read only a reply of exactly ``length`` bytes."""

    def check(reply: bytes) -> dict[str, Any]:
        return read(reply) if len(reply) == length else {}

    return check


def _flag_reader(key: str, words: tuple[Any, Any] = (True, False)) -> _Reader:
    """On (02) / off (03): ``words`` are the values for on and off."""

    def read(reply: bytes) -> dict[str, Any]:
        code = reply[2]
        if code not in _ON_OFF:
            return {}
        return {key: words[0] if _ON_OFF[code] else words[1]}

    return read


def _code_reader(key: str, names: dict[int, Any] | None = None) -> _Reader:
    """One code: ``names`` maps it to the value (a code not in it writes
    nothing); with no names the code is the value."""

    def read(reply: bytes) -> dict[str, Any]:
        code = reply[2]
        if names is None:
            return {key: code}
        return {key: names[code]} if code in names else {}

    return read


def _position_reader(key: str) -> _Reader:
    """0p 0q 0r 0s: a 16-bit position."""
    return lambda reply: {key: _nibbles(reply[2:6])}


def _level_reader(key: str, convert: Callable[[int], Any] = int) -> _Reader:
    """00 00 0p 0q: an 8-bit level."""
    return lambda reply: {key: convert(_nibbles(reply[4:6]))}


def _read_auto_framing(reply: bytes) -> dict[str, Any]:
    code = reply[2]
    return {"auto_framing": code == 0x01} if code in (0x00, 0x01) else {}


def _read_last_preset(reply: bytes) -> dict[str, Any]:
    # pp is the preset number less one (00-63); 7F means none.
    code = reply[2]
    return {"last_preset": code + 1 if code <= 0x3F else 0}


def _read_pan_tilt(reply: bytes) -> dict[str, Any]:
    return {
        "pan_position": _signed16(_nibbles(reply[2:6])),
        "tilt_position": _signed16(_nibbles(reply[6:10])),
    }


def _read_iris(reply: bytes) -> dict[str, Any]:
    value = _IRIS_NAMES.get(_nibbles(reply[4:6]))
    return {} if value is None else {"iris": value}


def _read_defog(reply: bytes) -> dict[str, Any]:
    code = reply[2]
    if code not in _ON_OFF:
        return {}
    return {"defog": _ON_OFF[code], "defog_level": reply[3]}


def _read_video_format(reply: bytes) -> dict[str, Any]:
    fmt = (reply[2] << 4) | (reply[3] & 0x0F)
    return {"video_format": _VIDEO_FORMATS.get(fmt, f"code {fmt:02X}")}


_READERS: dict[str, _Reader] = {
    "power": _flag_reader("power", ("on", "standby")),
    "auto_framing": _sized(4, _read_auto_framing),
    "tally": _flag_reader("tally"),
    "osd_menu": _flag_reader("osd_menu"),
    "focus_mode": _flag_reader("focus_mode", ("auto", "manual")),
    "last_preset": _sized(4, _read_last_preset),
    "pan_tilt": _sized(11, _read_pan_tilt),
    "zoom_position": _sized(7, _position_reader("zoom_position")),
    "focus_position": _sized(7, _position_reader("focus_position")),
    "iris": _sized(7, _read_iris),
    # GAIN 01 (0 dB) to 11 (48 dB), 3 dB a step.
    "gain": _sized(7, _level_reader("gain_db", lambda n: (n - 1) * 3)),
    # 00 (-10.5 dB) to 07 (0) to 0E (+10.5 dB).
    "exposure_comp_level": _sized(
        7, _level_reader("exposure_comp_level", lambda n: n - 7)
    ),
    "red_gain": _sized(7, _level_reader("red_gain")),
    "blue_gain": _sized(7, _level_reader("blue_gain")),
    "detail_level": _sized(7, _level_reader("detail_level")),
    "shutter": _sized(7, _level_reader("shutter")),
    "wb_speed": _sized(4, _code_reader("wb_speed")),
    "chroma_suppress": _sized(4, _code_reader("chroma_suppress")),
    "noise_reduction": _sized(4, _code_reader("noise_reduction", _NR_NAMES)),
    "defog": _sized(5, _read_defog),
    "video_format": _sized(5, _read_video_format),
}


def _table_readers() -> dict[str, _Reader]:
    """Readers for the on / off and one-code settings of the VISCA tables."""
    readers: dict[str, _Reader] = {}
    for state, _prefix, on, off in _VISCA_SWITCHES.values():
        readers[state] = _code_reader(state, {on: True, off: False})
    for state, _prefix, codes in _VISCA_ENUMS.values():
        names = {code: value for value, code in codes.items()}
        readers[state] = _code_reader(state, names)
    return readers


_READERS.update(_table_readers())

# An inquiry.cgi value as the state it sets, by the parameter's kind.
_SKIP = object()  # a value the state does not take: nothing is written
_UNREAD = object()  # a value that cannot be read: logged once


def _as_bool(raw: str, _choices: Any) -> Any:
    return raw == "on" if raw in ("on", "off") else _SKIP


def _as_flag(raw: str, _choices: Any) -> Any:
    return raw == "1" if raw in ("0", "1") else _SKIP


def _as_int(raw: str, _choices: Any) -> Any:
    try:
        return int(raw)
    except ValueError:
        return _UNREAD


def _as_enum(raw: str, choices: dict[str, str]) -> Any:
    if not raw:
        return "other"
    reverse = {word: value for value, word in choices.items()}
    return reverse.get(raw, "other")


_CGI_DECODERS: dict[str, Callable[[str, Any], Any]] = {
    "bool": _as_bool,
    "flag": _as_flag,
    "int": _as_int,
    "enum": _as_enum,
}

# The picker lists the camera's name lists feed.
_PICKERS = {
    "PresetName": "preset_options",
    "PtzAutoFramingPresetName": "composition_preset_options",
}

# VISCA errors (90 6y ee FF) as the sentence a person reads.
_VISCA_REFUSALS = {
    0x02: "The camera does not support this command or value.",
    0x03: "The camera is busy with two commands already; send it again.",
    0x41: "The camera cannot do this in its current state.",
}

# CGI actions with nothing to set: command -> (path, query, the inquiry
# group read back after it, if any).
_CGI_ACTIONS: dict[str, tuple[str, str, str | None]] = {
    "auto_framing_restart": (
        "analytics/ptzautoframingexe.cgi",
        "PtzAutoFramingRestart=on",
        None,
    ),
    "auto_framing_save_start_position": (
        "analytics/ptzautoframingexe.cgi",
        "PtzAutoFramingDecideStartPosition=on",
        None,
    ),
    "auto_framing_go_to_start_position": (
        "analytics/ptzautoframingexe.cgi",
        "PtzAutoFramingMoveStartPosition=on",
        None,
    ),
    "fixed_angle_save": (
        "analytics/ptzautoframingexe.cgi",
        "PtzAutoFramingFixedAnglePositionSet=1",
        None,
    ),
    "fixed_angle_recall": (
        "analytics/ptzautoframingexe.cgi",
        "PtzAutoFramingFixedAnglePositionCall=1",
        None,
    ),
    "rtmp_start": ("command/rtmpexe.cgi", "RtmpStreaming=on", "stream"),
    "rtmp_stop": ("command/rtmpexe.cgi", "RtmpStreaming=off", "stream"),
    "srt_start": ("command/srtexe.cgi", "SrtStreaming=on", "stream"),
    "srt_stop": ("command/srtexe.cgi", "SrtStreaming=off", "stream"),
    "reboot": ("command/main.cgi", "System=reboot", None),
}

# Zoom and focus drives: command -> (the code at standard speed, the base a
# speed 0-7 is added to). Zoom has no standard-speed form.
_ZOOM_DRIVE = {"zoom_tele": 0x20, "zoom_wide": 0x30}
_FOCUS_DRIVE = {"focus_far": (0x02, 0x20), "focus_near": (0x03, 0x30)}

# Composition presets 1-3 recalled or reset: command -> the parameter.
_COMPOSITION_PRESET_ACTIONS = {
    "composition_preset_recall": "PtzAutoFramingPresetCall",
    "composition_preset_reset": "PtzAutoFramingPresetReset",
}

# Stream destination URLs: command -> (the schemes it takes, path, parameter,
# the inquiry group that reads it back, the state key that holds it).
_STREAM_URLS: dict[str, tuple[tuple[str, ...], str, str, str, str]] = {
    "save_rtmp_url": (
        ("rtmp://", "rtmps://"),
        "command/rtmp.cgi",
        "RtmpServerUrl1",
        "rtmp",
        "rtmp_url",
    ),
    "save_srt_url": (
        ("srt://",),
        "command/srt.cgi",
        "SrtServerUrl1",
        "srt",
        "srt_url",
    ),
}


class SonySRGADriver(BaseDriver):
    """Sony SRG-A40 / SRG-A12 (VISCA over IP + HTTP CGI)."""

    DRIVER_INFO = {
        "id": "sony_srg_a",
        "name": "Sony SRG-A40 / SRG-A12 PTZ Auto Framing Camera",
        "manufacturer": "Sony",
        "category": "camera",
        "version": "1.0.0",
        # Binding the VISCA socket to Reach devices through (local_address)
        # needs 0.37.0; the contract check cannot see a method call.
        "min_platform_version": "0.37.0",
        "author": "OpenAVC",
        "description": "Sony SRG-A40 and SRG-A12 PTZ cameras with PTZ Auto Framing. VISCA over IP "
        "(UDP 52381) for power, pan, tilt, zoom, focus, presets 1-64, exposure, white "
        "balance, picture, tally and auto framing start and stop, every value the "
        "camera reports read back; the web interface (HTTP CGI, Digest login) for "
        "presets 1-256 with names, PTZ Auto Framing's settings, compositions and "
        "status, RTMP, SRT and NDI|HX streaming, reboot and factory reset. The "
        "camera's RTSP, MJPEG and snapshot URLs are offered for preview.",
        "source_url": "https://www.sony.com/electronics/support/res/manuals/5053/f48354480e724f0aef3d45d2e1eeab63/50531551M.pdf",
        "tags": [
            "ptz",
            "camera",
            "sony",
            "visca",
            "visca-ip",
            "auto-framing",
            "auto-tracking",
            "ndi",
            "lecture-capture",
        ],
        "verified": False,
        "simulated": True,
        "protocols": ["visca-ip", "sony-cgi"],
        "ports": [80, 52381],
        "transport": "http",
        "discovery": {
            # The camera's name is its model ("SRG-A40" / "SRG-A12", network.cgi
            # CameraName). No probe: VISCA's CAM_VersionInq answers a vendor and
            # model code the document does not list, and the CGI asks for a
            # login. No manufacturer_alias: sony_visca has it, and two drivers
            # holding the same alias tie, so an SRG-X400 found only as "Sony"
            # would be offered this driver first.
            "hostname": ["^SRG-A(40|12)"],
        },
        "compatible_models": [
            {
                "manufacturer": "Sony",
                "models": ["SRG-A40", "SRG-A12"],
                "confidence": "untested",
                "notes": "Built from Sony's VISCA Command List / CGI Command List for "
                "the SRG-A40 / A12, Software Version 4.0 (June 2026). The "
                "SRG-A12 has optical zoom only (12x). Ball Sports framing, "
                "the Lead Room Effect and the composition presets need camera "
                "software 4.0.",
            }
        ],
        "help": {
            "overview": "Sony SRG-A40 / SRG-A12 PTZ cameras with AI auto framing. Power, pan, "
            "tilt, zoom, focus, presets 1-64, exposure, white balance, picture, "
            "tally and auto framing start and stop use VISCA over IP; presets 65-256 "
            "and preset names, the auto framing settings, compositions and status, "
            "streaming, reboot and factory reset use the camera's web interface.",
            "setup": "1. Give the camera a fixed IP address (RM-IP Setup Tool or the web page's "
            "Network menu).\n"
            "2. Turn on CAMERA SETUP switch 3 (VISCA) on the back of the camera, then "
            "power it off and on: without it the camera does not answer VISCA over IP "
            "(UDP 52381).\n"
            "3. Enter the camera's web user name (admin unless you added another) and "
            "password. The camera asks for a password the first time anyone signs in to "
            "its web page. Settings, power, streaming, Reboot and Factory Reset need an "
            "Administrator account; a Full user can run presets and auto framing. "
            "Without a login, VISCA control still works.\n"
            "4. The camera counts wrong passwords and blocks the address after 8 (Brute "
            "Force Attack Protection). A refused password is not tried again: correct "
            "it here.\n"
            "5. If the camera answers only HTTPS, turn on HTTPS here and set the HTTP "
            "Port to 443.",
        },
        "default_config": {
            "host": "",
            "port": 80,
            "ssl": False,
            "verify_ssl": False,
            "username": "",
            "password": "",
            "rtsp_port": 554,
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
            "username": {
                "type": "string",
                "default": "",
                "label": "Web Username",
                "description": "The camera's web user name, usually admin.",
            },
            "password": {
                "type": "string",
                "default": "",
                "label": "Web Password",
                "secret": True,
            },
            "rtsp_port": {
                "type": "integer",
                "default": 554,
                "min": 1,
                "max": 65535,
                "label": "RTSP Port",
                "description": "For the preview URL; change it only if you "
                "changed it on the camera.",
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
                "max": 23,
                "label": "Tilt Speed (1-23)",
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
            "auto_framing": {
                "type": "boolean",
                "label": "PTZ Auto Framing",
                "control": True,
            },
            "auto_framing_status": {
                "type": "enum",
                "values": [
                    "idle",
                    "missing",
                    "preparing",
                    "searching",
                    "tracking",
                    "waiting",
                    "fixed_angle",
                    "other",
                ],
                "label": "Auto Framing Status",
                "help": "idle: not framing; missing: the subject "
                "is lost; preparing; searching for a "
                "subject; tracking; waiting for a subject "
                "to be chosen; fixed_angle: holding the "
                "Fixed Angle Position. Read through the "
                "web interface.",
            },
            "auto_framing_targets": {
                "type": "integer",
                "label": "Subjects Being Framed",
                "min": 0,
                "max": 8,
            },
            "pan_position": {
                "type": "integer",
                "label": "Pan Position",
                "min": -8704,
                "max": 8704,
                "cloud_priority": "low",
                "help": "-8704 (170 degrees left) to 8704 (170 degrees "
                "right); 0 is home.",
            },
            "tilt_position": {
                "type": "integer",
                "label": "Tilt Position",
                "min": -4608,
                "max": 4608,
                "cloud_priority": "low",
                "help": "-1024 (20 degrees down) to 4608 (90 degrees "
                "up) with Image Flip off; -4608 to 1024 with it "
                "on.",
            },
            "zoom_position": {
                "type": "integer",
                "label": "Zoom Position",
                "min": 0,
                "max": 31424,
                "cloud_priority": "low",
                "help": "0 (wide) to 16384 (optical tele end: 20x on "
                "the SRG-A40, 12x on the SRG-A12); to 31424 "
                "with digital zoom on the SRG-A40.",
            },
            "focus_position": {
                "type": "integer",
                "label": "Focus Position",
                "min": 4096,
                "max": 61440,
                "cloud_priority": "low",
                "help": "4096 (infinity) to 61440 (8 cm).",
            },
            "focus_mode": {
                "type": "enum",
                "values": ["auto", "manual"],
                "label": "Focus Mode",
            },
            "last_preset": {
                "type": "integer",
                "label": "Last Preset Recalled",
                "min": 0,
                "max": 64,
                "help": "The last of presets 1-64 recalled; 0 when none "
                "has been. Presets above 64 are not reported.",
            },
            "tally": {"type": "boolean", "label": "Tally Lamp", "control": True},
            "osd_menu": {"type": "boolean", "label": "On-Screen Menu Open"},
            "iris": {
                "type": "enum",
                "values": [
                    "F2.0",
                    "F2.2",
                    "F2.4",
                    "F2.6",
                    "F2.8",
                    "F3.1",
                    "F3.4",
                    "F3.7",
                    "F4.0",
                    "F4.4",
                    "F4.8",
                    "F5.2",
                    "F5.6",
                    "F6.2",
                    "F6.8",
                    "F7.3",
                    "F8.0",
                    "F8.7",
                    "F9.6",
                    "F10",
                    "F11",
                    "close",
                ],
                "label": "Iris",
            },
            "gain_db": {
                "type": "integer",
                "label": "Gain",
                "min": 0,
                "max": 48,
                "step": 3,
                "unit": "dB",
            },
            "exposure_comp_level": {
                "type": "integer",
                "label": "Exposure Compensation Level",
                "min": -7,
                "max": 7,
                "help": "-7 (-10.5 dB) to 7 (+10.5 dB), 1.5 dB a step.",
            },
            "noise_reduction": {
                "type": "enum",
                "values": ["off", "1", "2", "3", "4", "5", "2d_3d"],
                "label": "Noise Reduction",
                "help": "off, 1 (weak) to 5 (strong), or 2d_3d when "
                "the 2D and 3D levels are set separately on "
                "the camera.",
            },
            "defog": {"type": "boolean", "label": "Defog"},
            "defog_level": {
                "type": "integer",
                "label": "Defog Level",
                "min": 0,
                "max": 3,
            },
            "video_format": {"type": "string", "label": "Video Output Format"},
            "backlight": {"type": "boolean", "label": "Backlight Compensation"},
            "spotlight": {"type": "boolean", "label": "Spotlight Compensation"},
            "high_sensitivity": {"type": "boolean", "label": "High Sensitivity"},
            "exposure_comp": {"type": "boolean", "label": "Exposure Compensation"},
            "auto_slow_shutter": {"type": "boolean", "label": "Auto Slow Shutter"},
            "visibility_enhancer": {"type": "boolean", "label": "Visibility Enhancer"},
            "low_light": {"type": "boolean", "label": "Low Light Basis Brightness"},
            "flicker_cancel": {"type": "boolean", "label": "Flicker Cancel"},
            "image_stabilizer": {"type": "boolean", "label": "Image Stabilizer"},
            "high_resolution": {"type": "boolean", "label": "High Resolution"},
            "tele_convert": {"type": "boolean", "label": "Tele Convert"},
            "image_flip": {"type": "boolean", "label": "Image Flip"},
            "ir_receive": {"type": "boolean", "label": "IR Remote Receive"},
            "pan_tilt_slow": {"type": "boolean", "label": "Pan/Tilt Slow"},
            "exposure_mode": {
                "type": "enum",
                "values": ["full_auto", "manual", "shutter_priority", "iris_priority"],
                "label": "Exposure Mode",
            },
            "wb_mode": {
                "type": "enum",
                "values": ["auto1", "indoor", "outdoor", "one_push", "auto2", "manual"],
                "label": "White Balance Mode",
            },
            "picture_effect": {
                "type": "enum",
                "values": ["off", "black_white"],
                "label": "Picture Effect",
            },
            "zoom_mode": {
                "type": "enum",
                "values": ["optical", "clear_image", "digital"],
                "label": "Zoom Mode",
            },
            "af_mode": {
                "type": "enum",
                "values": ["normal", "interval", "zoom_trigger"],
                "label": "Autofocus Mode",
            },
            "af_sensitivity": {
                "type": "enum",
                "values": ["normal", "low"],
                "label": "Autofocus Sensitivity",
            },
            "ir_correction": {
                "type": "enum",
                "values": ["standard", "ir_light"],
                "label": "IR Correction",
            },
            "standby_mode": {
                "type": "enum",
                "values": ["side", "neutral"],
                "label": "Standby Position",
            },
            "preset_call_mode": {
                "type": "enum",
                "values": ["freeze", "normal"],
                "label": "Preset Call Mode",
            },
            "hdmi_color": {
                "type": "enum",
                "values": ["ycbcr", "rgb"],
                "label": "HDMI Color Space",
            },
            "wb_speed": {
                "type": "integer",
                "label": "White Balance Speed",
                "min": 1,
                "max": 5,
            },
            "chroma_suppress": {
                "type": "integer",
                "label": "Chroma Suppress",
                "min": 0,
                "max": 3,
            },
            "red_gain": {
                "type": "integer",
                "label": "Red Gain",
                "min": 0,
                "max": 255,
                "help": "Red in manual white balance, 0-255 (128 is the middle).",
            },
            "blue_gain": {
                "type": "integer",
                "label": "Blue Gain",
                "min": 0,
                "max": 255,
            },
            "detail_level": {
                "type": "integer",
                "label": "Detail Level",
                "min": 0,
                "max": 15,
            },
            "shutter": {"type": "integer", "label": "Shutter", "min": 6, "max": 33},
            "framing_mode": {
                "type": "enum",
                "values": ["person", "ball_sports", "other"],
                "label": "Framing Mode",
                "control": True,
            },
            "auto_framing_trigger": {
                "type": "enum",
                "values": ["auto", "manual", "other"],
                "label": "Subject Selection",
            },
            "auto_framing_auto_start": {
                "type": "boolean",
                "label": "Start Auto Framing at Power On",
            },
            "multi_tracking": {"type": "boolean", "label": "Multiple Subjects"},
            "multi_tracking_targets": {
                "type": "integer",
                "label": "Number of Subjects",
                "min": 2,
                "max": 8,
            },
            "multi_tracking_wait": {
                "type": "integer",
                "label": "Wait for Subjects (s)",
                "min": 0,
                "max": 60,
            },
            "lead_room": {
                "type": "enum",
                "values": ["off", "low", "middle", "high", "other"],
                "label": "Lead Room",
            },
            "face_indicator": {"type": "boolean", "label": "Face Indicator on Stream"},
            "individual_identification": {
                "type": "boolean",
                "label": "Personal Identification",
            },
            "registered_face_tracking": {
                "type": "boolean",
                "label": "Prefer Registered Faces",
            },
            "lost_wait_time": {
                "type": "integer",
                "label": "Lost Subject Wait (s)",
                "min": 1,
                "max": 60,
            },
            "face_lost_wait_time": {
                "type": "integer",
                "label": "Face Timeout (s)",
                "min": 0,
                "max": 60,
            },
            "detection_restart_time": {
                "type": "integer",
                "label": "Restart Search After (s)",
                "min": 0,
                "max": 600,
            },
            "tally_level": {
                "type": "enum",
                "values": ["off", "low", "high", "other"],
                "label": "Tally Brightness",
            },
            "tally_follows_auto_framing": {
                "type": "boolean",
                "label": "Tally Shows Auto Framing",
            },
            "stream_mode": {
                "type": "enum",
                "values": [
                    "rtsp",
                    "rtmp",
                    "srt_caller",
                    "srt_listener",
                    "ndi_hx",
                    "off",
                    "other",
                ],
                "label": "Streaming Mode",
                "control": True,
            },
            "ndi_source_name": {"type": "string", "label": "NDI Source Name"},
            "composition_vertical": {
                "type": "integer",
                "label": "Composition Vertical",
                "min": -50,
                "max": 70,
            },
            "composition_horizontal": {
                "type": "integer",
                "label": "Composition Horizontal",
                "min": -50,
                "max": 50,
            },
            "composition_size": {
                "type": "integer",
                "label": "Composition Size",
                "min": 160,
                "max": 3500,
            },
            "preset_options": {
                "type": "string",
                "label": "Preset List",
                "help": "The presets saved on the camera, for the "
                "preset pickers (read through the web "
                "interface).",
            },
            "composition_preset_options": {
                "type": "string",
                "label": "Composition Preset List",
            },
            "stream_status": {
                "type": "enum",
                "values": [
                    "invalid",
                    "off",
                    "streaming",
                    "ready",
                    "ready_ssl",
                    "streaming_ssl",
                    "other",
                ],
                "label": "Stream Status",
                "help": "off: an SRT caller stream is not connected; "
                "streaming; ready / ready_ssl: the RTMP / RTMPS "
                "server was reached; invalid: no status.",
            },
            "rtmp_url": {"type": "string", "label": "RTMP Server URL"},
            "rtmp_key_set": {"type": "boolean", "label": "RTMP Stream Key Saved"},
            "srt_url": {"type": "string", "label": "SRT Server URL"},
            "temperature_warning": {"type": "boolean", "label": "Temperature Warning"},
            "model": {"type": "string", "label": "Model"},
            "serial_number": {"type": "string", "label": "Serial Number"},
            "software_version": {"type": "string", "label": "Software Version"},
            "preview_url": {"type": "string", "label": "Stream URL"},
            "preview_format": {"type": "string", "label": "Stream Format"},
            "mjpeg_url": {"type": "string", "label": "MJPEG URL"},
            "snapshot_url": {"type": "string", "label": "Snapshot URL"},
            "last_error": {"type": "string", "label": "Last Error"},
        },
        "device_settings": {
            "focus_mode": {
                "type": "enum",
                "label": "Focus Mode",
                "help": "Autofocus, or focus held where it is set.",
                "values": ["auto", "manual"],
                "default": "auto",
                "state_key": "focus_mode",
                "setup": False,
            },
            "iris": {
                "type": "enum",
                "label": "Iris",
                "help": "The aperture in manual or iris priority exposure.",
                "values": [
                    "F2.0",
                    "F2.2",
                    "F2.4",
                    "F2.6",
                    "F2.8",
                    "F3.1",
                    "F3.4",
                    "F3.7",
                    "F4.0",
                    "F4.4",
                    "F4.8",
                    "F5.2",
                    "F5.6",
                    "F6.2",
                    "F6.8",
                    "F7.3",
                    "F8.0",
                    "F8.7",
                    "F9.6",
                    "F10",
                    "F11",
                    "close",
                ],
                "default": "F2.0",
                "state_key": "iris",
                "setup": False,
            },
            "gain_db": {
                "type": "integer",
                "label": "Gain (dB)",
                "help": "Gain in manual exposure, 0-48 dB in 3 dB steps "
                "(above 36 dB with High Sensitivity on).",
                "min": 0,
                "max": 48,
                "default": 0,
                "state_key": "gain_db",
                "setup": False,
            },
            "exposure_comp_level": {
                "type": "integer",
                "label": "Exposure Compensation Level",
                "help": "-7 (-10.5 dB) to 7 (+10.5 dB), 1.5 dB a "
                "step, with Exposure Compensation on.",
                "min": -7,
                "max": 7,
                "default": 0,
                "state_key": "exposure_comp_level",
                "setup": False,
            },
            "noise_reduction": {
                "type": "enum",
                "label": "Noise Reduction",
                "help": "off, or 1 (weak) to 5 (strong).",
                "values": ["off", "1", "2", "3", "4", "5"],
                "default": "off",
                "state_key": "noise_reduction",
                "setup": False,
            },
            "backlight": {
                "type": "boolean",
                "label": "Backlight Compensation",
                "help": "Brightens a subject in front of a bright background.",
                "default": False,
                "state_key": "backlight",
                "setup": False,
            },
            "spotlight": {
                "type": "boolean",
                "label": "Spotlight Compensation",
                "help": "Holds exposure down for a brightly lit subject on "
                "a dark stage.",
                "default": False,
                "state_key": "spotlight",
                "setup": False,
            },
            "high_sensitivity": {
                "type": "boolean",
                "label": "High Sensitivity",
                "help": "Raises the gain ceiling for dim rooms (gain up to 48 dB).",
                "default": False,
                "state_key": "high_sensitivity",
                "setup": False,
            },
            "exposure_comp": {
                "type": "boolean",
                "label": "Exposure Compensation",
                "help": "Turns the exposure compensation level on or off.",
                "default": False,
                "state_key": "exposure_comp",
                "setup": False,
            },
            "auto_slow_shutter": {
                "type": "boolean",
                "label": "Auto Slow Shutter",
                "help": "Lets the shutter slow down automatically in low light.",
                "default": False,
                "state_key": "auto_slow_shutter",
                "setup": False,
            },
            "visibility_enhancer": {
                "type": "boolean",
                "label": "Visibility Enhancer",
                "help": "Evens out dark and bright areas of the picture.",
                "default": False,
                "state_key": "visibility_enhancer",
                "setup": False,
            },
            "low_light": {
                "type": "boolean",
                "label": "Low Light Basis Brightness",
                "help": "Keeps the basic brightness up in low light.",
                "default": False,
                "state_key": "low_light",
                "setup": False,
            },
            "flicker_cancel": {
                "type": "boolean",
                "label": "Flicker Cancel",
                "help": "Reduces flicker from lighting.",
                "default": False,
                "state_key": "flicker_cancel",
                "setup": False,
            },
            "image_stabilizer": {
                "type": "boolean",
                "label": "Image Stabilizer",
                "help": "Reduces shake in the picture.",
                "default": False,
                "state_key": "image_stabilizer",
                "setup": False,
            },
            "high_resolution": {
                "type": "boolean",
                "label": "High Resolution",
                "help": "Sharpens edges for a more detailed picture.",
                "default": False,
                "state_key": "high_resolution",
                "setup": False,
            },
            "tele_convert": {
                "type": "boolean",
                "label": "Tele Convert",
                "help": "Doubles the zoom by cropping. On only at 1920x1080 formats.",
                "default": False,
                "state_key": "tele_convert",
                "setup": False,
            },
            "image_flip": {
                "type": "boolean",
                "label": "Image Flip",
                "help": "Turns the picture upside down for a ceiling-mounted camera.",
                "default": False,
                "state_key": "image_flip",
                "setup": False,
            },
            "ir_receive": {
                "type": "boolean",
                "label": "IR Remote Receive",
                "help": "Whether the camera takes commands from its infrared remote.",
                "default": False,
                "state_key": "ir_receive",
                "setup": False,
            },
            "pan_tilt_slow": {
                "type": "boolean",
                "label": "Pan/Tilt Slow",
                "help": "Slows every pan and tilt speed for smooth on-air moves.",
                "default": False,
                "state_key": "pan_tilt_slow",
                "setup": False,
            },
            "exposure_mode": {
                "type": "enum",
                "label": "Exposure Mode",
                "help": "How the camera sets exposure: fully automatic, "
                "manual, or with the shutter or iris held.",
                "values": ["full_auto", "manual", "shutter_priority", "iris_priority"],
                "default": "full_auto",
                "state_key": "exposure_mode",
                "setup": False,
            },
            "wb_mode": {
                "type": "enum",
                "label": "White Balance Mode",
                "help": "auto1 and auto2 follow the scene; indoor and outdoor "
                "are fixed; one_push holds the last One Push White "
                "Balance; manual uses the red and blue gains.",
                "values": ["auto1", "indoor", "outdoor", "one_push", "auto2", "manual"],
                "default": "auto1",
                "state_key": "wb_mode",
                "setup": False,
            },
            "picture_effect": {
                "type": "enum",
                "label": "Picture Effect",
                "help": "Off, or black and white.",
                "values": ["off", "black_white"],
                "default": "off",
                "state_key": "picture_effect",
                "setup": False,
            },
            "zoom_mode": {
                "type": "enum",
                "label": "Zoom Mode",
                "help": "Optical only, Clear Image Zoom (up to 1.5x more at "
                "4K, 2x otherwise) or digital zoom to 240x. The "
                "SRG-A12 has optical only. Changing it means "
                "setting up PTZ Auto Framing's start position, "
                "detection settings and tracking range again.",
                "values": ["optical", "clear_image", "digital"],
                "default": "optical",
                "state_key": "zoom_mode",
                "setup": False,
            },
            "af_mode": {
                "type": "enum",
                "label": "Autofocus Mode",
                "help": "Normal (always), Interval (at set times) or Zoom "
                "Trigger (after each zoom).",
                "values": ["normal", "interval", "zoom_trigger"],
                "default": "normal",
                "state_key": "af_mode",
                "setup": False,
            },
            "af_sensitivity": {
                "type": "enum",
                "label": "Autofocus Sensitivity",
                "help": "Normal, or Low for a steadier focus.",
                "values": ["normal", "low"],
                "default": "normal",
                "state_key": "af_sensitivity",
                "setup": False,
            },
            "ir_correction": {
                "type": "enum",
                "label": "IR Correction",
                "help": "Focus correction under infrared light.",
                "values": ["standard", "ir_light"],
                "default": "standard",
                "state_key": "ir_correction",
                "setup": False,
            },
            "standby_mode": {
                "type": "enum",
                "label": "Standby Position",
                "help": "Neutral leaves the camera where it is in "
                "standby; Side turns it to the end of its pan "
                "range.",
                "values": ["side", "neutral"],
                "default": "neutral",
                "state_key": "standby_mode",
                "setup": False,
            },
            "preset_call_mode": {
                "type": "enum",
                "label": "Preset Call Mode",
                "help": "Freeze holds the picture still while the "
                "camera moves to a preset.",
                "values": ["freeze", "normal"],
                "default": "normal",
                "state_key": "preset_call_mode",
                "setup": False,
            },
            "hdmi_color": {
                "type": "enum",
                "label": "HDMI Color Space",
                "help": "YCbCr or RGB on the HDMI output.",
                "values": ["ycbcr", "rgb"],
                "default": "ycbcr",
                "state_key": "hdmi_color",
                "setup": False,
            },
            "wb_speed": {
                "type": "integer",
                "label": "White Balance Speed",
                "help": "How quickly automatic white balance follows a "
                "change, 1 (slow) to 5 (fast).",
                "min": 1,
                "max": 5,
                "default": 3,
                "state_key": "wb_speed",
                "setup": False,
            },
            "chroma_suppress": {
                "type": "integer",
                "label": "Chroma Suppress",
                "help": "Reduces color noise in low light, 0 (off) to 3 (strong).",
                "min": 0,
                "max": 3,
                "default": 0,
                "state_key": "chroma_suppress",
                "setup": False,
            },
            "red_gain": {
                "type": "integer",
                "label": "Red Gain",
                "help": "Red in manual white balance, 0-255 (128 is the middle).",
                "min": 0,
                "max": 255,
                "default": 128,
                "state_key": "red_gain",
                "setup": False,
            },
            "blue_gain": {
                "type": "integer",
                "label": "Blue Gain",
                "help": "Blue in manual white balance, 0-255 (128 is the middle).",
                "min": 0,
                "max": 255,
                "default": 128,
                "state_key": "blue_gain",
                "setup": False,
            },
            "detail_level": {
                "type": "integer",
                "label": "Detail Level",
                "help": "Edge sharpening, 0-15 (7 is the factory level).",
                "min": 0,
                "max": 15,
                "default": 7,
                "state_key": "detail_level",
                "setup": False,
            },
            "shutter": {
                "type": "integer",
                "label": "Shutter",
                "help": "Shutter speed code, 6 (1/1 s) to 33 (1/10000 s), in "
                "manual or shutter priority. The speed each code "
                "gives depends on the video format: Sony's table "
                "lists them (18 is 1/60 at 1080/59.94p).",
                "min": 6,
                "max": 33,
                "default": 18,
                "state_key": "shutter",
                "setup": False,
            },
            "framing_mode": {
                "type": "enum",
                "label": "Framing Mode",
                "help": "Person, or Ball Sports (indoor 5v5 basketball, "
                "firmware 4.0). Needs the web login.",
                "values": ["person", "ball_sports"],
                "default": "person",
                "state_key": "framing_mode",
                "setup": False,
            },
            "auto_framing_trigger": {
                "type": "enum",
                "label": "Subject Selection",
                "help": "auto picks the subject itself; manual "
                "waits for one to be chosen (Track "
                "Subject At). Needs the web login.",
                "values": ["auto", "manual"],
                "default": "auto",
                "state_key": "auto_framing_trigger",
                "setup": False,
            },
            "auto_framing_auto_start": {
                "type": "boolean",
                "label": "Start Auto Framing at Power On",
                "help": "Starts PTZ Auto Framing whenever the "
                "camera powers on. Needs the web "
                "login.",
                "default": False,
                "state_key": "auto_framing_auto_start",
                "setup": False,
            },
            "multi_tracking": {
                "type": "boolean",
                "label": "Multiple Subjects",
                "help": "Frames several people together instead of "
                "one. Needs the web login.",
                "default": False,
                "state_key": "multi_tracking",
                "setup": False,
            },
            "multi_tracking_targets": {
                "type": "integer",
                "label": "Number of Subjects",
                "help": "How many people to frame together, 2-8. Needs the web login.",
                "min": 2,
                "max": 8,
                "default": 2,
                "state_key": "multi_tracking_targets",
                "setup": False,
            },
            "multi_tracking_wait": {
                "type": "integer",
                "label": "Wait for Subjects (s)",
                "help": "How long to wait for that many people "
                "before framing, 1-60 s; 0 waits until "
                "they are all there. Needs the web login.",
                "min": 0,
                "max": 60,
                "default": 0,
                "state_key": "multi_tracking_wait",
                "setup": False,
            },
            "lead_room": {
                "type": "enum",
                "label": "Lead Room",
                "help": "Leaves room in front of a moving subject. Needs "
                "the web login.",
                "values": ["off", "low", "middle", "high"],
                "default": "off",
                "state_key": "lead_room",
                "setup": False,
            },
            "face_indicator": {
                "type": "boolean",
                "label": "Face Indicator on Stream",
                "help": "Draws the face indicator on the streamed "
                "video. Needs the web login.",
                "default": False,
                "state_key": "face_indicator",
                "setup": False,
            },
            "individual_identification": {
                "type": "boolean",
                "label": "Personal Identification",
                "help": "Recognizes the person being framed "
                "so the camera stays on them. Needs "
                "the web login.",
                "default": False,
                "state_key": "individual_identification",
                "setup": False,
            },
            "registered_face_tracking": {
                "type": "boolean",
                "label": "Prefer Registered Faces",
                "help": "Frames faces registered on the "
                "camera first. Needs the web login.",
                "default": False,
                "state_key": "registered_face_tracking",
                "setup": False,
            },
            "lost_wait_time": {
                "type": "integer",
                "label": "Lost Subject Wait (s)",
                "help": "How long to wait for a lost subject, 1-60 s. "
                "Needs the web login.",
                "min": 1,
                "max": 60,
                "default": 6,
                "state_key": "lost_wait_time",
                "setup": False,
            },
            "face_lost_wait_time": {
                "type": "integer",
                "label": "Face Timeout (s)",
                "help": "How long framing continues without "
                "seeing a face, 1-60 s; 0 never times "
                "out. Needs the web login.",
                "min": 0,
                "max": 60,
                "default": 0,
                "state_key": "face_lost_wait_time",
                "setup": False,
            },
            "detection_restart_time": {
                "type": "integer",
                "label": "Restart Search After (s)",
                "help": "Starts searching again after this "
                "long, 1-600 s; 0 never. Needs the web "
                "login.",
                "min": 0,
                "max": 600,
                "default": 0,
                "state_key": "detection_restart_time",
                "setup": False,
            },
            "tally_level": {
                "type": "enum",
                "label": "Tally Brightness",
                "help": "Off (the lamp never lights), low or high. Needs "
                "the web login.",
                "values": ["off", "low", "high"],
                "default": "high",
                "state_key": "tally_level",
                "setup": False,
            },
            "tally_follows_auto_framing": {
                "type": "boolean",
                "label": "Tally Shows Auto Framing",
                "help": "The green tally lamp shows PTZ "
                "Auto Framing's status: lit while "
                "framing, flashing when the "
                "subject is lost. Needs the web "
                "login.",
                "default": False,
                "state_key": "tally_follows_auto_framing",
                "setup": False,
            },
            "stream_mode": {
                "type": "enum",
                "label": "Streaming Mode",
                "help": "Which stream the camera sends: RTSP, RTMP, SRT "
                "(caller or listener), NDI|HX or none. Changing "
                "it interrupts streaming. Needs the web login.",
                "values": [
                    "rtsp",
                    "rtmp",
                    "srt_caller",
                    "srt_listener",
                    "ndi_hx",
                    "off",
                ],
                "default": "rtsp",
                "state_key": "stream_mode",
                "setup": False,
            },
            "ndi_source_name": {
                "type": "string",
                "label": "NDI Source Name",
                "help": "The name the camera's NDI|HX stream appears "
                "under (up to 64 bytes). Needs the web login.",
                "default": "",
                "state_key": "ndi_source_name",
                "setup": False,
            },
            "rtmp_url": {
                "type": "string",
                "label": "RTMP Server URL",
                "help": "rtmp:// or rtmps://, up to 512 bytes. Blank clears "
                "it. The camera reads & and , as separators, so a "
                "URL holding either cannot be saved. Needs the web "
                "login.",
                "default": "",
                "state_key": "rtmp_url",
                "setup": False,
            },
            "srt_url": {
                "type": "string",
                "label": "SRT Server URL",
                "help": "srt://, for the SRT caller mode, up to 512 bytes. "
                "Blank clears it. Needs the web login.",
                "default": "",
                "state_key": "srt_url",
                "setup": False,
            },
        },
        "quick_actions": [
            "auto_framing_start",
            "auto_framing_stop",
            "pt_home",
            "power_on",
            "power_off",
        ],
        "commands": {
            "power_on": {
                "label": "Power On",
                "params": {},
                "help": "Wakes the camera from standby. It moves to preset 1 as it "
                "starts. Sent at least 10 s after Power Off (Standby), as "
                "Sony asks.",
                "sets": {"power": "on"},
            },
            "power_off": {
                "label": "Power Off (Standby)",
                "params": {},
                "help": "Puts the camera in standby. VISCA over IP and the web "
                "interface keep answering, so Power On works.",
                "sets": {"power": "standby"},
            },
            "auto_framing_start": {
                "label": "Start Auto Framing",
                "params": {},
                "help": "Starts PTZ Auto Framing: the camera finds and "
                "follows the subject.",
                "sets": {"auto_framing": True},
            },
            "auto_framing_stop": {
                "label": "Stop Auto Framing",
                "params": {},
                "help": "Stops PTZ Auto Framing; the camera stays where it is.",
                "sets": {"auto_framing": False},
            },
            "auto_framing_restart": {
                "label": "Restart Subject Search",
                "params": {},
                "help": "Makes PTZ Auto Framing search for a subject "
                "again. Needs the web login.",
            },
            "auto_framing_save_start_position": {
                "label": "Save Auto Framing Start Position",
                "params": {},
                "help": "Saves where the camera is now as "
                "the position PTZ Auto Framing "
                "starts from. Needs the web login.",
            },
            "auto_framing_go_to_start_position": {
                "label": "Go to Auto Framing Start Position",
                "params": {},
                "help": "Moves the camera to the PTZ Auto "
                "Framing start position. Needs the "
                "web login.",
            },
            "fixed_angle_save": {
                "label": "Save Fixed Angle Position",
                "params": {},
                "help": "Saves where the camera is now as the Fixed Angle "
                "Position, the shot it holds while a subject is in "
                "the predefined area. Needs the web login.",
            },
            "fixed_angle_recall": {
                "label": "Go to Fixed Angle Position",
                "params": {},
                "help": "Moves the camera to the Fixed Angle Position. "
                "Needs the web login.",
            },
            "track_subject_at": {
                "label": "Track Subject At",
                "params": {
                    "x": {
                        "type": "integer",
                        "required": True,
                        "label": "Across",
                        "min": 0,
                        "max": 10000,
                        "default": 5000,
                    },
                    "y": {
                        "type": "integer",
                        "required": True,
                        "label": "Down",
                        "min": 0,
                        "max": 10000,
                        "default": 5000,
                    },
                },
                "help": "Frames the person nearest a point on the picture "
                "(0,0 top left to 10000,10000 bottom right). Needs "
                "the web login.",
            },
            "set_composition": {
                "label": "Set Composition",
                "params": {
                    "vertical": {
                        "type": "integer",
                        "required": True,
                        "label": "Vertical",
                        "min": -50,
                        "max": 70,
                        "default": 0,
                    },
                    "horizontal": {
                        "type": "integer",
                        "required": True,
                        "label": "Horizontal",
                        "min": -50,
                        "max": 50,
                        "default": 0,
                    },
                    "size": {
                        "type": "integer",
                        "required": True,
                        "label": "Size",
                        "min": 160,
                        "max": 3500,
                        "default": 1200,
                    },
                },
                "help": "Where the subject sits in the shot while framing: "
                "vertical -50 to 70, horizontal -50 to 50, size 160 "
                "to 3500. Needs the web login.",
            },
            "composition_preset_recall": {
                "label": "Recall Composition Preset",
                "params": {
                    "number": {
                        "type": "integer",
                        "required": True,
                        "label": "Composition Preset",
                        "min": 1,
                        "max": 3,
                        "options_state": "composition_preset_options",
                    }
                },
                "help": "Recalls a saved framing composition "
                "(1-3), such as Full Body, Waist or "
                "Closeup. Needs the web login.",
            },
            "composition_preset_save": {
                "label": "Save Composition Preset",
                "params": {
                    "number": {
                        "type": "integer",
                        "required": True,
                        "label": "Composition Preset",
                        "min": 1,
                        "max": 3,
                        "options_state": "composition_preset_options",
                    },
                    "name": {"type": "string", "label": "Name", "default": ""},
                },
                "help": "Saves the current framing composition as "
                "preset 1-3 with a name (up to 32 bytes, no "
                '" % & , : ~). Needs the web login.',
            },
            "composition_preset_reset": {
                "label": "Reset Composition Preset",
                "params": {
                    "number": {
                        "type": "integer",
                        "required": True,
                        "label": "Composition Preset",
                        "min": 1,
                        "max": 3,
                        "options_state": "composition_preset_options",
                    }
                },
                "help": "Returns a composition preset to its "
                "factory composition. Needs the web login.",
                "confirm": "Returns this composition preset to its "
                "factory composition.",
            },
            "pt_up": {
                "label": "Tilt Up",
                "params": {},
                "help": "Moves the camera (tilt up) at the Pan Speed and Tilt Speed "
                "set on the device, until Pan/Tilt Stop.",
            },
            "pt_down": {
                "label": "Tilt Down",
                "params": {},
                "help": "Moves the camera (tilt down) at the Pan Speed and Tilt "
                "Speed set on the device, until Pan/Tilt Stop.",
            },
            "pt_left": {
                "label": "Pan Left",
                "params": {},
                "help": "Moves the camera (pan left) at the Pan Speed and Tilt Speed "
                "set on the device, until Pan/Tilt Stop.",
            },
            "pt_right": {
                "label": "Pan Right",
                "params": {},
                "help": "Moves the camera (pan right) at the Pan Speed and Tilt "
                "Speed set on the device, until Pan/Tilt Stop.",
            },
            "pt_up_left": {
                "label": "Pan Up-Left",
                "params": {},
                "help": "Moves the camera (pan up-left) at the Pan Speed and Tilt "
                "Speed set on the device, until Pan/Tilt Stop.",
            },
            "pt_up_right": {
                "label": "Pan Up-Right",
                "params": {},
                "help": "Moves the camera (pan up-right) at the Pan Speed and "
                "Tilt Speed set on the device, until Pan/Tilt Stop.",
            },
            "pt_down_left": {
                "label": "Pan Down-Left",
                "params": {},
                "help": "Moves the camera (pan down-left) at the Pan Speed and "
                "Tilt Speed set on the device, until Pan/Tilt Stop.",
            },
            "pt_down_right": {
                "label": "Pan Down-Right",
                "params": {},
                "help": "Moves the camera (pan down-right) at the Pan Speed "
                "and Tilt Speed set on the device, until Pan/Tilt "
                "Stop.",
            },
            "pt_stop": {
                "label": "Pan/Tilt Stop",
                "params": {},
                "help": "Stops pan/tilt movement.",
            },
            "pt_home": {
                "label": "Pan/Tilt Home",
                "params": {},
                "help": "Moves the camera to its home position (pan and tilt 0).",
            },
            "pt_reset": {
                "label": "Pan/Tilt Reset",
                "params": {},
                "help": "Runs the pan/tilt initialization: the camera sweeps its "
                "range and returns.",
            },
            "pt_absolute": {
                "label": "Pan/Tilt to Position",
                "params": {
                    "pan": {
                        "type": "integer",
                        "required": True,
                        "label": "Pan",
                        "min": -8704,
                        "max": 8704,
                        "default": 0,
                    },
                    "tilt": {
                        "type": "integer",
                        "required": True,
                        "label": "Tilt",
                        "min": -4608,
                        "max": 4608,
                        "default": 0,
                    },
                    "speed": {"type": "integer", "label": "Speed", "min": 1, "max": 24},
                },
                "help": "Moves to a pan and tilt position (see Pan Position and "
                "Tilt Position for the ranges).",
            },
            "zoom_tele": {
                "label": "Zoom In",
                "params": {
                    "speed": {"type": "integer", "label": "Speed", "min": 0, "max": 7}
                },
                "help": "Zooms in until Zoom Stop, at the speed given (0 slow to 7 "
                "fast) or the Zoom Speed set on the device.",
            },
            "zoom_wide": {
                "label": "Zoom Out",
                "params": {
                    "speed": {"type": "integer", "label": "Speed", "min": 0, "max": 7}
                },
                "help": "Zooms out until Zoom Stop, at the speed given (0 slow to "
                "7 fast) or the Zoom Speed set on the device.",
            },
            "zoom_stop": {"label": "Zoom Stop", "params": {}, "help": "Stops zooming."},
            "zoom_direct": {
                "label": "Zoom to Position",
                "params": {
                    "position": {
                        "type": "integer",
                        "required": True,
                        "label": "Position",
                        "min": 0,
                        "max": 31424,
                        "default": 0,
                    }
                },
                "help": "Zooms to a position: 0 (wide) to 16384 (optical tele "
                "end), to 31424 with digital zoom on the SRG-A40.",
            },
            "focus_far": {
                "label": "Focus Far",
                "params": {
                    "speed": {"type": "integer", "label": "Speed", "min": 0, "max": 7}
                },
                "help": "Focuses farther until Focus Stop, in manual focus.",
            },
            "focus_near": {
                "label": "Focus Near",
                "params": {
                    "speed": {"type": "integer", "label": "Speed", "min": 0, "max": 7}
                },
                "help": "Focuses nearer until Focus Stop, in manual focus.",
            },
            "focus_stop": {
                "label": "Focus Stop",
                "params": {},
                "help": "Stops focusing.",
            },
            "focus_auto": {
                "label": "Autofocus",
                "params": {},
                "help": "Turns autofocus on.",
                "sets": {"focus_mode": "auto"},
            },
            "focus_manual": {
                "label": "Manual Focus",
                "params": {},
                "help": "Turns autofocus off; focus stays where it is.",
                "sets": {"focus_mode": "manual"},
            },
            "focus_one_push": {
                "label": "One Push Autofocus",
                "params": {},
                "help": "Focuses once, in manual focus.",
            },
            "focus_infinity": {
                "label": "Focus to Infinity",
                "params": {},
                "help": "Focuses at infinity.",
            },
            "focus_direct": {
                "label": "Focus to Position",
                "params": {
                    "position": {
                        "type": "integer",
                        "required": True,
                        "label": "Position",
                        "min": 4096,
                        "max": 61440,
                        "default": 4096,
                    }
                },
                "help": "Focuses to a position, 4096 (infinity) to 61440 (8 "
                "cm), in manual focus.",
            },
            "preset_recall": {
                "label": "Recall Preset",
                "params": {
                    "number": {
                        "type": "integer",
                        "required": True,
                        "label": "Preset",
                        "min": 1,
                        "max": 256,
                        "options_state": "preset_options",
                    }
                },
                "help": "Moves the camera to a saved preset (1-256). Presets "
                "1-64 go over VISCA; 65-256 need the web login.",
            },
            "preset_save": {
                "label": "Save Preset",
                "params": {
                    "number": {
                        "type": "integer",
                        "required": True,
                        "label": "Preset",
                        "min": 1,
                        "max": 256,
                        "options_state": "preset_options",
                    },
                    "name": {"type": "string", "label": "Name", "default": ""},
                },
                "help": "Saves the camera's position and picture settings as a "
                "preset (1-256), with a name if given (up to 32 bytes, "
                'no " % & , : ~). A name, or a preset above 64, needs '
                "the web login.",
            },
            "preset_clear": {
                "label": "Clear Preset",
                "params": {
                    "number": {
                        "type": "integer",
                        "required": True,
                        "label": "Preset",
                        "min": 1,
                        "max": 256,
                        "options_state": "preset_options",
                    }
                },
                "help": "Erases a saved preset. Presets above 64 need the web login.",
                "confirm": "Erases this preset from the camera.",
            },
            "preset_rename": {
                "label": "Rename Preset",
                "params": {
                    "number": {
                        "type": "integer",
                        "required": True,
                        "label": "Preset",
                        "min": 1,
                        "max": 256,
                        "options_state": "preset_options",
                    },
                    "name": {"type": "string", "required": True, "label": "Name"},
                },
                "help": 'Names a saved preset (up to 32 bytes, no " % & , : '
                "~). Needs the web login.",
            },
            "set_iris": {
                "label": "Set Iris",
                "params": {
                    "value": {
                        "type": "enum",
                        "required": True,
                        "label": "Iris",
                        "values": [
                            "F2.0",
                            "F2.2",
                            "F2.4",
                            "F2.6",
                            "F2.8",
                            "F3.1",
                            "F3.4",
                            "F3.7",
                            "F4.0",
                            "F4.4",
                            "F4.8",
                            "F5.2",
                            "F5.6",
                            "F6.2",
                            "F6.8",
                            "F7.3",
                            "F8.0",
                            "F8.7",
                            "F9.6",
                            "F10",
                            "F11",
                            "close",
                        ],
                    }
                },
                "help": "Sets the aperture, in manual or iris priority exposure.",
            },
            "set_gain": {
                "label": "Set Gain",
                "params": {
                    "db": {
                        "type": "integer",
                        "required": True,
                        "label": "Gain (dB)",
                        "min": 0,
                        "max": 48,
                        "default": 0,
                    }
                },
                "help": "Sets the gain in dB (0-48 in 3 dB steps; above 36 dB needs "
                "High Sensitivity), in manual exposure.",
            },
            "set_exposure_comp_level": {
                "label": "Set Exposure Compensation Level",
                "params": {
                    "level": {
                        "type": "integer",
                        "required": True,
                        "label": "Level",
                        "min": -7,
                        "max": 7,
                        "default": 0,
                    }
                },
                "help": "Sets the exposure compensation, -7 (-10.5 "
                "dB) to 7 (+10.5 dB), with Exposure "
                "Compensation on.",
            },
            "set_noise_reduction": {
                "label": "Set Noise Reduction",
                "params": {
                    "value": {
                        "type": "enum",
                        "required": True,
                        "label": "Noise Reduction",
                        "values": ["off", "1", "2", "3", "4", "5"],
                    }
                },
                "help": "Sets noise reduction, off or 1 (weak) to 5 (strong).",
            },
            "set_defog": {
                "label": "Set Defog",
                "params": {
                    "enabled": {"type": "boolean", "required": True, "label": "On"},
                    "level": {
                        "type": "integer",
                        "label": "Level",
                        "min": 0,
                        "max": 3,
                        "default": 0,
                    },
                },
                "help": "Turns defog on or off, at a level 0-3 (0 is the same as 2).",
            },
            "iris_up": {
                "label": "Iris Open",
                "params": {},
                "help": "Opens the iris one step.",
            },
            "iris_down": {
                "label": "Iris Close",
                "params": {},
                "help": "Closes the iris one step.",
            },
            "gain_up": {
                "label": "Gain Up",
                "params": {},
                "help": "Raises the gain one step.",
            },
            "gain_down": {
                "label": "Gain Down",
                "params": {},
                "help": "Lowers the gain one step.",
            },
            "shutter_up": {
                "label": "Shutter Faster",
                "params": {},
                "help": "Makes the shutter one step faster.",
            },
            "shutter_down": {
                "label": "Shutter Slower",
                "params": {},
                "help": "Makes the shutter one step slower.",
            },
            "exposure_comp_up": {
                "label": "Exposure Compensation Up",
                "params": {},
                "help": "Brightens the picture one exposure compensation step.",
            },
            "exposure_comp_down": {
                "label": "Exposure Compensation Down",
                "params": {},
                "help": "Darkens the picture one exposure compensation step.",
            },
            "wb_one_push_trigger": {
                "label": "One Push White Balance",
                "params": {},
                "help": "Sets white balance from what the camera sees "
                "now, in One Push mode.",
            },
            "red_gain_up": {
                "label": "Red Gain Up",
                "params": {},
                "help": "Raises red one step, in manual white balance.",
            },
            "red_gain_down": {
                "label": "Red Gain Down",
                "params": {},
                "help": "Lowers red one step, in manual white balance.",
            },
            "blue_gain_up": {
                "label": "Blue Gain Up",
                "params": {},
                "help": "Raises blue one step, in manual white balance.",
            },
            "blue_gain_down": {
                "label": "Blue Gain Down",
                "params": {},
                "help": "Lowers blue one step, in manual white balance.",
            },
            "osd_menu": {
                "label": "OSD Menu",
                "params": {},
                "help": "Opens or closes the camera's on-screen menu. The camera "
                "takes no other commands while it is open.",
            },
            "osd_enter": {
                "label": "OSD Enter",
                "params": {},
                "help": "Selects the highlighted on-screen menu item.",
            },
            "set_backlight": {
                "label": "Set Backlight Compensation",
                "params": {
                    "enabled": {"type": "boolean", "required": True, "label": "On"}
                },
                "help": "Brightens a subject in front of a bright background.",
                "sets": {"backlight": "{enabled}"},
            },
            "set_spotlight": {
                "label": "Set Spotlight Compensation",
                "params": {
                    "enabled": {"type": "boolean", "required": True, "label": "On"}
                },
                "help": "Holds exposure down for a brightly lit subject on a "
                "dark stage.",
                "sets": {"spotlight": "{enabled}"},
            },
            "set_high_sensitivity": {
                "label": "Set High Sensitivity",
                "params": {
                    "enabled": {"type": "boolean", "required": True, "label": "On"}
                },
                "help": "Raises the gain ceiling for dim rooms (gain up to 48 dB).",
                "sets": {"high_sensitivity": "{enabled}"},
            },
            "set_exposure_comp": {
                "label": "Set Exposure Compensation",
                "params": {
                    "enabled": {"type": "boolean", "required": True, "label": "On"}
                },
                "help": "Turns the exposure compensation level on or off.",
                "sets": {"exposure_comp": "{enabled}"},
            },
            "set_auto_slow_shutter": {
                "label": "Set Auto Slow Shutter",
                "params": {
                    "enabled": {"type": "boolean", "required": True, "label": "On"}
                },
                "help": "Lets the shutter slow down automatically in low light.",
                "sets": {"auto_slow_shutter": "{enabled}"},
            },
            "set_visibility_enhancer": {
                "label": "Set Visibility Enhancer",
                "params": {
                    "enabled": {"type": "boolean", "required": True, "label": "On"}
                },
                "help": "Evens out dark and bright areas of the picture.",
                "sets": {"visibility_enhancer": "{enabled}"},
            },
            "set_low_light": {
                "label": "Set Low Light Basis Brightness",
                "params": {
                    "enabled": {"type": "boolean", "required": True, "label": "On"}
                },
                "help": "Keeps the basic brightness up in low light.",
                "sets": {"low_light": "{enabled}"},
            },
            "set_flicker_cancel": {
                "label": "Set Flicker Cancel",
                "params": {
                    "enabled": {"type": "boolean", "required": True, "label": "On"}
                },
                "help": "Reduces flicker from lighting.",
                "sets": {"flicker_cancel": "{enabled}"},
            },
            "set_image_stabilizer": {
                "label": "Set Image Stabilizer",
                "params": {
                    "enabled": {"type": "boolean", "required": True, "label": "On"}
                },
                "help": "Reduces shake in the picture.",
                "sets": {"image_stabilizer": "{enabled}"},
            },
            "set_high_resolution": {
                "label": "Set High Resolution",
                "params": {
                    "enabled": {"type": "boolean", "required": True, "label": "On"}
                },
                "help": "Sharpens edges for a more detailed picture.",
                "sets": {"high_resolution": "{enabled}"},
            },
            "set_tele_convert": {
                "label": "Set Tele Convert",
                "params": {
                    "enabled": {"type": "boolean", "required": True, "label": "On"}
                },
                "help": "Doubles the zoom by cropping. On only at 1920x1080 formats.",
                "sets": {"tele_convert": "{enabled}"},
            },
            "set_image_flip": {
                "label": "Set Image Flip",
                "params": {
                    "enabled": {"type": "boolean", "required": True, "label": "On"}
                },
                "help": "Turns the picture upside down for a ceiling-mounted camera.",
                "sets": {"image_flip": "{enabled}"},
            },
            "set_ir_receive": {
                "label": "Set IR Remote Receive",
                "params": {
                    "enabled": {"type": "boolean", "required": True, "label": "On"}
                },
                "help": "Whether the camera takes commands from its infrared remote.",
                "sets": {"ir_receive": "{enabled}"},
            },
            "set_pan_tilt_slow": {
                "label": "Set Pan/Tilt Slow",
                "params": {
                    "enabled": {"type": "boolean", "required": True, "label": "On"}
                },
                "help": "Slows every pan and tilt speed for smooth on-air moves.",
                "sets": {"pan_tilt_slow": "{enabled}"},
            },
            "set_exposure_mode": {
                "label": "Set Exposure Mode",
                "params": {
                    "value": {
                        "type": "enum",
                        "required": True,
                        "label": "Exposure Mode",
                        "values": [
                            "full_auto",
                            "manual",
                            "shutter_priority",
                            "iris_priority",
                        ],
                    }
                },
                "help": "How the camera sets exposure: fully automatic, "
                "manual, or with the shutter or iris held.",
                "sets": {"exposure_mode": "{value}"},
            },
            "set_wb_mode": {
                "label": "Set White Balance Mode",
                "params": {
                    "value": {
                        "type": "enum",
                        "required": True,
                        "label": "White Balance Mode",
                        "values": [
                            "auto1",
                            "indoor",
                            "outdoor",
                            "one_push",
                            "auto2",
                            "manual",
                        ],
                    }
                },
                "help": "auto1 and auto2 follow the scene; indoor and outdoor "
                "are fixed; one_push holds the last One Push White "
                "Balance; manual uses the red and blue gains.",
                "sets": {"wb_mode": "{value}"},
            },
            "set_picture_effect": {
                "label": "Set Picture Effect",
                "params": {
                    "value": {
                        "type": "enum",
                        "required": True,
                        "label": "Picture Effect",
                        "values": ["off", "black_white"],
                    }
                },
                "help": "Off, or black and white.",
                "sets": {"picture_effect": "{value}"},
            },
            "set_zoom_mode": {
                "label": "Set Zoom Mode",
                "params": {
                    "value": {
                        "type": "enum",
                        "required": True,
                        "label": "Zoom Mode",
                        "values": ["optical", "clear_image", "digital"],
                    }
                },
                "help": "Optical only, Clear Image Zoom (up to 1.5x more at "
                "4K, 2x otherwise) or digital zoom to 240x. The "
                "SRG-A12 has optical only. Changing it means setting "
                "up PTZ Auto Framing's start position, detection "
                "settings and tracking range again.",
                "sets": {"zoom_mode": "{value}"},
            },
            "set_af_mode": {
                "label": "Set Autofocus Mode",
                "params": {
                    "value": {
                        "type": "enum",
                        "required": True,
                        "label": "Autofocus Mode",
                        "values": ["normal", "interval", "zoom_trigger"],
                    }
                },
                "help": "Normal (always), Interval (at set times) or Zoom "
                "Trigger (after each zoom).",
                "sets": {"af_mode": "{value}"},
            },
            "set_af_sensitivity": {
                "label": "Set Autofocus Sensitivity",
                "params": {
                    "value": {
                        "type": "enum",
                        "required": True,
                        "label": "Autofocus Sensitivity",
                        "values": ["normal", "low"],
                    }
                },
                "help": "Normal, or Low for a steadier focus.",
                "sets": {"af_sensitivity": "{value}"},
            },
            "set_ir_correction": {
                "label": "Set IR Correction",
                "params": {
                    "value": {
                        "type": "enum",
                        "required": True,
                        "label": "IR Correction",
                        "values": ["standard", "ir_light"],
                    }
                },
                "help": "Focus correction under infrared light.",
                "sets": {"ir_correction": "{value}"},
            },
            "set_standby_mode": {
                "label": "Set Standby Position",
                "params": {
                    "value": {
                        "type": "enum",
                        "required": True,
                        "label": "Standby Position",
                        "values": ["side", "neutral"],
                    }
                },
                "help": "Neutral leaves the camera where it is in standby; "
                "Side turns it to the end of its pan range.",
                "sets": {"standby_mode": "{value}"},
            },
            "set_preset_call_mode": {
                "label": "Set Preset Call Mode",
                "params": {
                    "value": {
                        "type": "enum",
                        "required": True,
                        "label": "Preset Call Mode",
                        "values": ["freeze", "normal"],
                    }
                },
                "help": "Freeze holds the picture still while the "
                "camera moves to a preset.",
                "sets": {"preset_call_mode": "{value}"},
            },
            "set_hdmi_color": {
                "label": "Set HDMI Color Space",
                "params": {
                    "value": {
                        "type": "enum",
                        "required": True,
                        "label": "HDMI Color Space",
                        "values": ["ycbcr", "rgb"],
                    }
                },
                "help": "YCbCr or RGB on the HDMI output.",
                "sets": {"hdmi_color": "{value}"},
            },
            "set_wb_speed": {
                "label": "Set White Balance Speed",
                "params": {
                    "level": {
                        "type": "integer",
                        "required": True,
                        "label": "White Balance Speed",
                        "min": 1,
                        "max": 5,
                        "default": 3,
                    }
                },
                "help": "How quickly automatic white balance follows a change, "
                "1 (slow) to 5 (fast).",
                "sets": {"wb_speed": "{level}"},
            },
            "set_chroma_suppress": {
                "label": "Set Chroma Suppress",
                "params": {
                    "level": {
                        "type": "integer",
                        "required": True,
                        "label": "Chroma Suppress",
                        "min": 0,
                        "max": 3,
                        "default": 0,
                    }
                },
                "help": "Reduces color noise in low light, 0 (off) to 3 (strong).",
                "sets": {"chroma_suppress": "{level}"},
            },
            "set_red_gain": {
                "label": "Set Red Gain",
                "params": {
                    "level": {
                        "type": "integer",
                        "required": True,
                        "label": "Red Gain",
                        "min": 0,
                        "max": 255,
                        "default": 128,
                    }
                },
                "help": "Red in manual white balance, 0-255 (128 is the middle).",
                "sets": {"red_gain": "{level}"},
            },
            "set_blue_gain": {
                "label": "Set Blue Gain",
                "params": {
                    "level": {
                        "type": "integer",
                        "required": True,
                        "label": "Blue Gain",
                        "min": 0,
                        "max": 255,
                        "default": 128,
                    }
                },
                "help": "Blue in manual white balance, 0-255 (128 is the middle).",
                "sets": {"blue_gain": "{level}"},
            },
            "set_detail_level": {
                "label": "Set Detail Level",
                "params": {
                    "level": {
                        "type": "integer",
                        "required": True,
                        "label": "Detail Level",
                        "min": 0,
                        "max": 15,
                        "default": 7,
                    }
                },
                "help": "Edge sharpening, 0-15 (7 is the factory level).",
                "sets": {"detail_level": "{level}"},
            },
            "set_shutter": {
                "label": "Set Shutter",
                "params": {
                    "level": {
                        "type": "integer",
                        "required": True,
                        "label": "Shutter",
                        "min": 6,
                        "max": 33,
                        "default": 18,
                    }
                },
                "help": "Shutter speed code, 6 (1/1 s) to 33 (1/10000 s), in "
                "manual or shutter priority. The speed each code gives "
                "depends on the video format: Sony's table lists them "
                "(18 is 1/60 at 1080/59.94p).",
                "sets": {"shutter": "{level}"},
            },
            "set_framing_mode": {
                "label": "Set Framing Mode",
                "params": {
                    "value": {
                        "type": "enum",
                        "required": True,
                        "label": "Framing Mode",
                        "values": ["person", "ball_sports"],
                    }
                },
                "help": "Person, or Ball Sports (indoor 5v5 basketball, "
                "firmware 4.0). Needs the web login.",
                "sets": {"framing_mode": "{value}"},
            },
            "set_auto_framing_trigger": {
                "label": "Set Subject Selection",
                "params": {
                    "value": {
                        "type": "enum",
                        "required": True,
                        "label": "Subject Selection",
                        "values": ["auto", "manual"],
                    }
                },
                "help": "auto picks the subject itself; manual "
                "waits for one to be chosen (Track Subject "
                "At). Needs the web login.",
                "sets": {"auto_framing_trigger": "{value}"},
            },
            "set_auto_framing_auto_start": {
                "label": "Set Start Auto Framing at Power On",
                "params": {
                    "value": {
                        "type": "boolean",
                        "required": True,
                        "label": "Start Auto Framing at Power On",
                    }
                },
                "help": "Starts PTZ Auto Framing whenever the "
                "camera powers on. Needs the web login.",
                "sets": {"auto_framing_auto_start": "{value}"},
            },
            "set_multi_tracking": {
                "label": "Set Multiple Subjects",
                "params": {
                    "value": {
                        "type": "boolean",
                        "required": True,
                        "label": "Multiple Subjects",
                    }
                },
                "help": "Frames several people together instead of one. "
                "Needs the web login.",
                "sets": {"multi_tracking": "{value}"},
            },
            "set_multi_tracking_targets": {
                "label": "Set Number of Subjects",
                "params": {
                    "value": {
                        "type": "integer",
                        "required": True,
                        "label": "Number of Subjects",
                        "min": 2,
                        "max": 8,
                    }
                },
                "help": "How many people to frame together, 2-8. Needs the web login.",
                "sets": {"multi_tracking_targets": "{value}"},
            },
            "set_multi_tracking_wait": {
                "label": "Set Wait for Subjects (s)",
                "params": {
                    "value": {
                        "type": "integer",
                        "required": True,
                        "label": "Wait for Subjects (s)",
                        "min": 0,
                        "max": 60,
                    }
                },
                "help": "How long to wait for that many people "
                "before framing, 1-60 s; 0 waits until they "
                "are all there. Needs the web login.",
                "sets": {"multi_tracking_wait": "{value}"},
            },
            "set_lead_room": {
                "label": "Set Lead Room",
                "params": {
                    "value": {
                        "type": "enum",
                        "required": True,
                        "label": "Lead Room",
                        "values": ["off", "low", "middle", "high"],
                    }
                },
                "help": "Leaves room in front of a moving subject. Needs the "
                "web login.",
                "sets": {"lead_room": "{value}"},
            },
            "set_face_indicator": {
                "label": "Set Face Indicator on Stream",
                "params": {
                    "value": {
                        "type": "boolean",
                        "required": True,
                        "label": "Face Indicator on Stream",
                    }
                },
                "help": "Draws the face indicator on the streamed video. "
                "Needs the web login.",
                "sets": {"face_indicator": "{value}"},
            },
            "set_individual_identification": {
                "label": "Set Personal Identification",
                "params": {
                    "value": {
                        "type": "boolean",
                        "required": True,
                        "label": "Personal Identification",
                    }
                },
                "help": "Recognizes the person being framed so "
                "the camera stays on them. Needs the "
                "web login.",
                "sets": {"individual_identification": "{value}"},
            },
            "set_registered_face_tracking": {
                "label": "Set Prefer Registered Faces",
                "params": {
                    "value": {
                        "type": "boolean",
                        "required": True,
                        "label": "Prefer Registered Faces",
                    }
                },
                "help": "Frames faces registered on the camera "
                "first. Needs the web login.",
                "sets": {"registered_face_tracking": "{value}"},
            },
            "set_lost_wait_time": {
                "label": "Set Lost Subject Wait (s)",
                "params": {
                    "value": {
                        "type": "integer",
                        "required": True,
                        "label": "Lost Subject Wait (s)",
                        "min": 1,
                        "max": 60,
                    }
                },
                "help": "How long to wait for a lost subject, 1-60 s. "
                "Needs the web login.",
                "sets": {"lost_wait_time": "{value}"},
            },
            "set_face_lost_wait_time": {
                "label": "Set Face Timeout (s)",
                "params": {
                    "value": {
                        "type": "integer",
                        "required": True,
                        "label": "Face Timeout (s)",
                        "min": 0,
                        "max": 60,
                    }
                },
                "help": "How long framing continues without seeing a "
                "face, 1-60 s; 0 never times out. Needs the "
                "web login.",
                "sets": {"face_lost_wait_time": "{value}"},
            },
            "set_detection_restart_time": {
                "label": "Set Restart Search After (s)",
                "params": {
                    "value": {
                        "type": "integer",
                        "required": True,
                        "label": "Restart Search After (s)",
                        "min": 0,
                        "max": 600,
                    }
                },
                "help": "Starts searching again after this long, "
                "1-600 s; 0 never. Needs the web login.",
                "sets": {"detection_restart_time": "{value}"},
            },
            "set_tally_level": {
                "label": "Set Tally Brightness",
                "params": {
                    "value": {
                        "type": "enum",
                        "required": True,
                        "label": "Tally Brightness",
                        "values": ["off", "low", "high"],
                    }
                },
                "help": "Off (the lamp never lights), low or high. Needs the "
                "web login.",
                "sets": {"tally_level": "{value}"},
            },
            "set_tally_follows_auto_framing": {
                "label": "Set Tally Shows Auto Framing",
                "params": {
                    "value": {
                        "type": "boolean",
                        "required": True,
                        "label": "Tally Shows Auto Framing",
                    }
                },
                "help": "The green tally lamp shows PTZ Auto "
                "Framing's status: lit while framing, "
                "flashing when the subject is lost. "
                "Needs the web login.",
                "sets": {"tally_follows_auto_framing": "{value}"},
            },
            "set_stream_mode": {
                "label": "Set Streaming Mode",
                "params": {
                    "value": {
                        "type": "enum",
                        "required": True,
                        "label": "Streaming Mode",
                        "values": [
                            "rtsp",
                            "rtmp",
                            "srt_caller",
                            "srt_listener",
                            "ndi_hx",
                            "off",
                        ],
                    }
                },
                "help": "Which stream the camera sends: RTSP, RTMP, SRT "
                "(caller or listener), NDI|HX or none. Changing it "
                "interrupts streaming. Needs the web login.",
                "sets": {"stream_mode": "{value}"},
            },
            "set_ndi_source_name": {
                "label": "Set NDI Source Name",
                "params": {
                    "value": {
                        "type": "string",
                        "required": True,
                        "label": "NDI Source Name",
                    }
                },
                "help": "The name the camera's NDI|HX stream appears "
                "under (up to 64 bytes). Needs the web login.",
                "sets": {"ndi_source_name": "{value}"},
            },
            "set_tally": {
                "label": "Set Tally",
                "params": {
                    "enabled": {"type": "boolean", "required": True, "label": "On"}
                },
                "help": "Lights or clears the tally lamp. The camera turns the "
                "lamp off after 15 s without a Tally On, so the driver "
                "sends it again every 5 s until Tally Off.",
                "sets": {"tally": "{enabled}"},
            },
            "rtmp_start": {
                "label": "Start RTMP Stream",
                "params": {},
                "help": "Starts streaming to the saved RTMP server (Streaming "
                "Mode RTMP). Needs the web login.",
            },
            "rtmp_stop": {
                "label": "Stop RTMP Stream",
                "params": {},
                "help": "Stops the RTMP stream. Needs the web login.",
            },
            "srt_start": {
                "label": "Start SRT Stream",
                "params": {},
                "help": "Starts the SRT caller stream to the saved SRT server "
                "(Streaming Mode SRT Caller). Needs the web login.",
            },
            "srt_stop": {
                "label": "Stop SRT Stream",
                "params": {},
                "help": "Stops the SRT stream. Needs the web login.",
            },
            "save_rtmp_url": {
                "label": "Save RTMP Server URL",
                "params": {"url": {"type": "string", "label": "URL", "default": ""}},
                "help": "rtmp:// or rtmps://, up to 512 bytes. Blank clears "
                "it. The camera reads & and , as separators, so a URL "
                "holding either cannot be saved. Needs the web login.",
            },
            "save_srt_url": {
                "label": "Save SRT Server URL",
                "params": {"url": {"type": "string", "label": "URL", "default": ""}},
                "help": "srt://, for the SRT caller mode, up to 512 bytes. "
                "Blank clears it. Needs the web login.",
            },
            "save_rtmp_key": {
                "label": "Save RTMP Stream Key",
                "params": {
                    "key": {"type": "string", "label": "Stream Key", "secret": True}
                },
                "help": "Saves the stream key from the video platform in the "
                "camera (up to 190 bytes; no & or ,). Blank clears it. "
                "The key is never shown: RTMP Stream Key Saved says "
                "whether one is. Needs the web login.",
            },
            "reboot": {
                "label": "Reboot Camera",
                "params": {},
                "help": "Restarts the camera. It is offline until it has started "
                "again. Needs the web login.",
            },
            "factory_reset": {
                "label": "Factory Reset",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "label": "Reset",
                        "values": ["soft", "hard"],
                        "default": "soft",
                    }
                },
                "help": "Returns the camera to factory settings: soft keeps "
                "the network and security settings, hard resets "
                "everything. Needs the web login.",
                "confirm": "Returns the camera's settings to factory values. A "
                "hard reset also clears its network and security "
                "settings, so it comes back on DHCP with no "
                "password and must be set up again.",
            },
        },
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._http: httpx.AsyncClient | None = None
        self._base_url = ""
        self._referer = ""
        self._visca_transport: asyncio.DatagramTransport | None = None
        self._visca_target: tuple[str, int] | None = None
        self._sequence = 0
        self._visca_lock: asyncio.Lock | None = None
        self._visca_waiter: asyncio.Future[bytes] | None = None
        self._waiting_for = ""
        self._control_reply: asyncio.Future[None] | None = None
        self._poll_count = 0
        self._cgi_unavailable = ""
        self._cgi_refusals: dict[str, str] = {}
        self._unread_logged: set[str] = set()
        self._tally_hold = False
        self._tally_lit_at = 0.0
        self._tally_task: asyncio.Task | None = None
        self._standby_at = 0.0

    # ── Connection lifecycle ──

    async def _pre_connect(self) -> None:
        if not self.config.get("host", ""):
            raise ConnectionError(f"[{self.device_id}] host is required")
        self._visca_lock = asyncio.Lock()
        self._sequence = 0
        self._poll_count = 0
        self._cgi_unavailable = ""
        self._cgi_refusals = {}
        self._unread_logged = set()
        self._tally_hold = False

    async def _create_transport(self, transport_type: str) -> None:
        """Driver-owned session: an httpx client for the CGI and a UDP socket
        for VISCA over IP. No platform transport."""
        host = self.config.get("host", "")
        port = int(self.config.get("port", 80))
        ssl = bool(self.config.get("ssl"))
        scheme = "https" if ssl else "http"
        self._base_url = f"{scheme}://{host}:{port}"
        default_port = 443 if ssl else 80
        # "If you use CGI commands with Referer check enabled, you can do so by
        # adding Referer header" (Changing the Setting of Referer Check). The
        # camera's factory setting has the check on.
        self._referer = (
            f"{scheme}://{host}/"
            if port == default_port
            else f"{scheme}://{host}:{port}/"
        )
        username = self.config.get("username", "") or ""
        password = self.config.get("password", "") or ""
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=5.0,
            auth=httpx.DigestAuth(username, password) if username else None,
            headers={"Referer": self._referer},
            verify=bool(self.config.get("verify_ssl", False)),
            follow_redirects=False,
        )
        # Under simulation (and the catalog's connect test) the platform points
        # a driver's datagrams at the simulator with udp_redirect.
        redirect = getattr(self, "udp_redirect", None)
        target = redirect or (host, _VISCA_PORT)
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
        visca_ok = await self._visca_answers()
        http_ok = await self._http_answers()
        if not visca_ok and not http_ok:
            raise ConnectionError(
                f"The camera at {self.config.get('host')} answered neither "
                f"VISCA over IP (UDP {_VISCA_PORT}) nor HTTP."
            )
        port = int(self.config.get("port", 80))
        self.set_states(self._stream_urls())
        if port and not visca_ok:
            log.info(f"[{self.device_id}] VISCA over IP is not answering; HTTP is")

    async def _visca_answers(self) -> bool:
        try:
            await self._control_reset()
        except asyncio.TimeoutError:
            pass
        return await self._inquire(_INQUIRY["power"]) is not None

    async def _http_answers(self) -> bool:
        try:
            return await self._read_groups(_CGI_AT_CONNECT, quiet=False)
        except ConnectionFaultError:
            raise
        except ConnectionError:
            return False

    def _stream_urls(self) -> dict[str, str]:
        host = self.config.get("host")
        rtsp_port = int(self.config.get("rtsp_port", 554))
        rtsp = (
            f"rtsp://{host}/video1"
            if rtsp_port == 554
            else f"rtsp://{host}:{rtsp_port}/video1"
        )
        return {
            "preview_url": rtsp,
            "preview_format": "rtsp",
            "mjpeg_url": f"{self._base_url}/mjpeg",
            "snapshot_url": f"{self._base_url}/oneshotimage1",
        }

    async def _initial_sync(self) -> None:
        try:
            await self.poll(full=True)
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Initial poll failed")

    def _link_alive(self) -> bool:
        return self._http is not None

    async def _close_session(self) -> None:
        self._tally_hold = False
        task, self._tally_task = self._tally_task, None
        if task is not None and not task.done():
            task.cancel()
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
            _settle(self._control_reply, None)
        elif payload_type == PAYLOAD_CONTROL_CMD:
            # ERROR 0F 01 (abnormality in the sequence number) or 0F 02 (in
            # the message type): the camera's answer to the message in flight.
            if payload[:1] == b"\x0f":
                _settle(self._visca_waiter, b"\x0f" + payload[1:2])
        elif payload_type == PAYLOAD_VISCA_REPLY:
            self._on_visca_reply(payload, sequence)

    def _on_visca_reply(self, payload: bytes, sequence: int) -> None:
        if len(payload) < 3 or payload[0] != 0x90:
            return
        if self._answers_waiter(payload[1], sequence):
            _settle(self._visca_waiter, payload)

    def _answers_waiter(self, kind: int, sequence: int) -> bool:
        """Whether a reply of this kind answers the message in flight."""
        if self._waiting_for == "inquiry":
            # An inquiry's answer is exactly 50 with data, or an error (6y).
            # A command's late completion (5y, y = socket) is not it.
            return kind == 0x50 or kind & 0xF0 == 0x60
        # A command waits for its acknowledgement or the error refusing it.
        if kind & 0xF0 in (0x40, 0x60):
            return True
        # A completion counts only with this command's sequence number: the
        # previous command's completion can arrive after its acknowledgement
        # (aver_tr's simulation finding, kept).
        return kind & 0xF0 == 0x50 and sequence == self._sequence

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

    async def _exchange(
        self, payload: bytes, kind: str, timeout: float
    ) -> bytes | None:
        """One VISCA message and its first reply, or None on silence."""
        if self._visca_lock is None or self._visca_transport is None:
            return None
        async with self._visca_lock:
            for attempt in range(2):
                self._visca_waiter = asyncio.get_running_loop().create_future()
                self._waiting_for = kind
                try:
                    self._send(
                        payload,
                        PAYLOAD_VISCA_INQUIRY
                        if kind == "inquiry"
                        else PAYLOAD_VISCA_COMMAND,
                    )
                    reply = await asyncio.wait_for(self._visca_waiter, timeout=timeout)
                except asyncio.TimeoutError:
                    return None
                finally:
                    self._visca_waiter = None
                if reply[:1] != b"\x0f":
                    return reply
                if attempt:
                    return None
                # The camera has lost track of the sequence (another start,
                # or it restarted): reset it and send once more.
                log.info(
                    f"[{self.device_id}] VISCA sequence error; resetting the sequence"
                )
                try:
                    await self._control_reset()
                except asyncio.TimeoutError:
                    return None
            return None

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
            raise CommandRefusedError(self._refusal(reply[2]))

    def _refusal(self, code: int) -> str:
        if code == 0x41 and self.get_state("osd_menu"):
            return (
                "The camera's on-screen menu is open, and the camera takes no "
                "commands while it is. Close it with OSD Menu."
            )
        return _VISCA_REFUSALS.get(
            code, f"The camera refused the command (VISCA error {code:02X})."
        )

    def _apply(self, name: str, reply: bytes) -> None:
        """Write one inquiry's answer into state. An error reply writes nothing."""
        reader = _READERS.get(name)
        if not reply or reader is None:
            return
        for key, value in reader(reply).items():
            self.set_state(key, value)

    async def _read_back(self, names: tuple[str, ...]) -> None:
        for name in names:
            reply = await self._inquire(_INQUIRY[name])
            if reply is None:
                return
            self._apply(name, reply)

    # ── Tally ──

    def _hold_tally(self) -> None:
        self._tally_hold = True
        self._tally_lit_at = time.monotonic()
        if self._tally_task is None or self._tally_task.done():
            self._tally_task = asyncio.ensure_future(self._tally_loop())

    async def _tally_loop(self) -> None:
        """Tally On again every few seconds while it is held: the camera turns
        it off after 15 s without one."""
        try:
            while self._tally_hold and self._visca_transport is not None:
                await asyncio.sleep(_TALLY_REFRESH_S)
                if not self._tally_hold:
                    return
                try:
                    await self._visca_command(b"\x81\x01\x7e\x01\x0a\x00\x02\xff")
                    self._tally_lit_at = time.monotonic()
                except (ConnectionError, CommandRefusedError) as exc:
                    log.debug(f"[{self.device_id}] Tally refresh not taken: {exc}")
        except asyncio.CancelledError:
            pass

    def _check_tally(self) -> None:
        """A tally read off soon after the driver lit it was turned off by
        another controller: stop holding it on."""
        if self._tally_hold and self._tally_turned_off():
            self._tally_hold = False

    def _tally_turned_off(self) -> bool:
        if self.get_state("tally") is not False:
            return False
        return time.monotonic() - self._tally_lit_at < _TALLY_OVERRIDE_S

    # ── HTTP CGI ──

    async def _cgi(self, path: str, timeout: float | None = None) -> str:
        """GET /<path>. Returns the body of a 2xx; raises otherwise."""
        if self._http is None:
            raise ConnectionError("Not connected")
        options = {} if timeout is None else {"timeout": timeout}
        try:
            resp = await self._http.get(f"/{path}", **options)
        except httpx.TransportError as exc:
            raise ConnectionError(
                f"The camera at {self._base_url} is not answering HTTP: {exc}"
            ) from exc
        self._check_status(resp.status_code)
        return resp.text

    def _check_status(self, status: int) -> None:
        if status == 401:
            self._refused_login()
        if status == 403:
            raise _Forbidden()
        if not 200 <= status < 300:
            raise CommandRefusedError(
                f"The camera answered HTTP {status} to that request."
            )

    def _refused_login(self) -> None:
        if self.config.get("username"):
            # The camera counts refusals as a brute force attack (8 by
            # default) and locks the address out: never retried.
            raise ConnectionFaultError(
                "The camera refused the web username and password.",
                code="auth_failed",
            )
        raise _LoginRequired()

    async def _cgi_set(self, path: str, query: str) -> None:
        try:
            await self._cgi(f"{path}?{query}")
        except _Forbidden:
            raise CommandRefusedError(
                "The camera refused this to the web account entered (HTTP 403). "
                "Settings, power and streaming need an Administrator account."
            ) from None

    def _apply_cgi(self, values: dict[str, str]) -> None:
        for name, raw in values.items():
            spec = _CGI_READS.get(name)
            if spec is not None:
                self._apply_cgi_value(name, raw, spec)
                continue
            read = _CGI_SPECIAL_READS.get(name)
            if read is not None:
                read(self, name, raw)

    def _apply_cgi_value(self, name: str, raw: str, spec: tuple[str, str, Any]) -> None:
        state, kind, choices = spec
        value = _CGI_DECODERS.get(kind, lambda text, _c: text)(raw, choices)
        if value is _UNREAD:
            self._unread(name, raw)
        elif value is not _SKIP:
            self.set_state(state, value)

    def _read_composition(self, name: str, raw: str) -> None:
        try:
            vertical, horizontal, size = (int(p) for p in raw.split(",")[:3])
        except ValueError:
            self._unread(name, raw)
            return
        self.set_states(
            {
                "composition_vertical": vertical,
                "composition_horizontal": horizontal,
                "composition_size": size,
            }
        )

    def _read_target_count(self, name: str, raw: str) -> None:
        try:
            self.set_state("auto_framing_targets", int(raw.split(",")[0]))
        except ValueError:
            self._unread(name, raw)

    def _read_picker(self, name: str, raw: str) -> None:
        self.set_state(_PICKERS[name], _options(raw))

    def _read_key_set(self, _name: str, raw: str) -> None:
        if raw in ("0", "1"):
            self.set_state("rtmp_key_set", raw == "1")

    def _unread(self, what: str, text: str) -> None:
        if what not in self._unread_logged:
            self._unread_logged.add(what)
            log.warning(
                f"[{self.device_id}] {what} reply not understood: {text[:80]!r}"
            )

    async def _read_groups(self, groups: tuple[str, ...], quiet: bool = True) -> bool:
        """Read inquiry groups into state. True when the camera answered.

        A camera with no web login entered answers 401: that is a missing
        login, not a dead camera, so Last Error says why and VISCA keeps
        working. The read is still made, because the 401 is what shows the
        camera is still there (``aver_tr`` 1.0.1's lesson)."""
        query = "&".join(f"inq={g}" for g in groups)
        timeout = _POLL_HTTP_TIMEOUT_S if quiet else None
        try:
            text = await self._cgi(f"command/inquiry.cgi?{query}", timeout=timeout)
        except _LoginRequired:
            self._cgi_unavailable = (
                "The camera asks for its web login, which is not entered: presets "
                "above 64, preset names, PTZ Auto Framing settings and status, "
                "streaming and the camera's model are not available. VISCA control "
                "works."
            )
            return True
        except _Forbidden:
            self._cgi_refusals[",".join(groups)] = (
                f"The web account entered may not read {', '.join(groups)} (HTTP 403)."
            )
            return True
        except CommandRefusedError as exc:
            self._cgi_refusals[",".join(groups)] = f"Reading {', '.join(groups)}: {exc}"
            return True
        self._cgi_unavailable = ""
        self._cgi_refusals.pop(",".join(groups), None)
        self._apply_cgi(_parse_inquiry(text))
        return True

    def _http_silent(self) -> str:
        port = int(self.config.get("port", 80))
        return (
            f"The camera's web interface (TCP {port}) is not answering, though "
            f"VISCA over IP is: presets above 64, preset names, PTZ Auto Framing "
            f"settings, streaming, Reboot and Factory Reset are unavailable."
        )

    def _model_zoom_max(self) -> int:
        return _ZOOM_MAX.get(str(self.get_state("model") or ""), 0x7AC0)

    # ── Commands ──

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
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
        handler = _COMMAND_HANDLERS.get(command)
        if handler is None:
            raise ValueError(f"Unknown command: {command}")
        await handler(self, command, params)

    # Commands from the VISCA tables.

    async def _run_simple(self, command: str, _params: dict[str, Any]) -> None:
        if command == "power_on":
            await self._wait_after_standby()
        await self._visca_command(_SIMPLE_COMMANDS[command])
        if command == "power_off":
            self._standby_at = time.monotonic()

    async def _wait_after_standby(self) -> None:
        if not self._standby_at:
            return
        wait = _POWER_ON_GAP_S - (time.monotonic() - self._standby_at)
        if wait > 0:
            await asyncio.sleep(wait)

    async def _run_drive(self, command: str, _params: dict[str, Any]) -> None:
        pan_dir, tilt_dir = _PT_DIRECTIONS[command]
        speeds = [self._speed("pan_speed", 24), self._speed("tilt_speed", 23)]
        await self._visca_command(
            b"\x81\x01\x06\x01" + bytes(speeds + [pan_dir, tilt_dir]) + b"\xff"
        )

    async def _run_switch(self, command: str, params: dict[str, Any]) -> None:
        _state, prefix, on, off = _VISCA_SWITCHES[command]
        await self._visca_command(
            prefix + bytes([on if params["enabled"] else off, 0xFF])
        )

    async def _run_enum(self, command: str, params: dict[str, Any]) -> None:
        _state, prefix, codes = _VISCA_ENUMS[command]
        value = _one_of(str(params["value"]), codes)
        if command == "set_zoom_mode":
            self._check_zoom_mode(value)
        await self._visca_command(prefix + bytes([codes[value], 0xFF]))

    def _check_zoom_mode(self, value: str) -> None:
        if value != "optical" and self.get_state("model") == "SRG-A12":
            raise CommandRefusedError(
                "The SRG-A12 has no Clear Image Zoom or digital zoom."
            )

    async def _run_nibble_level(self, command: str, params: dict[str, Any]) -> None:
        _state, prefix, low, high = _VISCA_NIBBLE_LEVELS[command]
        level = _bounded(params["level"], low, high)
        await self._visca_command(prefix + bytes([level, 0xFF]))

    async def _run_byte_level(self, command: str, params: dict[str, Any]) -> None:
        _state, prefix, low, high = _VISCA_BYTE_LEVELS[command]
        level = _bounded(params["level"], low, high)
        await self._visca_command(
            prefix + b"\x00\x00" + _to_nibbles(level, 2) + b"\xff"
        )

    async def _run_cgi_setting(self, command: str, params: dict[str, Any]) -> None:
        await self._cgi_setting(command, params["value"])

    async def _cgi_setting(self, command: str, value: Any) -> None:
        path, name, _state, kind, choices, group = _CGI_SETTINGS[command]
        word = _CGI_ENCODERS.get(kind, _encode_text)(value, choices)
        await self._cgi_set(path, f"{name}={word}")
        await self._read_groups((group,), quiet=False)

    # Lens and position.

    async def _pt_absolute(self, _command: str, params: dict[str, Any]) -> None:
        pan = _bounded(params["pan"], -8704, 8704) & 0xFFFF
        tilt = _bounded(params["tilt"], -4608, 4608) & 0xFFFF
        speed = params.get("speed")
        speed = (
            self._speed("pan_speed", 24)
            if speed in (None, "")
            else _bounded(speed, 1, 24)
        )
        await self._visca_command(
            b"\x81\x01\x06\x02"
            + bytes([speed, 0x00])
            + _to_nibbles(pan)
            + _to_nibbles(tilt)
            + b"\xff"
        )

    async def _zoom_drive(self, command: str, params: dict[str, Any]) -> None:
        speed = params.get("speed")
        if speed in (None, ""):
            speed = self.config.get("zoom_speed", 4)
        code = _ZOOM_DRIVE[command] | _bounded(speed, 0, 7)
        await self._visca_command(bytes([0x81, 0x01, 0x04, 0x07, code, 0xFF]))

    async def _zoom_direct(self, _command: str, params: dict[str, Any]) -> None:
        position = int(params["position"])
        limit = self._model_zoom_max()
        if not 0 <= position <= limit:
            raise ValueError(
                f"Zoom position 0-{limit} on this camera "
                f"({self.get_state('model') or 'model not read'})."
            )
        await self._visca_command(b"\x81\x01\x04\x47" + _to_nibbles(position) + b"\xff")

    async def _focus_drive(self, command: str, params: dict[str, Any]) -> None:
        standard, base = _FOCUS_DRIVE[command]
        speed = params.get("speed")
        # No speed: the standard-speed form (8x 01 04 08 02 / 03 FF).
        code = standard if speed in (None, "") else base | _bounded(speed, 0, 7)
        await self._visca_command(bytes([0x81, 0x01, 0x04, 0x08, code, 0xFF]))

    async def _focus_direct(self, _command: str, params: dict[str, Any]) -> None:
        position = _bounded(params["position"], 0x1000, 0xF000)
        await self._visca_command(b"\x81\x01\x04\x48" + _to_nibbles(position) + b"\xff")

    # Picture.

    async def _set_iris(self, _command: str, params: dict[str, Any]) -> None:
        value = _one_of(str(params["value"]), _IRIS_CODES)
        await self._visca_command(
            b"\x81\x01\x04\x4b\x00\x00" + _to_nibbles(_IRIS_CODES[value], 2) + b"\xff"
        )

    async def _set_gain(self, _command: str, params: dict[str, Any]) -> None:
        db = int(params["db"])
        if db % 3 or not 0 <= db <= 48:
            raise ValueError("Gain is 0-48 dB in 3 dB steps.")
        await self._visca_command(
            b"\x81\x01\x04\x4c\x00\x00" + _to_nibbles(db // 3 + 1, 2) + b"\xff"
        )

    async def _set_exposure_comp_level(
        self, _command: str, params: dict[str, Any]
    ) -> None:
        level = _bounded(params["level"], -7, 7)
        await self._visca_command(
            b"\x81\x01\x04\x4e\x00\x00" + _to_nibbles(level + 7, 2) + b"\xff"
        )

    async def _set_noise_reduction(self, _command: str, params: dict[str, Any]) -> None:
        value = _one_of(str(params["value"]), _NR_CODES)
        await self._visca_command(
            bytes([0x81, 0x01, 0x04, 0x53, _NR_CODES[value], 0xFF])
        )

    async def _set_defog(self, _command: str, params: dict[str, Any]) -> None:
        level = _bounded(params.get("level") or 0, 0, 3)
        on = 0x02 if params["enabled"] else 0x03
        await self._visca_command(bytes([0x81, 0x01, 0x04, 0x37, on, level, 0xFF]))

    async def _set_tally(self, _command: str, params: dict[str, Any]) -> None:
        if params["enabled"]:
            await self._visca_command(b"\x81\x01\x7e\x01\x0a\x00\x02\xff")
            self._hold_tally()
        else:
            self._tally_hold = False
            await self._visca_command(b"\x81\x01\x7e\x01\x0a\x00\x03\xff")

    # Presets: 1-64 over VISCA (pp = the number less one), the rest and the
    # names over the CGI.

    async def _preset_action(self, number: int, visca_action: int, query: str) -> None:
        if number <= _PRESETS_OVER_VISCA:
            await self._visca_command(
                bytes([0x81, 0x01, 0x04, 0x3F, visca_action, number - 1, 0xFF])
            )
        else:
            await self._cgi_set("command/presetposition.cgi", query)

    async def _preset_recall(self, _command: str, params: dict[str, Any]) -> None:
        number = _preset_number(params)
        await self._preset_action(number, 0x02, f"PresetCall={number}")

    async def _preset_save(self, _command: str, params: dict[str, Any]) -> None:
        number = _preset_number(params)
        name = str(params.get("name") or "")
        if number <= _PRESETS_OVER_VISCA and not name:
            await self._preset_action(number, 0x01, "")
        else:
            value = _name_value(name, "preset name")
            await self._cgi_set(
                "command/presetposition.cgi", f"PresetSet={number},{value},off"
            )
        await self._read_presets_quietly()

    async def _preset_clear(self, _command: str, params: dict[str, Any]) -> None:
        number = _preset_number(params)
        await self._preset_action(number, 0x00, f"PresetClear={number}")
        await self._read_presets_quietly()

    async def _preset_rename(self, _command: str, params: dict[str, Any]) -> None:
        number = _preset_number(params)
        value = _name_value(str(params.get("name") or ""), "preset name")
        await self._cgi_set(
            "command/presetposition.cgi", f"PresetName={number},{value}"
        )
        await self._read_groups(("presetposition",), quiet=False)

    async def _read_presets_quietly(self) -> None:
        """Preset names come from the CGI; without it they wait for the poll."""
        try:
            await self._read_groups(("presetposition",), quiet=False)
        except (ConnectionError, _LoginRequired):
            pass

    # PTZ Auto Framing.

    async def _auto_framing_set(self, query: str) -> None:
        await self._cgi_set("analytics/ptzautoframing.cgi", query)
        await self._read_groups(("ptzautoframing",), quiet=False)

    async def _set_composition(self, _command: str, params: dict[str, Any]) -> None:
        vertical = _bounded(params["vertical"], -50, 70)
        horizontal = _bounded(params["horizontal"], -50, 50)
        size = _bounded(params["size"], 160, 3500)
        await self._auto_framing_set(
            f"PtzAutoFramingAdjustSetting={vertical},{horizontal},{size}"
        )

    async def _composition_preset(self, command: str, params: dict[str, Any]) -> None:
        number = _bounded(params["number"], 1, 3)
        await self._auto_framing_set(f"{_COMPOSITION_PRESET_ACTIONS[command]}={number}")

    async def _composition_preset_save(
        self, _command: str, params: dict[str, Any]
    ) -> None:
        number = _bounded(params["number"], 1, 3)
        value = _name_value(str(params.get("name") or ""), "composition preset name")
        await self._auto_framing_set(f"PtzAutoFramingPresetSet={number},{value}")

    async def _track_subject_at(self, _command: str, params: dict[str, Any]) -> None:
        x = _bounded(params["x"], 0, 10000)
        y = _bounded(params["y"], 0, 10000)
        await self._cgi_set(
            "analytics/ptzautoframing.cgi", f"PtzAutoFramingObjectPosition={x},{y}"
        )

    async def _cgi_action(self, command: str, _params: dict[str, Any]) -> None:
        path, query, group = _CGI_ACTIONS[command]
        await self._cgi_set(path, query)
        if group:
            await self._read_groups((group,), quiet=False)

    # Streaming and the system.

    async def _save_stream_url(self, command: str, params: dict[str, Any]) -> None:
        schemes, path, name, group, key = _STREAM_URLS[command]
        url = str(params.get("url") or "")
        await self._cgi_set(path, f"{name}={_url_value(url, schemes)}")
        await self._read_groups((group,), quiet=False)
        if self.get_state(key) != url:
            raise CommandRefusedError("The camera did not keep that URL.")

    async def _save_rtmp_key(self, _command: str, params: dict[str, Any]) -> None:
        key = str(params.get("key") or "")
        if len(key.encode("utf-8")) > 190:
            raise ValueError("The stream key is at most 190 bytes.")
        value = _text_value(key, "stream key")
        # The camera reports only whether a key is set; the key itself is
        # never read and never put in state, which reaches the cloud.
        await self._cgi_set("command/rtmp.cgi", f"RtmpStreamKey1={value}")
        await self._read_groups(("rtmp",), quiet=False)

    async def _factory_reset(self, _command: str, params: dict[str, Any]) -> None:
        mode = str(params.get("mode") or "soft")
        if mode not in ("soft", "hard"):
            raise ValueError("Factory Reset is soft or hard.")
        await self._cgi_set("command/main.cgi", f"FactoryDefault={mode}")

    # ── Device settings ──

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if key == "focus_mode":
            await self.send_command(
                "focus_auto" if str(value) == "auto" else "focus_manual"
            )
            return
        spec = _SETTING_COMMANDS.get(key)
        if spec is None:
            raise ValueError(f"Unknown device setting: {key}")
        command, param = spec
        kind = self.DRIVER_INFO["device_settings"][key]["type"]
        if kind == "boolean":
            value = (
                value
                if isinstance(value, bool)
                else str(value).strip().lower()
                in (
                    "1",
                    "true",
                    "on",
                    "yes",
                )
            )
        elif kind == "integer":
            value = int(value)
        await self.send_command(command, {param: value})

    # ── Polling ──

    async def poll(self, full: bool = False) -> None:
        if self._http is None:
            return
        self._poll_count += 1
        slow = full or self._poll_count % _SLOW_EVERY == 0
        problems: list[str] = []
        visca_answered = await self._poll_visca(slow, problems)
        self._check_tally()
        await self._poll_cgi(self._poll_groups(full, slow), visca_answered, problems)
        self._report_problems(problems)

    async def _poll_visca(self, slow: bool, problems: list[str]) -> bool:
        """The VISCA inquiries; True when the camera answered any.

        Stops at the first inquiry nothing answers. The camera answers one
        it does not support with an error, so silence means VISCA over IP is
        not reaching it, and waiting out the rest would only make the poll
        run long."""
        answered = False
        inquiries = _FAST_INQUIRIES + (_SLOW_INQUIRIES if slow else ())
        for name, payload in inquiries:
            reply = await self._inquire(payload)
            if reply is None:
                problems.append(
                    f"The camera did not answer VISCA over IP on UDP {_VISCA_PORT}: "
                    f"check that CAMERA SETUP switch 3 (VISCA) is on."
                )
                break
            answered = True
            self._apply(name, reply)
        return answered

    def _poll_groups(self, full: bool, slow: bool) -> tuple[str, ...]:
        groups = _CGI_EVERY_POLL
        if slow:
            groups += _CGI_EVERY_6TH
        if full or self._poll_count % (_SLOW_EVERY * 2) == 0:
            groups += _CGI_EVERY_12TH
        if full:
            groups += _CGI_AT_CONNECT
        return groups

    async def _poll_cgi(
        self, groups: tuple[str, ...], visca_answered: bool, problems: list[str]
    ) -> None:
        # With VISCA silent too, a transport error raises: that is what takes
        # a camera that stopped answering offline.
        try:
            await self._read_groups(groups)
        except ConnectionFaultError:
            raise
        except ConnectionError:
            if not visca_answered:
                raise
            problems.append(self._http_silent())

    def _report_problems(self, problems: list[str]) -> None:
        if self._cgi_unavailable:
            problems.append(self._cgi_unavailable)
        problems.extend(self._cgi_refusals.values())
        if self.get_state("temperature_warning"):
            problems.append("The camera reports a temperature anomaly inside it.")
        # A standing condition: written again on every poll while it holds,
        # since the platform clears Last Error after a clean poll.
        if problems:
            self.set_state("last_error", " ".join(problems))

    def _speed(self, key: str, high: int) -> int:
        try:
            value = int(self.config.get(key, 12))
        except (TypeError, ValueError):
            value = 12
        return max(1, min(high, value))


# What each command changes that VISCA reports, read again right after the
# camera accepts it.
_READ_BACK: dict[str, tuple[str, ...]] = {
    "power_on": ("power",),
    "power_off": ("power",),
    "auto_framing_start": ("auto_framing",),
    "auto_framing_stop": ("auto_framing",),
    "pt_stop": ("pan_tilt",),
    "pt_home": ("pan_tilt",),
    "pt_absolute": ("pan_tilt",),
    "zoom_stop": ("zoom_position",),
    "zoom_direct": ("zoom_position",),
    "focus_stop": ("focus_position",),
    "focus_direct": ("focus_position",),
    "focus_infinity": ("focus_position",),
    "focus_auto": ("focus_mode",),
    "focus_manual": ("focus_mode",),
    "set_iris": ("iris",),
    "iris_up": ("iris",),
    "iris_down": ("iris",),
    "set_gain": ("gain",),
    "gain_up": ("gain",),
    "gain_down": ("gain",),
    "shutter_up": ("shutter",),
    "shutter_down": ("shutter",),
    "set_exposure_comp_level": ("exposure_comp_level",),
    "exposure_comp_up": ("exposure_comp_level",),
    "exposure_comp_down": ("exposure_comp_level",),
    "red_gain_up": ("red_gain",),
    "red_gain_down": ("red_gain",),
    "blue_gain_up": ("blue_gain",),
    "blue_gain_down": ("blue_gain",),
    "set_noise_reduction": ("noise_reduction",),
    "set_defog": ("defog",),
    "set_tally": ("tally",),
    "osd_menu": ("osd_menu",),
    "preset_recall": ("last_preset",),
    "preset_save": ("last_preset",),
}
_READ_BACK.update({cmd: (spec[0],) for cmd, spec in _VISCA_SWITCHES.items()})
_READ_BACK.update({cmd: (spec[0],) for cmd, spec in _VISCA_ENUMS.items()})
_READ_BACK.update({cmd: (spec[0],) for cmd, spec in _VISCA_NIBBLE_LEVELS.items()})
_READ_BACK.update({cmd: (spec[0],) for cmd, spec in _VISCA_BYTE_LEVELS.items()})

# Device setting -> (command, its parameter).
_SETTING_COMMANDS: dict[str, tuple[str, str]] = {
    "iris": ("set_iris", "value"),
    "gain_db": ("set_gain", "db"),
    "exposure_comp_level": ("set_exposure_comp_level", "level"),
    "noise_reduction": ("set_noise_reduction", "value"),
    "rtmp_url": ("save_rtmp_url", "url"),
    "srt_url": ("save_srt_url", "url"),
}
_SETTING_COMMANDS.update(
    {spec[0]: (cmd, "enabled") for cmd, spec in _VISCA_SWITCHES.items()}
)
_SETTING_COMMANDS.update(
    {spec[0]: (cmd, "value") for cmd, spec in _VISCA_ENUMS.items()}
)
_SETTING_COMMANDS.update(
    {spec[0]: (cmd, "level") for cmd, spec in _VISCA_NIBBLE_LEVELS.items()}
)
_SETTING_COMMANDS.update(
    {spec[0]: (cmd, "level") for cmd, spec in _VISCA_BYTE_LEVELS.items()}
)
_SETTING_COMMANDS.update(
    {spec[2]: (cmd, "value") for cmd, spec in _CGI_SETTINGS.items()}
)

# Every command and the method that sends it. The VISCA and CGI tables share
# a method per table; no command is in two of them.
_D = SonySRGADriver
_COMMAND_HANDLERS: dict[str, Callable[..., Any]] = {
    **dict.fromkeys(_SIMPLE_COMMANDS, _D._run_simple),
    **dict.fromkeys(_PT_DIRECTIONS, _D._run_drive),
    **dict.fromkeys(_VISCA_SWITCHES, _D._run_switch),
    **dict.fromkeys(_VISCA_ENUMS, _D._run_enum),
    **dict.fromkeys(_VISCA_NIBBLE_LEVELS, _D._run_nibble_level),
    **dict.fromkeys(_VISCA_BYTE_LEVELS, _D._run_byte_level),
    **dict.fromkeys(_CGI_SETTINGS, _D._run_cgi_setting),
    **dict.fromkeys(_CGI_ACTIONS, _D._cgi_action),
    **dict.fromkeys(_COMPOSITION_PRESET_ACTIONS, _D._composition_preset),
    **dict.fromkeys(_STREAM_URLS, _D._save_stream_url),
    "pt_absolute": _D._pt_absolute,
    "zoom_tele": _D._zoom_drive,
    "zoom_wide": _D._zoom_drive,
    "zoom_direct": _D._zoom_direct,
    "focus_far": _D._focus_drive,
    "focus_near": _D._focus_drive,
    "focus_direct": _D._focus_direct,
    "set_iris": _D._set_iris,
    "set_gain": _D._set_gain,
    "set_exposure_comp_level": _D._set_exposure_comp_level,
    "set_noise_reduction": _D._set_noise_reduction,
    "set_defog": _D._set_defog,
    "set_tally": _D._set_tally,
    "preset_recall": _D._preset_recall,
    "preset_save": _D._preset_save,
    "preset_clear": _D._preset_clear,
    "preset_rename": _D._preset_rename,
    "set_composition": _D._set_composition,
    "composition_preset_save": _D._composition_preset_save,
    "track_subject_at": _D._track_subject_at,
    "save_rtmp_key": _D._save_rtmp_key,
    "factory_reset": _D._factory_reset,
}

# inquiry.cgi parameters that set more than one value, or a picker list.
_CGI_SPECIAL_READS: dict[str, Callable[[Any, str, str], None]] = {
    "PtzAutoFramingAdjustSetting": _D._read_composition,
    "PtzAutoFramingMultiTrackingCurrentTargetNum": _D._read_target_count,
    "PresetName": _D._read_picker,
    "PtzAutoFramingPresetName": _D._read_picker,
    "RtmpStreamKey1Used": _D._read_key_set,
}
del _D


class _LoginRequired(Exception):
    """The camera answered 401 and no web login is entered."""


class _Forbidden(Exception):
    """The camera answered 403: the account entered may not do this."""


class _ViscaProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_data) -> None:
        self._on_data = on_data

    def datagram_received(self, data, addr) -> None:
        try:
            self._on_data(data)
        except Exception:
            log.exception("VISCA datagram handler error")
