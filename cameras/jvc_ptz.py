"""
OpenAVC JVC PTZ Camera Driver (JVC Web API).

Controls JVC's KY-PZ510, KY-PZ400N and KY-PZ200N / KY-PZ200 PTZ cameras
through the JVC Camcorder Web API the camera serves on port 80: pan, tilt and
zoom with live position read-back, presets and home, focus, exposure and white
balance, picture adjustments, flip and mirror, tally, starting and stopping the
live stream, the on-screen menu, and on the KY-PZ510 its auto tracking (on and
off, mode, framing and the positions each mode uses). The camera's RTSP stream
is offered to a panel as a preview.

Protocol
--------
"JVC Camcorder Web API Reference" version 1.21 (JVCKENWOOD, linked from every
KY-PZ product page). The client signs in with HTTP Digest (user ``jvc``) on
``GET /api.php``; the camera answers ``302`` with a ``SessionID`` cookie. Every
command is then ``POST /cgi-bin/api.cgi`` carrying compact JSON (no spaces or
line breaks, which the camera refuses) under
``application/x-www-form-urlencoded``::

    {"Request":{"Command":"GetCamStatus","SessionID":"<id>","Params":{...}}}

and the camera answers ``{"Response":{"Requested":..,"Result":..,"Data":{..}}}``
with ``Result`` one of Success, DisableError, RequestedError, FormatError,
CommandError, SessionError, DualExeError or Failed, always as HTTP 200. A
``SessionError`` signs in again once and repeats the request; a session refused
straight after a fresh sign-in, or a 401 on the sign-in, is a refused password.
Section 2.1 says a session ends after 30 seconds and must be renewed with
SessionRenewal; section 3.2.2 says renewal is obsolete and must not be used.
This driver never renews and signs in again whenever the camera says so.

Push vs poll
------------
Poll only: the document describes no event or subscription mechanism.
``GetCamStatusMinimum`` (exposure, iris, shutter, gain, white balance, focus,
zoom, streaming, tally and tracking) and ``GetPTPosition`` are read every
``poll_interval`` (default 3 s; the document asks for no more than one status
read every 500 ms). The full ``GetCamStatus`` (picture adjustments, flip,
mirror, pan/tilt options), the auto tracking settings and the RTSP settings
are read every 30 s, and straight after any change this driver makes to them.
A preset recall reads the full status too: presets also hold exposure, colour
and picture settings (firmware 3.0.12 on the KY-PZ510, 1.2.46 / 1.1.53 on the
KY-PZ400 / KY-PZ200).

Why Python
----------
The sign-in has no declarative form: Digest on one page answered with a
session cookie whose value then rides inside every JSON body. One status reply
fans out into about thirty values across nested objects, and the camera
refuses a request inside a 200 body (``Result``), which a YAML HTTP driver
cannot read as a refusal. The command and setting surface narrows to the model
the camera reports (auto tracking is KY-PZ510 only; the KY-PZ200 has no tally
lamp), and a setting the camera acknowledges is reported done only once the
camera reports the new value.

What the document leaves open (no unit has been on the bench)
--------------------------------------------------------------
* Power and standby: only the VISCA "Standard Command List" has power on and
  off, and no document gives the network port that protocol listens on, so this
  driver has no power command.
* The Digest realm on a KY-PZ camera (the document's example is a GY-HM650,
  whose realm is its model), so discovery uses hostname hints only.
* The pan and tilt ranges are the document's (pan 0-35080 with 17540 at the
  front, tilt 0-12080 with 9040 level); the KY-PZ510 manual gives +/-162.5
  degrees of pan and -30 to +90 of tilt.
* Section 2.1 says a second client cannot use the API while one is signed in;
  revision 1.06's note says it can. Not handled specially.
* Switching the tracked person: JoyStickOperation "switches the tracking target
  by pan (left or right)" while tracking is on; this driver sends one short
  pan and a stop.

Source:
    JVC Camcorder Web API Reference 1.21 (JVCKENWOOD Corporation):
    https://jasc.jvc.com/files/prod/3978/5/JvcCamcorderApiReferenceV121_public.pdf
"""

from __future__ import annotations

import asyncio
import json
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

USERNAME = "jvc"  # fixed on these cameras (instructions, System > User)
API_PATH = "/cgi-bin/api.cgi"
LOGIN_PATH = "/api.php"

SLOW_INTERVAL_S = 30.0
# A change is read back after this pause, and once more after the second if
# the camera does not report it yet.
READ_BACK_DELAY_S = 0.4
VERIFY_RETRY_S = 1.5
# A cached "this button is disabled" flag older than this is read again before
# a command is refused for it.
ENABLE_FRESH_S = 0.6
# How long the short pan that switches the tracked person lasts.
TARGET_SWITCH_S = 0.3

KNOWN_MODELS = ("PZ510", "PZ400", "PZ200")
TRACKING_MODELS = ("PZ510",)
TALLY_MODELS = ("PZ400", "PZ510")

# The camera's words -> this driver's state values.
_ON_OFF = {"On": True, "Off": False}
_AUTO_MANUAL = {"Auto": "auto", "Manual": "manual"}
_EXPOSURE = {
    "Auto": "auto", "Manual": "manual", "IrisPriority": "iris_priority",
    "ShutterPriority": "shutter_priority", "Bright": "bright",
}
_WB = {"Auto": "auto", "Indoor": "indoor", "Outdoor": "outdoor", "OnePush": "one_push", "Manual": "manual"}
_STREAMING = {
    "Stop": "stopped", "Stopping": "stopping", "Start": "streaming",
    "Starting": "starting", "Waiting": "waiting", "Error": "error",
}
_STUDIO_TALLY = {"Off": "off", "Program": "program", "Preview": "preview"}
_TRACKING_MODE = {
    "Standard": "standard", "Area": "area", "Stage": "stage",
    "WideArea": "wide_area", "FineAdjustment": "fine_adjustment",
}
_BOUNDING_BOX = {"Auto": "auto", "On": "on", "Off": "off"}
_FIGURE_SIZE = {"Full": "full", "Upper": "upper", "Close": "close", "Custom": "custom"}
_HORIZONTAL = {"Left": "left", "Center": "center", "Right": "right"}
_START_POSITION = {
    "CurrentPosition": "current_position", "HomePosition": "home_position",
    "Preset1": "preset_1", "W1": "w1", "W2": "w2",
}
_STARTING_AREA = {"A1": "a1", "A2": "a2", "A3": "a3", "A4": "a4"}
_RETENTION = {5: "5", 8: "8", 10: "10", 12: "12", 15: "15"}

# Status reply path -> (state key, how it is read). Read from
# GetCamStatusMinimum (fast) and GetCamStatus (slow); a path absent from a
# reply leaves its key alone.
_STATUS_READS: dict[tuple[str, ...], tuple[str, Any]] = {
    ("Camera", "VideoOutputStatus"): ("video_output", _ON_OFF),
    ("Camera", "MenuStatus"): ("menu_open", _ON_OFF),
    ("Exposure", "Status"): ("exposure_mode", _EXPOSURE),
    ("Iris", "Status"): ("iris_mode", _AUTO_MANUAL),
    ("Iris", "Value"): ("iris", "str"),
    ("Shutter", "Status"): ("shutter_mode", _AUTO_MANUAL),
    ("Shutter", "Value"): ("shutter", "str"),
    ("Gain", "Status"): ("gain_mode", _AUTO_MANUAL),
    ("Gain", "Value"): ("gain", "str"),
    ("Whb", "Status"): ("wb_mode", _WB),
    ("Whb", "Value"): ("wb_value", "str"),
    ("Whb", "WhPRPosition"): ("wb_red_paint", "int"),
    ("Whb", "WhPBPosition"): ("wb_blue_paint", "int"),
    ("Focus", "Status"): ("focus_mode", _AUTO_MANUAL),
    ("Focus", "Value"): ("focus", "str"),
    ("Zoom", "Position"): ("zoom_position", "int"),
    ("Zoom", "DisplayValue"): ("zoom_display", "str"),
    ("Bright", "Value"): ("bright", "str"),
    ("Streaming", "Status"): ("streaming_status", _STREAMING),
    ("TallyLamp", "Lighting"): ("tally_lamp", _ON_OFF),
    ("TallyLamp", "StudioTally"): ("studio_tally", _STUDIO_TALLY),
    ("Tracking", "Status"): ("tracking", _ON_OFF),
    ("TrackingMode", "Status"): ("tracking_mode", _TRACKING_MODE),
    ("TrackingBoundingBox", "Status"): ("tracking_bounding_box", _BOUNDING_BOX),
    ("Luminance", "Value"): ("luminance", "int"),
    ("Saturation", "Value"): ("saturation", "int"),
    ("Contrast", "Value"): ("contrast", "int"),
    ("Sharpness", "Value"): ("sharpness", "int"),
    ("Hue", "Value"): ("hue", "int"),
    ("Flip", "Status"): ("flip", _ON_OFF),
    ("Mirror", "Status"): ("mirror", _ON_OFF),
    ("PanTilt", "SpeedWithZoom"): ("speed_with_zoom", _ON_OFF),
    ("PanTilt", "PresetSpeed"): ("preset_speed", "int"),
}

# The tracking settings (KY-PZ510). JVC's tracking manual: a setting with the
# same name is one setting shared by every tracking mode, so each is written
# and read through one mode's commands. Setting -> (mode, field, how it reads).
_TRACKING_SETTINGS: dict[str, tuple[str, str, Any]] = {
    "tracking_auto_zoom": ("WideArea", "AutoZoom", _ON_OFF),
    "tracking_auto_tilt": ("WideArea", "AutoTilt", _ON_OFF),
    "tracking_retention": ("WideArea", "TargetRetentionTime", _RETENTION),
    "tracking_figure_size": ("WideArea", "FigureSize", _FIGURE_SIZE),
    "tracking_figure_custom": ("WideArea", "FigureSizeCustom", "int"),
    "tracking_vertical": ("WideArea", "VerticalPosition", "int"),
    "tracking_horizontal": ("WideArea", "HorizontalPosition", _HORIZONTAL),
    "tracking_start_position": ("WideArea", "StartPosition", _START_POSITION),
    "tracking_starting_area": ("Area", "StartingArea", _STARTING_AREA),
    "tracking_duration": ("FineAdjustment", "DurationTime", "int"),
}
_TRACKING_READS = ("WideArea", "Area", "FineAdjustment")

# Button commands: command -> (Kind, Button, the label a refusal names, the
# status read that carries its Enable flag, or None when it has none).
_BUTTONS: dict[str, tuple[str, str, str, str | None]] = {
    "iris_open": ("Iris", "Open1", "Iris Open", "minimum"),
    "iris_close": ("Iris", "Close1", "Iris Close", "minimum"),
    "gain_up": ("Gain", "Up1", "Gain Up", "minimum"),
    "gain_down": ("Gain", "Down1", "Gain Down", "minimum"),
    "shutter_slower": ("Shutter", "Slower", "Shutter Slower", "minimum"),
    "shutter_faster": ("Shutter", "Faster", "Shutter Faster", "minimum"),
    "bright_up": ("Bright", "Up", "Bright Up", "minimum"),
    "bright_down": ("Bright", "Down", "Bright Down", "minimum"),
    "luminance_up": ("Luminance", "Up", "Luminance Up", "full"),
    "luminance_down": ("Luminance", "Down", "Luminance Down", "full"),
    "saturation_up": ("Saturation", "Up", "Saturation Up", "full"),
    "saturation_down": ("Saturation", "Down", "Saturation Down", "full"),
    "contrast_up": ("Contrast", "Up", "Contrast Up", "full"),
    "contrast_down": ("Contrast", "Down", "Contrast Down", "full"),
    "sharpness_up": ("Sharpness", "Up", "Sharpness Up", "full"),
    "sharpness_down": ("Sharpness", "Down", "Sharpness Down", "full"),
    "hue_up": ("Hue", "Up", "Hue Up", "full"),
    "hue_down": ("Hue", "Down", "Hue Down", "full"),
    "focus_one_push": ("Focus", "PushAuto", "One Push Auto Focus", "full"),
    "focus_stop": ("Focus", "Stop", "Focus Stop", None),
    "menu_toggle": ("Menu", "Menu", "Menu", None),
    "menu_enter": ("Menu", "Set", "Menu Enter", None),
    "menu_back": ("Menu", "Cancel", "Menu Back", None),
    "menu_up": ("Menu", "Up", "Menu Up", None),
    "menu_down": ("Menu", "Down", "Menu Down", None),
    "menu_left": ("Menu", "Left", "Menu Left", None),
    "menu_right": ("Menu", "Right", "Menu Right", None),
}
# The Enable flag that gates a button, where it is named differently from the
# button (GetCamStatusMinimum names One Push White Balance's flag AwbTrigger).
_ENABLE_ALIASES = {("Whb", "Adjust"): ("Whb", "AwbTrigger")}

# Jog commands -> (pan direction, tilt direction).
_JOG = {
    "pt_up": ("Stop", "Up"),
    "pt_down": ("Stop", "Down"),
    "pt_left": ("Left", "Stop"),
    "pt_right": ("Right", "Stop"),
    "pt_up_left": ("Left", "Up"),
    "pt_up_right": ("Right", "Up"),
    "pt_down_left": ("Left", "Down"),
    "pt_down_right": ("Right", "Down"),
}

# Model narrowing: command or setting -> the models that have it.
_MODEL_ONLY: dict[str, tuple[str, ...]] = {
    "tally_on": TALLY_MODELS,
    "tally_off": TALLY_MODELS,
    "tally_program": TALLY_MODELS,
    "tally_preview": TALLY_MODELS,
    "tally_clear": TALLY_MODELS,
    "tracking_on": TRACKING_MODELS,
    "tracking_off": TRACKING_MODELS,
    "tracking_target_left": TRACKING_MODELS,
    "tracking_target_right": TRACKING_MODELS,
    "tracking_area_set": TRACKING_MODELS,
    "tracking_area_delete": TRACKING_MODELS,
    "tracking_stage_set": TRACKING_MODELS,
    "tracking_stage_delete": TRACKING_MODELS,
    "tracking_wide_set": TRACKING_MODELS,
    "tracking_wide_delete": TRACKING_MODELS,
    "tracking_mode": TRACKING_MODELS,
    "tracking_bounding_box": TRACKING_MODELS,
    **{key: TRACKING_MODELS for key in _TRACKING_SETTINGS},
}

_MODEL_NAMES = {"PZ510": "KY-PZ510", "PZ400": "KY-PZ400", "PZ200": "KY-PZ200"}

# What the camera's Result words mean, for a refusal.
_RESULT_SENTENCES = {
    "DisableError": "it can't do that right now",
    "RequestedError": "it doesn't accept that value",
    "FormatError": "it couldn't read the request",
    "CommandError": "this camera doesn't have that command",
    "DualExeError": "it is still carrying out the previous command",
    "Failed": "it couldn't carry it out",
}


class CameraRefused(Exception):
    """The camera answered and said no: a Result other than Success, or an
    HTTP status other than 200."""

    def __init__(self, message: str, *, result: str = "") -> None:
        super().__init__(message)
        self.result = result


def _dig(data: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def _read(raw: Any, kind: Any) -> Any:
    """One reported value as this driver states it, or None when it is not one
    the driver knows."""
    if raw is None:
        return None
    if isinstance(kind, dict):
        if isinstance(raw, str):
            return kind.get(raw.strip())
        return kind.get(raw)
    if kind == "int":
        try:
            return int(str(raw).strip())
        except ValueError:
            return None
    return str(raw)


def _word(value: Any, kind: dict) -> Any:
    """This driver's value -> the camera's word for it (the inverse of _read)."""
    for word, mine in kind.items():
        if mine == value:
            return word
    raise ValueError(f"{value!r} is not one of {sorted(str(v) for v in kind.values())}")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "on", "yes")


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _compact(body: dict) -> bytes:
    """The request body as the camera takes it: JSON with no whitespace."""
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class JVCPTZDriver(BaseDriver):
    """JVC KY-PZ PTZ cameras over the JVC Camcorder Web API."""

    DRIVER_INFO = {
        "id": "jvc_ptz",
        "name": "JVC PTZ Camera",
        "manufacturer": "JVC",
        "category": "camera",
        "version": "1.0.0",
        "min_platform_version": "0.36.0",
        "author": "OpenAVC",
        "description": (
            "Controls JVC KY-PZ510, KY-PZ400N and KY-PZ200 PTZ cameras over the "
            "JVC Web API: pan, tilt and zoom with live position read-back, presets "
            "and home, focus, exposure, white balance and picture adjustments, "
            "tally, live stream start and stop, the on-screen menu, the KY-PZ510's "
            "auto tracking, and the camera's RTSP stream as a panel preview."
        ),
        "source_url": "https://jasc.jvc.com/files/prod/3978/5/JvcCamcorderApiReferenceV121_public.pdf",
        "tags": ["ptz", "camera", "jvc", "auto-tracking", "tally", "rtsp", "ndi"],
        "verified": False,
        "simulated": True,
        "protocols": ["jvc_web_api"],
        "ports": [80],
        "transport": "http",
        "discovery": {
            # The ZeroConfig host name prefix (shared with the NDI name) is
            # PZ510, PZ400 or PZ200 from the factory (the instructions,
            # Network > ZeroConfig). No probe: every API page needs a sign-in,
            # and the document gives no KY-PZ Digest realm to match.
            "hostname": ["^PZ(510|400|200)"],
            "manufacturer_alias": ["jvc", "jvckenwood", "jvc kenwood"],
        },
        "compatible_models": [
            {
                "manufacturer": "JVC",
                "models": [
                    "KY-PZ510NWU", "KY-PZ510NBU", "KY-PZ510NWE", "KY-PZ510NBE",
                    "KY-PZ510WU", "KY-PZ510BU", "KY-PZ510WE", "KY-PZ510BE",
                ],
                "confidence": "untested",
                "notes": (
                    "Every command in JVC's Web API 1.21 for the KY-PZ510, auto "
                    "tracking included (firmware 3.0.12 or later). Power and standby "
                    "are not available through this API. Not yet tested on this hardware."
                ),
            },
            {
                "manufacturer": "JVC",
                "models": ["KY-PZ400NWU", "KY-PZ400NBU", "KY-PZ400NWE", "KY-PZ400NBE"],
                "confidence": "untested",
                "notes": (
                    "As the KY-PZ510 without auto tracking. Power and standby are not "
                    "available through this API. Not yet tested on this hardware."
                ),
            },
            {
                "manufacturer": "JVC",
                "models": [
                    "KY-PZ200NWU", "KY-PZ200NBU", "KY-PZ200NWE", "KY-PZ200NBE",
                    "KY-PZ200WU", "KY-PZ200BU", "KY-PZ200WE", "KY-PZ200BE",
                ],
                "confidence": "untested",
                "notes": (
                    "As the KY-PZ400N without a tally lamp: tally is reported but not "
                    "controlled. Power and standby are not available through this API. "
                    "Not yet tested on this hardware."
                ),
            },
        ],
        "help": {
            "overview": (
                "Controls a JVC KY-PZ PTZ camera over its web API: pan, tilt and zoom, "
                "presets and the home position, focus, exposure, white balance and "
                "picture adjustments, tally, and starting and stopping the live "
                "stream. On a KY-PZ510, auto tracking is turned on and off, its mode "
                "picked, and its framing and positions set from here. The live pan, "
                "tilt and zoom positions are read back from the camera, and its RTSP "
                "stream is offered to the Video Panel as a preview. The camera's power "
                "and standby can't be controlled through its web API."
            ),
            "setup": (
                "1. Find the camera's IP address on its front LCD (192.168.0.10 from "
                "the factory).\n"
                "2. Open that address in a web browser and sign in as jvc with the "
                "factory password 0000; the camera asks for a new password. Set one.\n"
                "3. Enter the camera's IP address and that password here. Leave the "
                "port at 80 unless the camera's HTTP port was changed (Network > Port "
                "Settings).\n"
                "4. To show the camera on a panel, leave RTSP Auth off on the "
                "camera's Network page."
            ),
        },
        "default_config": {
            "host": "",
            "port": 80,
            "password": "",
            "model": "",
            "pt_speed": 15,
            "zoom_speed": 4,
            "focus_speed": 1,
            "poll_interval": 3,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {
                "type": "integer", "default": 80, "min": 1, "max": 65535, "label": "Port",
                "description": "The camera's HTTP port: 80 unless changed under Network > Port Settings.",
            },
            "password": {
                "type": "string", "default": "", "required": True, "label": "Password",
                "secret": True,
                "description": "The camera's web password (the user name is always jvc).",
            },
            "model": {
                "type": "enum", "default": "", "label": "Model",
                "values": [
                    {"value": "", "label": "Detect automatically"},
                    {"value": "PZ510", "label": "KY-PZ510"},
                    {"value": "PZ400", "label": "KY-PZ400N"},
                    {"value": "PZ200", "label": "KY-PZ200N / KY-PZ200"},
                ],
                "description": (
                    "The commands to offer before the camera has been reached, and "
                    "the model a simulated camera plays. Once connected, the model "
                    "the camera reports is used."
                ),
            },
            "pt_speed": {
                "type": "integer", "default": 15, "min": 1, "max": 30, "label": "Pan/Tilt Speed",
                "description": "Speed for pan and tilt moves that do not give their own (1 slowest, 30 fastest).",
            },
            "zoom_speed": {
                "type": "integer", "default": 4, "min": 1, "max": 8, "label": "Zoom Speed",
                "description": "Speed for zoom moves that do not give their own (1 slowest, 8 fastest).",
            },
            "focus_speed": {
                "type": "integer", "default": 1, "min": 1, "max": 3, "label": "Focus Speed",
                "description": "Speed for manual focus moves that do not give their own (1 slowest, 3 fastest).",
            },
            "poll_interval": {
                "type": "integer", "default": 3, "min": 0, "label": "Poll Interval (sec)",
                "description": (
                    "How often exposure, focus, zoom, position, tally and tracking "
                    "are read. Picture and tracking settings are read every 30 "
                    "seconds. 0 stops polling."
                ),
            },
        },
        "state_variables": {
            "model": {"type": "string", "label": "Model", "help": "As the camera reports it: PZ510, PZ400 or PZ200."},
            "region": {"type": "string", "label": "Region", "help": "The camera's destination code (US, EU, JP and so on)."},
            "api_version": {"type": "string", "label": "API Version"},
            "serial_number": {"type": "string", "label": "Serial Number"},
            "video_output": {"type": "boolean", "label": "Video Output"},
            "menu_open": {"type": "boolean", "label": "On-Screen Menu Open"},
            "pan_position": {
                "type": "integer", "label": "Pan Position", "min": 0, "max": 35080,
                "help": "0 is the back-left limit, 17540 the front, 35080 the back-right limit.",
                "cloud_priority": "low",
            },
            "tilt_position": {
                "type": "integer", "label": "Tilt Position", "min": 0, "max": 12080,
                "help": "0 is straight up, 9040 level, 12080 the lowest the camera tilts.",
                "cloud_priority": "low",
            },
            "zoom_position": {
                "type": "integer", "label": "Zoom Position", "min": 0, "max": 499,
                "help": "0 is fully wide, 499 fully zoomed in.",
                "control": True,
                "cloud_priority": "low",
            },
            "zoom_display": {"type": "string", "label": "Zoom", "cloud_priority": "low"},
            "focus_mode": {"type": "enum", "values": ["auto", "manual"], "label": "Focus Mode", "control": True},
            "focus": {"type": "string", "label": "Focus", "cloud_priority": "low"},
            "exposure_mode": {
                "type": "enum",
                "values": ["auto", "manual", "iris_priority", "shutter_priority", "bright"],
                "label": "Exposure Mode",
                "control": True,
            },
            "iris_mode": {"type": "enum", "values": ["auto", "manual"], "label": "Iris Mode"},
            "iris": {"type": "string", "label": "Iris"},
            "shutter_mode": {"type": "enum", "values": ["auto", "manual"], "label": "Shutter Mode"},
            "shutter": {"type": "string", "label": "Shutter"},
            "gain_mode": {"type": "enum", "values": ["auto", "manual"], "label": "Gain Mode"},
            "gain": {"type": "string", "label": "Gain"},
            "bright": {"type": "string", "label": "Bright Level"},
            "wb_mode": {
                "type": "enum",
                "values": ["auto", "indoor", "outdoor", "one_push", "manual"],
                "label": "White Balance Mode",
                "control": True,
            },
            "wb_value": {"type": "string", "label": "White Balance"},
            "wb_red_paint": {"type": "integer", "label": "White Paint Red", "min": 0, "max": 255, "step": 1},
            "wb_blue_paint": {"type": "integer", "label": "White Paint Blue", "min": 0, "max": 255, "step": 1},
            "luminance": {"type": "integer", "label": "Luminance"},
            "saturation": {"type": "integer", "label": "Saturation"},
            "contrast": {"type": "integer", "label": "Contrast"},
            "sharpness": {"type": "integer", "label": "Sharpness"},
            "hue": {"type": "integer", "label": "Hue"},
            "flip": {"type": "boolean", "label": "Flip"},
            "mirror": {"type": "boolean", "label": "Mirror"},
            "speed_with_zoom": {"type": "boolean", "label": "Speed With Zoom"},
            "preset_speed": {"type": "integer", "label": "Preset Speed", "min": 1, "max": 30},
            "streaming_status": {
                "type": "enum",
                "values": ["stopped", "stopping", "streaming", "starting", "waiting", "error"],
                "label": "Live Stream",
                "help": "waiting: an RTSP or SRT listener stream waiting for a receiver to connect.",
                "control": True,
                "cloud_priority": "high",
            },
            "tally_lamp": {"type": "boolean", "label": "Tally Lamp", "control": True, "cloud_priority": "high"},
            "studio_tally": {
                "type": "enum", "values": ["off", "program", "preview"], "label": "Studio Tally",
                "help": "program lights the tally lamp red, preview green.",
                "control": True,
                "cloud_priority": "high",
            },
            "tracking": {"type": "boolean", "label": "Auto Tracking", "control": True, "cloud_priority": "high"},
            "tracking_mode": {
                "type": "enum",
                "values": ["standard", "area", "stage", "wide_area", "fine_adjustment"],
                "label": "Tracking Mode",
                "control": True,
            },
            "tracking_bounding_box": {"type": "enum", "values": ["auto", "on", "off"], "label": "Tracking Bounding Box"},
            "tracking_auto_zoom": {"type": "boolean", "label": "Tracking Auto Zoom"},
            "tracking_auto_tilt": {"type": "boolean", "label": "Tracking Auto Tilt"},
            "tracking_retention": {"type": "enum", "values": ["5", "8", "10", "12", "15"], "label": "Target Retention Time"},
            "tracking_figure_size": {"type": "enum", "values": ["full", "upper", "close", "custom"], "label": "Tracking Figure Size"},
            "tracking_figure_custom": {"type": "integer", "label": "Custom Figure Size", "min": 80, "max": 290},
            "tracking_vertical": {"type": "integer", "label": "Tracking Vertical Position", "min": -5, "max": 5},
            "tracking_horizontal": {"type": "enum", "values": ["left", "center", "right"], "label": "Tracking Horizontal Position"},
            "tracking_start_position": {
                "type": "enum",
                "values": ["current_position", "home_position", "preset_1", "w1", "w2"],
                "label": "Tracking Start Position",
            },
            "tracking_starting_area": {"type": "enum", "values": ["a1", "a2", "a3", "a4"], "label": "Starting Area"},
            "tracking_duration": {"type": "integer", "label": "Tracking Duration Time", "min": 1, "max": 6},
            "preview_url": {"type": "string", "label": "Preview URL"},
            "preview_format": {"type": "string", "label": "Preview Format"},
            "preview_status": {"type": "string", "label": "Preview Status"},
            "preview_status_detail": {"type": "string", "label": "Preview Status Detail"},
        },
        "commands": {
            "pt_up": {
                "label": "Tilt Up", "help": "Tilts the camera up until Pan/Tilt Stop.",
                "params": {"speed": {"type": "integer", "min": 1, "max": 30, "label": "Speed"}},
            },
            "pt_down": {
                "label": "Tilt Down", "help": "Tilts the camera down until Pan/Tilt Stop.",
                "params": {"speed": {"type": "integer", "min": 1, "max": 30, "label": "Speed"}},
            },
            "pt_left": {
                "label": "Pan Left",
                "help": "Pans the camera left until Pan/Tilt Stop. While auto tracking is on, the camera tracks the next person to the left instead.",
                "params": {"speed": {"type": "integer", "min": 1, "max": 30, "label": "Speed"}},
            },
            "pt_right": {
                "label": "Pan Right",
                "help": "Pans the camera right until Pan/Tilt Stop. While auto tracking is on, the camera tracks the next person to the right instead.",
                "params": {"speed": {"type": "integer", "min": 1, "max": 30, "label": "Speed"}},
            },
            "pt_up_left": {
                "label": "Pan Up-Left", "help": "Moves the camera up and left until Pan/Tilt Stop.",
                "params": {"speed": {"type": "integer", "min": 1, "max": 30, "label": "Speed"}},
            },
            "pt_up_right": {
                "label": "Pan Up-Right", "help": "Moves the camera up and right until Pan/Tilt Stop.",
                "params": {"speed": {"type": "integer", "min": 1, "max": 30, "label": "Speed"}},
            },
            "pt_down_left": {
                "label": "Pan Down-Left", "help": "Moves the camera down and left until Pan/Tilt Stop.",
                "params": {"speed": {"type": "integer", "min": 1, "max": 30, "label": "Speed"}},
            },
            "pt_down_right": {
                "label": "Pan Down-Right", "help": "Moves the camera down and right until Pan/Tilt Stop.",
                "params": {"speed": {"type": "integer", "min": 1, "max": 30, "label": "Speed"}},
            },
            "pt_stop": {"label": "Pan/Tilt Stop", "help": "Stops pan and tilt movement.", "params": {}},
            "pt_home": {
                "label": "Home",
                "help": "Returns the camera to its home pan and tilt position.",
                "params": {"speed": {"type": "integer", "min": 1, "max": 30, "label": "Speed"}},
            },
            "pt_position": {
                "label": "Pan/Tilt To Position",
                "help": "Moves the camera to a pan and tilt position: pan 17540 faces front, tilt 9040 is level.",
                "params": {
                    "pan": {"type": "integer", "required": True, "min": 0, "max": 35080, "label": "Pan"},
                    "tilt": {"type": "integer", "required": True, "min": 0, "max": 12080, "label": "Tilt"},
                    "speed": {"type": "integer", "min": 1, "max": 30, "label": "Speed"},
                },
                "sets": {"pan_position": "{pan}", "tilt_position": "{tilt}"},
            },
            "zoom_in": {
                "label": "Zoom In", "help": "Zooms in (tele) until Zoom Stop.",
                "params": {"speed": {"type": "integer", "min": 1, "max": 8, "label": "Speed"}},
            },
            "zoom_out": {
                "label": "Zoom Out", "help": "Zooms out (wide) until Zoom Stop.",
                "params": {"speed": {"type": "integer", "min": 1, "max": 8, "label": "Speed"}},
            },
            "zoom_stop": {"label": "Zoom Stop", "help": "Stops zooming.", "params": {}},
            "zoom_direct": {
                "label": "Zoom To Position",
                "help": "Zooms to a position at the camera's preset zoom speed: 0 is fully wide, 499 fully zoomed in.",
                "params": {
                    "position": {"type": "integer", "required": True, "min": 0, "max": 499, "label": "Zoom Position"},
                },
                "sets": {"zoom_position": "{position}"},
            },
            "focus_auto": {
                "label": "Auto Focus", "help": "Turns auto focus on.", "params": {},
                "sets": {"focus_mode": "auto"},
            },
            "focus_manual": {
                "label": "Manual Focus", "help": "Turns auto focus off so focus holds where it is.", "params": {},
                "sets": {"focus_mode": "manual"},
            },
            "focus_near": {
                "label": "Focus Near", "help": "Moves focus nearer until Focus Stop (manual focus only).",
                "params": {"speed": {"type": "integer", "min": 1, "max": 3, "label": "Speed"}},
            },
            "focus_far": {
                "label": "Focus Far", "help": "Moves focus farther until Focus Stop (manual focus only).",
                "params": {"speed": {"type": "integer", "min": 1, "max": 3, "label": "Speed"}},
            },
            "focus_stop": {"label": "Focus Stop", "help": "Stops a manual focus move.", "params": {}},
            "focus_one_push": {
                "label": "One Push Auto Focus",
                "help": "Focuses once on the current picture, then holds (manual focus only).",
                "params": {},
            },
            "preset_recall": {
                "label": "Recall Preset",
                "help": "Moves the camera to a stored preset, with the picture settings stored in it.",
                "params": {"preset": {"type": "integer", "required": True, "min": 1, "max": 255, "label": "Preset"}},
            },
            "preset_save": {
                "label": "Save Preset",
                "help": (
                    "Stores the camera's position and picture settings as a preset, replacing "
                    "what was there. On a KY-PZ510, presets 187 to 195 hold the auto tracking "
                    "positions."
                ),
                "params": {"preset": {"type": "integer", "required": True, "min": 1, "max": 255, "label": "Preset"}},
            },
            "preset_clear": {
                "label": "Delete Preset",
                "help": "Deletes a stored preset.",
                "confirm": "Deletes the camera position and settings stored in this preset.",
                "params": {"preset": {"type": "integer", "required": True, "min": 1, "max": 255, "label": "Preset"}},
            },
            "iris_open": {"label": "Iris Open", "help": "Opens the iris one step (manual or iris-priority exposure).", "params": {}},
            "iris_close": {"label": "Iris Close", "help": "Closes the iris one step (manual or iris-priority exposure).", "params": {}},
            "gain_up": {"label": "Gain Up", "help": "Raises the gain one step (manual exposure).", "params": {}},
            "gain_down": {"label": "Gain Down", "help": "Lowers the gain one step (manual exposure).", "params": {}},
            "shutter_slower": {"label": "Shutter Slower", "help": "Makes the shutter one step slower (manual or shutter-priority exposure).", "params": {}},
            "shutter_faster": {"label": "Shutter Faster", "help": "Makes the shutter one step faster (manual or shutter-priority exposure).", "params": {}},
            "bright_up": {"label": "Bright Up", "help": "Brightens the picture one step (Bright exposure).", "params": {}},
            "bright_down": {"label": "Bright Down", "help": "Darkens the picture one step (Bright exposure).", "params": {}},
            "wb_one_push": {
                "label": "One Push White Balance",
                "help": "Sets white balance once from what the camera sees now (One Push white balance mode).",
                "params": {},
            },
            "luminance_up": {"label": "Luminance Up", "help": "Raises the picture's luminance one step.", "params": {}},
            "luminance_down": {"label": "Luminance Down", "help": "Lowers the picture's luminance one step.", "params": {}},
            "saturation_up": {"label": "Saturation Up", "help": "Raises colour saturation one step.", "params": {}},
            "saturation_down": {"label": "Saturation Down", "help": "Lowers colour saturation one step.", "params": {}},
            "contrast_up": {"label": "Contrast Up", "help": "Raises contrast one step.", "params": {}},
            "contrast_down": {"label": "Contrast Down", "help": "Lowers contrast one step.", "params": {}},
            "sharpness_up": {"label": "Sharpness Up", "help": "Raises sharpness one step.", "params": {}},
            "sharpness_down": {"label": "Sharpness Down", "help": "Lowers sharpness one step.", "params": {}},
            "hue_up": {"label": "Hue Up", "help": "Shifts the hue one step up.", "params": {}},
            "hue_down": {"label": "Hue Down", "help": "Shifts the hue one step down.", "params": {}},
            "tally_on": {
                "label": "Tally On", "help": "Lights the camera's tally lamp (the camera's Tally Mode menu item must be on).",
                "params": {}, "sets": {"tally_lamp": True},
            },
            "tally_off": {
                "label": "Tally Off", "help": "Turns the camera's tally lamp off.",
                "params": {}, "sets": {"tally_lamp": False},
            },
            "tally_program": {
                "label": "Tally Program", "help": "Shows the camera as on program: the tally lamp lights red.",
                "params": {}, "sets": {"studio_tally": "program"},
            },
            "tally_preview": {
                "label": "Tally Preview", "help": "Shows the camera as on preview: the tally lamp lights green.",
                "params": {}, "sets": {"studio_tally": "preview"},
            },
            "tally_clear": {
                "label": "Tally Clear", "help": "Clears program and preview tally: the lamp goes off.",
                "params": {}, "sets": {"studio_tally": "off"},
            },
            "streaming_start": {
                "label": "Start Live Stream",
                "help": "Starts the camera's live stream with its current streaming settings.",
                "params": {},
            },
            "streaming_stop": {"label": "Stop Live Stream", "help": "Stops the camera's live stream.", "params": {}},
            "menu_toggle": {"label": "Menu", "help": "Opens or closes the camera's on-screen menu.", "params": {}},
            "menu_up": {"label": "Menu Up", "help": "Moves up in the on-screen menu.", "params": {}},
            "menu_down": {"label": "Menu Down", "help": "Moves down in the on-screen menu.", "params": {}},
            "menu_left": {"label": "Menu Left", "help": "Changes the selected menu item's value one way.", "params": {}},
            "menu_right": {"label": "Menu Right", "help": "Changes the selected menu item's value the other way.", "params": {}},
            "menu_enter": {"label": "Menu Enter", "help": "Selects the highlighted menu item.", "params": {}},
            "menu_back": {"label": "Menu Back", "help": "Goes back to the previous menu.", "params": {}},
            "tracking_on": {
                "label": "Auto Tracking On",
                "help": "Starts auto tracking in the current tracking mode. The camera first moves to its tracking start position.",
                "params": {}, "sets": {"tracking": True},
            },
            "tracking_off": {
                "label": "Auto Tracking Off",
                "help": "Stops auto tracking. The camera returns to its tracking start position.",
                "params": {}, "sets": {"tracking": False},
            },
            "tracking_target_left": {
                "label": "Track Person To The Left",
                "help": "While auto tracking is on, switches tracking to the next person to the left.",
                "params": {},
            },
            "tracking_target_right": {
                "label": "Track Person To The Right",
                "help": "While auto tracking is on, switches tracking to the next person to the right.",
                "params": {},
            },
            "tracking_area_set": {
                "label": "Set Tracking Area",
                "help": "Stores the camera's current view as one of Area mode's four areas, left to right as the camera sees them.",
                "params": {
                    "area": {
                        "type": "enum", "required": True, "label": "Area",
                        "values": ["A1", "A2", "A3", "A4"],
                    },
                },
            },
            "tracking_area_delete": {
                "label": "Delete Tracking Area",
                "help": "Deletes one of Area mode's stored areas.",
                "confirm": "Deletes the view stored for this tracking area.",
                "params": {
                    "area": {
                        "type": "enum", "required": True, "label": "Area",
                        "values": ["A1", "A2", "A3", "A4"],
                    },
                },
            },
            "tracking_stage_set": {
                "label": "Set Stage Position",
                "help": "Stores the camera's current view as Stage mode's left edge, right edge or tracking start position.",
                "params": {
                    "position": {
                        "type": "enum", "required": True, "label": "Position",
                        "values": [
                            {"value": "Left", "label": "Left Edge"},
                            {"value": "Right", "label": "Right Edge"},
                            {"value": "Start", "label": "Tracking Start Position"},
                        ],
                    },
                },
            },
            "tracking_stage_delete": {
                "label": "Delete Stage Position",
                "help": "Deletes one of Stage mode's stored positions.",
                "confirm": "Deletes the view stored for this stage position.",
                "params": {
                    "position": {
                        "type": "enum", "required": True, "label": "Position",
                        "values": [
                            {"value": "Left", "label": "Left Edge"},
                            {"value": "Right", "label": "Right Edge"},
                            {"value": "Start", "label": "Tracking Start Position"},
                        ],
                    },
                },
            },
            "tracking_wide_set": {
                "label": "Set Wide Area",
                "help": "Stores the camera's current view as Wide Area mode's W1 (left) or W2 (right).",
                "params": {
                    "area": {"type": "enum", "required": True, "label": "Area", "values": ["W1", "W2"]},
                },
            },
            "tracking_wide_delete": {
                "label": "Delete Wide Area",
                "help": "Deletes one of Wide Area mode's stored views.",
                "confirm": "Deletes the view stored for this wide area.",
                "params": {
                    "area": {"type": "enum", "required": True, "label": "Area", "values": ["W1", "W2"]},
                },
            },
            "reboot": {
                "label": "Reboot",
                "help": "Restarts the camera.",
                "params": {},
                "restarts_device_for": 120,
            },
            "restore_defaults": {
                "label": "Restore Factory Settings",
                "help": "Returns every camera setting to its factory value, the IP address included.",
                "confirm": (
                    "Returns every camera setting to its factory value. The IP address goes "
                    "back to 192.168.0.10, so the camera may leave this network."
                ),
                "params": {},
            },
        },
        "device_settings": {
            "exposure_mode": {
                "type": "enum", "label": "Exposure Mode", "state_key": "exposure_mode",
                "values": [
                    {"value": "auto", "label": "Auto"},
                    {"value": "manual", "label": "Manual"},
                    {"value": "iris_priority", "label": "Iris Priority"},
                    {"value": "shutter_priority", "label": "Shutter Priority"},
                    {"value": "bright", "label": "Bright"},
                ],
                "default": "auto", "setup": False,
                "help": "Manual fixes iris, shutter and gain; iris or shutter priority fixes that one; Bright sets one brightness level.",
            },
            "wb_mode": {
                "type": "enum", "label": "White Balance Mode", "state_key": "wb_mode",
                "values": [
                    {"value": "auto", "label": "Auto"},
                    {"value": "indoor", "label": "Indoor"},
                    {"value": "outdoor", "label": "Outdoor"},
                    {"value": "one_push", "label": "One Push"},
                    {"value": "manual", "label": "Manual"},
                ],
                "default": "auto", "setup": False,
                "help": "Manual uses White Paint Red and Blue; One Push uses One Push White Balance.",
            },
            "wb_red_paint": {
                "type": "integer", "label": "White Paint Red", "state_key": "wb_red_paint",
                "min": 0, "max": 255, "default": 10, "setup": False,
                "help": "Red white paint: up to 255 in manual white balance, up to 20 in auto.",
            },
            "wb_blue_paint": {
                "type": "integer", "label": "White Paint Blue", "state_key": "wb_blue_paint",
                "min": 0, "max": 255, "default": 10, "setup": False,
                "help": "Blue white paint: up to 255 in manual white balance, up to 20 in auto.",
            },
            "flip": {"type": "boolean", "label": "Flip", "state_key": "flip", "default": False, "setup": False},
            "mirror": {"type": "boolean", "label": "Mirror", "state_key": "mirror", "default": False, "setup": False},
            "speed_with_zoom": {
                "type": "boolean", "label": "Speed With Zoom", "state_key": "speed_with_zoom",
                "default": False, "setup": False,
                "help": "Slows pan and tilt as the camera zooms in.",
            },
            "tracking_mode": {
                "type": "enum", "label": "Tracking Mode", "state_key": "tracking_mode",
                "values": [
                    {"value": "standard", "label": "Standard"},
                    {"value": "area", "label": "Area"},
                    {"value": "stage", "label": "Stage"},
                    {"value": "wide_area", "label": "Wide Area"},
                    {"value": "fine_adjustment", "label": "Fine Adjustment"},
                ],
                "default": "standard", "setup": False,
                "help": (
                    "Standard follows the person. Area moves between up to four stored "
                    "views as the person crosses them. Stage follows only between a "
                    "stored left and right edge. Wide Area holds two stored views and "
                    "follows outside them. Fine Adjustment centres the person for a "
                    "moment after each preset recall. Changes only while auto tracking is off."
                ),
            },
            "tracking_bounding_box": {
                "type": "enum", "label": "Tracking Bounding Box", "state_key": "tracking_bounding_box",
                "values": [
                    {"value": "auto", "label": "Auto"},
                    {"value": "on", "label": "On"},
                    {"value": "off", "label": "Off"},
                ],
                "default": "auto", "setup": False,
                "help": "The green frame around the tracked person, on every video output. Auto shows it while picking a person.",
            },
            "tracking_auto_zoom": {
                "type": "boolean", "label": "Tracking Auto Zoom", "state_key": "tracking_auto_zoom",
                "default": True, "setup": False,
                "help": "Zooms to keep the person at the figure size (Standard, Wide Area and Fine Adjustment).",
            },
            "tracking_auto_tilt": {
                "type": "boolean", "label": "Tracking Auto Tilt", "state_key": "tracking_auto_tilt",
                "default": True, "setup": False,
                "help": "Tilts to keep the person at the vertical position (Standard, Wide Area and Fine Adjustment).",
            },
            "tracking_retention": {
                "type": "enum", "label": "Target Retention Time", "state_key": "tracking_retention",
                "values": ["5", "8", "10", "12", "15"],
                "default": "10", "setup": False,
                "help": "How long the camera waits for a lost person before returning to its start position. A guide, not seconds.",
            },
            "tracking_figure_size": {
                "type": "enum", "label": "Tracking Figure Size", "state_key": "tracking_figure_size",
                "values": [
                    {"value": "full", "label": "Full (whole body)"},
                    {"value": "upper", "label": "Upper (from the hips)"},
                    {"value": "close", "label": "Close (from the chest)"},
                    {"value": "custom", "label": "Custom"},
                ],
                "default": "full", "setup": False,
                "help": "How the tracked person is framed. Custom uses Custom Figure Size. Needs Tracking Auto Zoom.",
            },
            "tracking_figure_custom": {
                "type": "integer", "label": "Custom Figure Size", "state_key": "tracking_figure_custom",
                "min": 80, "max": 290, "default": 141, "setup": False,
                "help": "The framing for a Custom figure size; setting it switches Tracking Figure Size to Custom.",
            },
            "tracking_vertical": {
                "type": "integer", "label": "Tracking Vertical Position", "state_key": "tracking_vertical",
                "min": -5, "max": 5, "default": 0, "setup": False,
                "help": "Headroom above the tracked person, -5 to 5. Needs Tracking Auto Tilt.",
            },
            "tracking_horizontal": {
                "type": "enum", "label": "Tracking Horizontal Position", "state_key": "tracking_horizontal",
                "values": [
                    {"value": "left", "label": "Left"},
                    {"value": "center", "label": "Center"},
                    {"value": "right", "label": "Right"},
                ],
                "default": "center", "setup": False,
                "help": "Where the tracked person sits across the picture.",
            },
            "tracking_start_position": {
                "type": "enum", "label": "Tracking Start Position", "state_key": "tracking_start_position",
                "values": [
                    {"value": "current_position", "label": "Current Position"},
                    {"value": "home_position", "label": "Home Position"},
                    {"value": "preset_1", "label": "Preset 1"},
                    {"value": "w1", "label": "W1 (Wide Area mode)"},
                    {"value": "w2", "label": "W2 (Wide Area mode)"},
                ],
                "default": "current_position", "setup": False,
                "help": "Where tracking starts, and where the camera returns when it loses the person.",
            },
            "tracking_starting_area": {
                "type": "enum", "label": "Starting Area", "state_key": "tracking_starting_area",
                "values": [
                    {"value": "a1", "label": "A1"},
                    {"value": "a2", "label": "A2"},
                    {"value": "a3", "label": "A3"},
                    {"value": "a4", "label": "A4"},
                ],
                "default": "a1", "setup": False,
                "help": "The area Area mode starts in and returns to. Pick an area that has been set.",
            },
            "tracking_duration": {
                "type": "integer", "label": "Tracking Duration Time", "state_key": "tracking_duration",
                "min": 1, "max": 6, "default": 3, "setup": False,
                "help": "How long Fine Adjustment follows after a preset recall. A guide, not seconds.",
            },
        },
        "quick_actions": ["pt_home", "tally_program", "tally_clear", "tracking_on", "tracking_off"],
    }

    HEALTH_FAULT_MESSAGE = "The camera stopped answering its web API."

    _client: httpx.AsyncClient | None = None

    def __init__(self, device_id: str, config: dict[str, Any], state: Any, events: Any) -> None:
        super().__init__(device_id, config, state, events)
        self._client = None
        self._auth: httpx.DigestAuth | None = None
        self._session_id = ""
        self._lock = asyncio.Lock()
        self._slow_due = 0.0
        self._model = ""
        # The last status replies, for the Enable flags a button checks.
        self._status: dict[str, dict[str, Any]] = {}
        self._status_at: dict[str, float] = {}
        self._target_task: asyncio.Task | None = None
        picked = self._picked_model()
        if picked:
            self._model = picked
            self._narrow()

    # ── Config accessors ──

    @property
    def _host(self) -> str:
        return str(self.config.get("host", "") or "").strip()

    @property
    def _port(self) -> int:
        return _as_int(self.config.get("port"), 80) or 80

    def _picked_model(self) -> str:
        model = str(self.config.get("model", "") or "").strip().upper()
        return model if model in KNOWN_MODELS else ""

    def _speed(self, params: dict[str, Any], config_key: str, default: int, top: int) -> int:
        speed = params.get("speed")
        if speed is None or speed == "":
            speed = self.config.get(config_key, default)
        return max(1, min(top, _as_int(speed, default)))

    def _auth_fault(self, message: str = "") -> ConnectionFaultError:
        if not str(self.config.get("password", "") or ""):
            return ConnectionFaultError("The camera needs its web password and none is entered.", code="auth_failed")
        return ConnectionFaultError(message or "The camera refused the password for user jvc.", code="auth_failed")

    def _model_name(self) -> str:
        return _MODEL_NAMES.get(self._model, "This camera")

    # ── Model narrowing ──

    def _narrow(self) -> None:
        """Offer only what this model has. An unknown model keeps the full
        declaration."""
        base = type(self).DRIVER_INFO
        if self._model not in KNOWN_MODELS:
            self.DRIVER_INFO = base
            return
        commands = {
            key: cdef for key, cdef in base["commands"].items()
            if self._model in _MODEL_ONLY.get(key, KNOWN_MODELS)
        }
        settings = {
            key: sdef for key, sdef in base["device_settings"].items()
            if self._model in _MODEL_ONLY.get(key, KNOWN_MODELS)
        }
        self.DRIVER_INFO = {
            **base,
            "commands": commands,
            "device_settings": settings,
            "quick_actions": [key for key in base["quick_actions"] if key in commands],
        }

    def _has(self, key: str) -> bool:
        return not self._model or self._model not in KNOWN_MODELS or self._model in _MODEL_ONLY.get(key, KNOWN_MODELS)

    # ── Connection lifecycle ──

    async def _create_transport(self, transport_type: str) -> None:
        if not self._host:
            raise ConnectionFaultError("No IP address is entered.", code="invalid_config")
        if not str(self.config.get("password", "") or ""):
            # Never send a sign-in that cannot succeed: the camera's account
            # always has a password.
            raise self._auth_fault()
        if not await self._verify_reachable(self._host, self._port):
            raise ConnectionError(f"{self._host}:{self._port} is not responding")
        self._auth = httpx.DigestAuth(USERNAME, str(self.config.get("password", "") or ""))
        self._client = httpx.AsyncClient(
            base_url=f"http://{self._host}:{self._port}",
            timeout=httpx.Timeout(10.0, connect=5.0),
            # The sign-in answers 302 with the session cookie: never follow it.
            follow_redirects=False,
        )

    async def _post_connect(self) -> None:
        self._session_id = ""
        self._slow_due = 0.0
        self._status = {}
        self._status_at = {}
        try:
            async with self._lock:
                await self._sign_in()
            info = await self._call("GetSystemInfo")
        except CameraRefused as exc:
            raise ConnectionFaultError(
                f"The device at {self._host} answered, but not with the JVC camera API ({exc}).",
                code="invalid_config",
            ) from exc
        self._apply_system_info(info)
        log.info(
            f"[{self.device_id}] Connected to JVC {self.get_state('model') or 'camera'} at "
            f"{self._host}:{self._port} (Web API {self.get_state('api_version') or 'unknown'})"
        )

    async def _initial_sync(self) -> None:
        await self.poll()

    def _link_alive(self) -> bool:
        return self._client is not None

    async def _close_session(self) -> None:
        task, self._target_task = self._target_task, None
        if task is not None and not task.done():
            task.cancel()
        client, self._client = self._client, None
        self._session_id = ""
        if client is not None:
            await client.aclose()

    async def _liveness_probe(self) -> None:
        try:
            await self._call("GetSystemInfo")
        except CameraRefused:
            return  # it answered: the link is up

    def _apply_system_info(self, data: dict[str, Any]) -> None:
        updates: dict[str, Any] = {}
        for field, key in (("Model", "model"), ("Destination", "region"), ("ApiVersion", "api_version"), ("Serial", "serial_number")):
            value = data.get(field)
            if isinstance(value, str) and value.strip():
                updates[key] = value.strip()
        if updates:
            self.set_states(updates)
        reported = str(data.get("Model", "") or "").strip().upper()
        model = reported if reported in KNOWN_MODELS else self._picked_model()
        if reported and reported not in KNOWN_MODELS:
            log.warning(f"[{self.device_id}] The camera reports model {reported!r}; every command is offered.")
        if model != self._model or not self._model:
            self._model = model
            self._narrow()

    # ── HTTP plumbing ──

    async def _sign_in(self) -> None:
        """GET /api.php under Digest; the camera answers 302 with the
        SessionID cookie. Called with the lock held."""
        client = self._client
        if client is None:
            raise ConnectionError("Not connected")
        try:
            resp = await client.get(LOGIN_PATH, auth=self._auth)
        except httpx.TransportError as exc:
            raise ConnectionError(f"{self._host} is not responding ({type(exc).__name__}).") from exc
        if resp.status_code == 401:
            raise self._auth_fault()
        session = resp.cookies.get("SessionID") or client.cookies.get("SessionID")
        if resp.status_code not in (200, 302) or not session:
            raise CameraRefused(f"the sign-in page answered HTTP {resp.status_code} with no session")
        self._session_id = session

    async def _post(self, command: str, params: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
        """One API request (lock held). Returns (Result, Data)."""
        client = self._client
        if client is None:
            raise ConnectionError("Not connected")
        request: dict[str, Any] = {"Command": command, "SessionID": self._session_id}
        if params:
            request["Params"] = params
        try:
            resp = await client.post(
                API_PATH,
                content=_compact({"Request": request}),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "X-Requested-With": "XMLHttpRequest",
                },
                auth=self._auth,
            )
        except httpx.TransportError as exc:
            raise ConnectionError(f"{self._host} is not responding ({type(exc).__name__}).") from exc
        if resp.status_code == 401:
            raise self._auth_fault()
        if resp.status_code in (301, 302, 303, 307):
            # A redirect to the sign-in page is a session the camera ended.
            return "SessionError", {}
        if resp.status_code != 200:
            raise CameraRefused(f"HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise CameraRefused("an answer that is not JSON") from exc
        response = body.get("Response") if isinstance(body, dict) else None
        if not isinstance(response, dict):
            raise CameraRefused("an answer with no Response")
        data = response.get("Data")
        return str(response.get("Result", "") or ""), data if isinstance(data, dict) else {}

    async def _call(self, command: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one command and return its Data. A SessionError signs in again
        once; one straight after a fresh sign-in is a refused password. Any
        other Result than Success raises CameraRefused."""
        async with self._lock:
            if not self._session_id:
                await self._sign_in()
            result, data = await self._post(command, params)
            if result == "SessionError":
                await self._sign_in()
                result, data = await self._post(command, params)
                if result == "SessionError":
                    raise self._auth_fault("The camera refused the session it had just opened for user jvc.")
        if result != "Success":
            raise CameraRefused(result or "no Result", result=result)
        return data

    async def _command(self, command: str, params: dict[str, Any] | None, what: str) -> dict[str, Any]:
        """_call for a user's command: a refusal becomes CommandRefusedError
        naming what was refused and the camera's reason."""
        try:
            data = await self._call(command, params)
        except CameraRefused as exc:
            reason = _RESULT_SENTENCES.get(exc.result, f"it answered {exc}")
            raise CommandRefusedError(f"The camera refused {what}: {reason}.") from exc
        # A change can enable or disable buttons (a new exposure or focus
        # mode): the cached Enable flags are read again before they refuse.
        self._status_at.clear()
        return data

    # ── Reads ──

    async def _read_status(self, which: str) -> dict[str, Any]:
        """GetCamStatusMinimum ("minimum") or GetCamStatus ("full") into state."""
        command = "GetCamStatusMinimum" if which == "minimum" else "GetCamStatus"
        data = await self._call(command)
        self._status[which] = data
        self._status_at[which] = time.monotonic()
        updates: dict[str, Any] = {}
        for path, (key, kind) in _STATUS_READS.items():
            value = _read(_dig(data, path), kind)
            if value is not None:
                updates[key] = value
        if updates:
            self.set_states(updates)
        return data

    async def _read_position(self) -> None:
        data = await self._call("GetPTPosition")
        updates = {}
        for field, key in (("Pan", "pan_position"), ("Tilt", "tilt_position")):
            value = _read(data.get(field), "int")
            if value is not None:
                updates[key] = value
        if updates:
            self.set_states(updates)

    async def _read_tracking(self) -> None:
        if not self._has("tracking_mode"):
            return
        replies: dict[str, dict[str, Any]] = {}
        for mode in _TRACKING_READS:
            try:
                replies[mode] = await self._call(f"GetTracking{mode}")
            except CameraRefused as exc:
                log.debug(f"[{self.device_id}] GetTracking{mode} answered {exc}")
        updates: dict[str, Any] = {}
        for key, (mode, field, kind) in _TRACKING_SETTINGS.items():
            value = _read(replies.get(mode, {}).get(field), kind)
            if value is not None:
                updates[key] = value
        if updates:
            self.set_states(updates)

    async def _read_preview(self) -> None:
        """The camera's RTSP stream for the Video Panel."""
        try:
            data = await self._call("GetStreamingServerSettings", {"Type": "RTSP"})
        except CameraRefused as exc:
            log.debug(f"[{self.device_id}] RTSP settings answered {exc}")
            return
        rtsp = data.get("Rtsp") if isinstance(data.get("Rtsp"), dict) else {}
        if _as_int(rtsp.get("Auth"), 0) == 1:
            self.set_states({
                "preview_url": "",
                "preview_format": "",
                "preview_status": "unavailable",
                "preview_status_detail": (
                    "RTSP Auth is on at the camera. Turn it off on the camera's "
                    "Network page to show the camera on a panel."
                ),
            })
            return
        port = _as_int(rtsp.get("DstPort"), 554) or 554
        path = str(rtsp.get("StreamId", "") or "").strip() or "1"
        self.set_states({
            "preview_url": f"rtsp://{self._host}:{port}/{path}",
            "preview_format": "rtsp",
            "preview_status": "",
            "preview_status_detail": "",
        })

    async def _read_slow(self) -> None:
        for reader in (lambda: self._read_status("full"), self._read_tracking, self._read_preview):
            try:
                await reader()
            except CameraRefused as exc:
                log.debug(f"[{self.device_id}] A slow read answered {exc}")
        self._slow_due = time.monotonic() + SLOW_INTERVAL_S

    # ── Polling ──

    async def poll(self) -> None:
        if self._client is None:
            return
        try:
            await self._read_status("minimum")
            await self._read_position()
        except CameraRefused as exc:
            # A typed ConnectionError counts toward taking the device offline.
            raise ConnectionError(f"The camera answered its status read with {exc}.") from exc
        if time.monotonic() >= self._slow_due:
            await self._read_slow()

    # ── Commands ──

    def _enable_flag(self, which: str, kind: str, button: str) -> Any:
        kind, button = _ENABLE_ALIASES.get((kind, button), (kind, button))
        return _dig(self._status.get(which, {}), ("Enable", kind, button))

    async def _check_enabled(self, which: str | None, kind: str, button: str) -> bool:
        """False when the camera's own Enable flag for this button says no,
        read again first when the cached one is not fresh."""
        if which is None:
            return True
        flag = self._enable_flag(which, kind, button)
        if flag is None or _as_int(flag, 1) != 0:
            return True
        if time.monotonic() - self._status_at.get(which, 0.0) > ENABLE_FRESH_S:
            try:
                await self._read_status(which)
            except CameraRefused:
                return True
            flag = self._enable_flag(which, kind, button)
        return flag is None or _as_int(flag, 1) != 0

    def _disabled_sentence(self, kind: str, label: str) -> str:
        if kind in ("Iris", "Gain", "Shutter", "Bright"):
            mode = self.get_state("exposure_mode")
            words = {
                "auto": "Auto", "manual": "Manual", "iris_priority": "Iris Priority",
                "shutter_priority": "Shutter Priority", "bright": "Bright",
            }
            if mode in words:
                return f"The camera doesn't take {label} in {words[mode]} exposure."
        if kind == "Whb":
            return "One Push White Balance works only in One Push white balance mode."
        if kind == "Focus":
            return f"The camera doesn't take {label} in auto focus."
        return f"The camera isn't taking {label} right now."

    async def _button(self, kind: str, button: str, label: str, which: str | None) -> None:
        if not await self._check_enabled(which, kind, button):
            raise CommandRefusedError(self._disabled_sentence(kind, label))
        await self._command("SetWebButtonEvent", {"Kind": kind, "Button": button}, label)

    async def _read_back(self, which: str = "minimum") -> None:
        await asyncio.sleep(READ_BACK_DELAY_S)
        try:
            await self._read_status(which)
        except CameraRefused as exc:
            log.debug(f"[{self.device_id}] Read-back answered {exc}")

    async def _verify(self, reader: Any, key: str, expected: Any, what: str) -> None:
        """Read back until the camera reports ``expected`` for ``key``, twice;
        refuse when it never does."""
        for pause in (READ_BACK_DELAY_S, VERIFY_RETRY_S):
            await asyncio.sleep(pause)
            await reader()
            if self.get_state(key) == expected:
                return
        raise CommandRefusedError(
            f"The camera accepted {what} but still reports {self.get_state(key)!r}."
        )

    def _require(self, key: str) -> None:
        if not self._has(key):
            label = type(self).DRIVER_INFO["commands"].get(key, {}).get("label") or (
                type(self).DRIVER_INFO["device_settings"].get(key, {}).get("label", key)
            )
            raise CommandRefusedError(f"The {self._model_name()} has no {label}.")

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        if command not in type(self).DRIVER_INFO["commands"]:
            raise ValueError(f"Unknown command: {command}")
        self._require(command)

        if command in _JOG:
            pan, tilt = _JOG[command]
            speed = self._speed(params, "pt_speed", 15, 30)
            await self._command(
                "JoyStickOperation",
                {
                    "PanDirection": pan, "PanSpeed": speed if pan != "Stop" else 0,
                    "TiltDirection": tilt, "TiltSpeed": speed if tilt != "Stop" else 0,
                },
                "the pan/tilt move",
            )
        elif command == "pt_stop":
            await self._command(
                "JoyStickOperation",
                {"PanDirection": "Stop", "PanSpeed": 0, "TiltDirection": "Stop", "TiltSpeed": 0},
                "Pan/Tilt Stop",
            )
            await self._read_position()
        elif command == "pt_home":
            speed = self._speed(params, "pt_speed", 15, 30)
            await self._command(
                "SetPTCtrl",
                {"PanDirection": "Home", "PanSpeed": speed, "TiltDirection": "Home", "TiltSpeed": speed},
                "Home",
            )
        elif command == "pt_position":
            pan = _as_int(params.get("pan"), -1)
            tilt = _as_int(params.get("tilt"), -1)
            if not 0 <= pan <= 35080 or not 0 <= tilt <= 12080:
                raise ValueError("Pan must be 0 to 35080 and tilt 0 to 12080.")
            speed = self._speed(params, "pt_speed", 15, 30)
            await self._command(
                "SetPTCtrl",
                {
                    "PanDirection": "Position", "PanPosition": pan, "PanSpeed": speed,
                    "TiltDirection": "Position", "TiltPosition": tilt, "TiltSpeed": speed,
                },
                f"the move to pan {pan}, tilt {tilt}",
            )
        elif command in ("zoom_in", "zoom_out"):
            speed = self._speed(params, "zoom_speed", 4, 8)
            direction = "Tele" if command == "zoom_in" else "Wide"
            await self._command(
                "ZoomSwitchOperation", {"Direction": direction, "Speed": speed},
                "Zoom In" if command == "zoom_in" else "Zoom Out",
            )
        elif command == "zoom_stop":
            await self._command("ZoomSwitchOperation", {"Direction": "Stop", "Speed": 0}, "Zoom Stop")
            await self._read_back()
        elif command == "zoom_direct":
            position = _as_int(params.get("position"), -1)
            if not 0 <= position <= 499:
                raise ValueError("Zoom position must be 0 to 499.")
            await self._command("SetZoomCtrl", {"Position": position}, f"zoom position {position}")
            await self._read_back()
        elif command in ("focus_auto", "focus_manual"):
            target = "auto" if command == "focus_auto" else "manual"
            await self._command(
                "SetWebButtonEvent", {"Kind": "Focus", "Button": "Auto" if target == "auto" else "Manual"},
                "Auto Focus" if target == "auto" else "Manual Focus",
            )
            await self._verify(
                lambda: self._read_status("minimum"), "focus_mode", target,
                "Auto Focus" if target == "auto" else "Manual Focus",
            )
        elif command in ("focus_near", "focus_far"):
            step = self._speed(params, "focus_speed", 1, 3)
            button = f"{'Near' if command == 'focus_near' else 'Far'}{step}"
            label = "Focus Near" if command == "focus_near" else "Focus Far"
            # The document: on these cameras a Far or Near event runs until Stop.
            await self._button("Focus", button, label, "full")
        elif command in ("preset_recall", "preset_save", "preset_clear"):
            preset = _as_int(params.get("preset"), -1)
            if not 1 <= preset <= 255:
                raise ValueError("Preset must be 1 to 255.")
            operation, what = {
                "preset_recall": ("Move", "recalling preset"),
                "preset_save": ("Set", "saving preset"),
                "preset_clear": ("Delete", "deleting preset"),
            }[command]
            await self._command("SetPTZPreset", {"No": preset, "Operation": operation}, f"{what} {preset}")
            if command == "preset_recall":
                # A preset also holds exposure, colour and picture settings.
                self._slow_due = 0.0
        elif command == "wb_one_push":
            await self._button("Whb", "Adjust", "One Push White Balance", "minimum")
        elif command in _BUTTONS:
            kind, button, label, which = _BUTTONS[command]
            await self._button(kind, button, label, which)
            if kind in ("Iris", "Gain", "Shutter", "Bright"):
                await self._read_back("minimum")
            elif kind in ("Luminance", "Saturation", "Contrast", "Sharpness", "Hue"):
                await self._read_back("full")
            elif kind == "Menu":
                await self._read_back("minimum")
        elif command in ("tally_on", "tally_off"):
            on = command == "tally_on"
            await self._command("SetTallyLampCtrl", {"Sw": "On" if on else "Off"}, "Tally On" if on else "Tally Off")
            await self._verify(
                lambda: self._read_status("minimum"), "tally_lamp", on, "Tally On" if on else "Tally Off",
            )
        elif command in ("tally_program", "tally_preview", "tally_clear"):
            indication = {"tally_program": "Program", "tally_preview": "Preview", "tally_clear": "Off"}[command]
            await self._command("SetStudioTally", {"Indication": indication}, f"studio tally {indication}")
            await self._verify(
                lambda: self._read_status("minimum"), "studio_tally", _STUDIO_TALLY[indication],
                f"studio tally {indication}",
            )
        elif command in ("streaming_start", "streaming_stop"):
            start = command == "streaming_start"
            if self.get_state("menu_open") is True:
                # The document: start and stop do nothing while the menu is shown.
                raise CommandRefusedError(
                    "The camera's on-screen menu is open. Close it first: the camera "
                    "doesn't start or stop its live stream while the menu is shown."
                )
            await self._command("SetStreamingCtrl", {"Streaming": "On" if start else "Off"},
                                "Start Live Stream" if start else "Stop Live Stream")
            await self._read_back()
        elif command in ("tracking_on", "tracking_off"):
            on = command == "tracking_on"
            await self._command("SetTracking", {"Tracking": "On" if on else "Off"},
                                "Auto Tracking On" if on else "Auto Tracking Off")
            await self._verify(
                lambda: self._read_status("minimum"), "tracking", on,
                "Auto Tracking On" if on else "Auto Tracking Off",
            )
        elif command in ("tracking_target_left", "tracking_target_right"):
            await self._read_status("minimum")
            if self.get_state("tracking") is not True:
                raise CommandRefusedError("Auto tracking is off. Turn it on to pick a person.")
            await self._switch_target("Left" if command == "tracking_target_left" else "Right")
        elif command in ("tracking_area_set", "tracking_area_delete"):
            area = str(params.get("area", "") or "")
            if area not in _STARTING_AREA:
                raise ValueError("Area must be A1, A2, A3 or A4.")
            operation = "Set" if command == "tracking_area_set" else "Delete"
            await self._command("ExecuteTrackingStartingArea", {"Command": operation, "Area": area},
                                f"{'setting' if operation == 'Set' else 'deleting'} tracking area {area}")
        elif command in ("tracking_stage_set", "tracking_stage_delete"):
            position = str(params.get("position", "") or "")
            if position not in ("Left", "Right", "Start"):
                raise ValueError("Position must be Left, Right or Start.")
            operation = "Set" if command == "tracking_stage_set" else "Delete"
            await self._command("ExecuteTrackingStagePosition", {"Command": operation, "Position": position},
                                f"{'setting' if operation == 'Set' else 'deleting'} the stage position {position}")
        elif command in ("tracking_wide_set", "tracking_wide_delete"):
            area = str(params.get("area", "") or "")
            if area not in ("W1", "W2"):
                raise ValueError("Area must be W1 or W2.")
            operation = "Set" if command == "tracking_wide_set" else "Delete"
            await self._command("ExecuteTrackingWideAreaPosition", {"Command": operation, "Position": area},
                                f"{'setting' if operation == 'Set' else 'deleting'} wide area {area}")
        elif command == "reboot":
            await self._command("SystemRequest", {"Request": "Reboot"}, "Reboot")
        elif command == "restore_defaults":
            await self._command("SystemRequest", {"Request": "Restore"}, "Restore Factory Settings")
        else:
            raise ValueError(f"Unknown command: {command}")

    async def _switch_target(self, direction: str) -> None:
        """One short pan while tracking: the camera moves tracking to the next
        person that way (Web API 3.4.2)."""
        speed = self._speed({}, "pt_speed", 15, 30)
        await self._command(
            "JoyStickOperation",
            {"PanDirection": direction, "PanSpeed": speed, "TiltDirection": "Stop", "TiltSpeed": 0},
            "switching the tracked person",
        )
        await asyncio.sleep(TARGET_SWITCH_S)
        await self._command(
            "JoyStickOperation",
            {"PanDirection": "Stop", "PanSpeed": 0, "TiltDirection": "Stop", "TiltSpeed": 0},
            "switching the tracked person",
        )

    # ── Device settings ──

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        if key not in type(self).DRIVER_INFO["device_settings"]:
            raise ValueError(f"Unknown device setting: {key}")
        self._require(key)
        label = type(self).DRIVER_INFO["device_settings"][key]["label"]
        minimum = lambda: self._read_status("minimum")  # noqa: E731
        full = lambda: self._read_status("full")  # noqa: E731

        if key == "exposure_mode":
            await self._command("SetWebButtonEvent", {"Kind": "Exposure", "Button": _word(value, _EXPOSURE)}, label)
            await self._verify(minimum, key, value, f"{label} = {value}")
        elif key == "wb_mode":
            await self._command("SetWebButtonEvent", {"Kind": "Whb", "Button": _word(value, _WB)}, label)
            await self._verify(minimum, key, value, f"{label} = {value}")
        elif key in ("wb_red_paint", "wb_blue_paint"):
            await self._write_paint(key, _as_int(value, -1), label)
        elif key in ("flip", "mirror", "speed_with_zoom"):
            on = _as_bool(value)
            kind = {"flip": "Flip", "mirror": "Mirror", "speed_with_zoom": "SpeedWithZoom"}[key]
            await self._command("SetWebButtonEvent", {"Kind": kind, "Button": "On" if on else "Off"}, label)
            await self._verify(full, key, on, f"{label} = {'on' if on else 'off'}")
        elif key == "tracking_mode":
            await self._read_status("minimum")
            if self.get_state("tracking") is True:
                # JVC's tracking manual: the mode changes only with tracking off.
                raise CommandRefusedError("Turn auto tracking off before changing its mode.")
            await self._command("SetTrackingMode", {"TrackingMode": _word(value, _TRACKING_MODE)}, label)
            await self._verify(minimum, key, value, f"{label} = {value}")
        elif key == "tracking_bounding_box":
            await self._command("SetTrackingBoundingBox", {"TrackingBoundingBox": _word(value, _BOUNDING_BOX)}, label)
            await self._verify(full, key, value, f"{label} = {value}")
        elif key in _TRACKING_SETTINGS:
            await self._write_tracking(key, value, label)
        else:
            raise ValueError(f"Unknown device setting: {key}")

    async def _write_paint(self, key: str, value: int, label: str) -> None:
        """SetWebXYFieldEvent carries both paints (X blue, Y red), so the other
        one goes as the camera last reported it."""
        await self._read_status("minimum")
        whb = self._status.get("minimum", {}).get("Whb", {}) or {}
        scale_field = "WhPRScale" if key == "wb_red_paint" else "WhPBScale"
        top = _as_int(whb.get(scale_field), 255) or 255
        if not 0 <= value <= top:
            mode = self.get_state("wb_mode") or "the current"
            raise CommandRefusedError(f"In {mode} white balance, {label} goes from 0 to {top}.")
        red = value if key == "wb_red_paint" else _as_int(self.get_state("wb_red_paint"), 0)
        blue = value if key == "wb_blue_paint" else _as_int(self.get_state("wb_blue_paint"), 0)
        await self._command(
            "SetWebXYFieldEvent", {"Kind": "WhPaintRB", "XPosition": blue, "YPosition": red},
            f"{label} = {value}",
        )
        await self._verify(lambda: self._read_status("minimum"), key, value, f"{label} = {value}")

    async def _write_tracking(self, key: str, value: Any, label: str) -> None:
        mode, field, kind = _TRACKING_SETTINGS[key]
        params: dict[str, Any]
        if key == "tracking_figure_custom":
            size = _as_int(value, -1)
            if not 80 <= size <= 290:
                raise ValueError("Custom figure size must be 80 to 290.")
            command, params, expected = f"SetTracking{mode}FigureSize", {"FigureSize": "Custom", "FigureSizeCustom": size}, size
        elif key == "tracking_figure_size":
            command = f"SetTracking{mode}FigureSize"
            params = {"FigureSize": _word(value, _FIGURE_SIZE)}
            if value == "custom":
                params["FigureSizeCustom"] = _as_int(self.get_state("tracking_figure_custom"), 141) or 141
            expected = value
        elif key == "tracking_retention":
            seconds = _as_int(value, -1)
            if seconds not in _RETENTION:
                raise ValueError("Target retention time must be 5, 8, 10, 12 or 15.")
            command, params, expected = f"SetTracking{mode}TargetRetentionTime", {field: seconds}, _RETENTION[seconds]
        elif key == "tracking_duration":
            duration = _as_int(value, -1)
            if not 1 <= duration <= 6:
                raise ValueError("Tracking duration time must be 1 to 6.")
            command, params, expected = f"SetTracking{mode}DurationTime", {field: duration}, duration
        elif key == "tracking_vertical":
            position = _as_int(value, 99)
            if not -5 <= position <= 5:
                raise ValueError("Vertical position must be -5 to 5.")
            command, params, expected = f"SetTracking{mode}VerticalPosition", {field: position}, position
        elif key in ("tracking_auto_zoom", "tracking_auto_tilt"):
            on = _as_bool(value)
            command, params, expected = f"SetTracking{mode}{field}", {field: "On" if on else "Off"}, on
        else:
            command = f"SetTracking{mode}{field}"
            params = {field: _word(value, kind)}
            expected = value
        await self._command(command, params, f"{label} = {value}")
        await self._verify(self._read_tracking, key, expected, f"{label} = {value}")
