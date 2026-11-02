"""
OpenAVC Epiphan EC20 PTZ Camera Driver (EC20 REST API).

Controls Epiphan's EC20 4K PTZ camera through the REST API the camera serves
over HTTP: power and standby, pan, tilt, zoom and focus (jogs, absolute and
relative moves, position read-back), presets and the home position, AI
auto-tracking (on and off, presenter or zone mode, presenter selection),
the tally light, recording, freeze, exposure, white balance and picture
settings, image flip, the on-screen menu, and RTMP, SRT, multicast and ONVIF
streaming settings. The camera's RTSP stream is offered to a panel as a
preview.

Push vs poll
------------
Poll only. The API documents no subscription, event or callback. Power, the
pan/tilt/zoom position, recording, freeze, tally and tracking are read every
``poll_interval`` (default 5 s); focus, exposure, color, picture, video
output, speed limits, the on-screen menu and streaming every 30 s. Every
command reads what it changed back at once. After a move the position is read
until it stops changing; after Power On or Standby the power state is read
until the camera reports it.

Why Python
----------
A write is answered 200 with no value and the camera reports nothing by
itself, so every write is followed by a GET of the same resource in the same
command, and a write the camera answers but does not apply is reported as
refused. A YAML HTTP command is one request. Several resources also answer
bodies of the same shape (``/misc/standby`` and ``/osd/action`` are both
``{"action": ...}``, and four more open with ``enable``), and a YAML HTTP
response rule does not know which path it answers. Nested bodies
(``normal.panSpeed``, ``stream1.encode``, ``presenterTracking.figurePosition``)
are flattened into state, and Zoom To Position reads the current pan and tilt
so that zoom alone moves.

Protocol
--------
``http://<camera>/api/v1/<resource>`` with HTTP Digest (MD5) sign-in; the
factory account is admin / admin. ``GET`` answers the resource as a JSON
object. ``POST`` takes a JSON object of the fields to change, with values
written as JSON strings (``{"action": "on"}``), and answers 200. ``PATCH``
steps one field of an image resource (``{"target": "shutter", "op": "inc",
"value": "1"}``). Positions, picture levels and stream sizes come back as
JSON numbers, most other values as strings. This driver sends only the fields
it changes, as the API's own exposure and color examples do.

In ``/misc/standby`` the action ``on`` means the camera is active and ``off``
means standby; this driver reports them as ``on`` and ``standby``.

The camera's RTSP streams are ``rtsp://<camera>/1`` (main) and ``/2``
(preview) on the RTSP port (default 554). The API reports neither the RTSP
port nor whether RTSP sign-in is on, so the port is a setting here and the
preview needs RTSP authentication off at the camera.

Source:
    Epiphan EC20 PTZ Camera REST API v1 (OpenAPI 3.0, Epiphan Video):
    https://epiphan-video.github.io/pearl_api_swagger_ui/api/ec20/v1/openapi.yml
    EC20 user guide (ports, RTSP stream addresses, factory account, tracking
    settings): https://www.epiphan.com/userguides/ec20/Content/Home-EC20.htm
"""

from __future__ import annotations

import asyncio
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

API = "/api/v1"

# The read every poll makes first, at connect too.
CORE = "/misc/standby"

# Read every poll / every SLOW_INTERVAL_S.
FAST = (
    "/misc/standby",
    "/movement/absolute/position",
    "/misc/record",
    "/misc/freeze",
    "/tally/param",
    "/ai/param",
)
SLOW = (
    "/focus/mode",
    "/focus/sensitivity",
    "/focus/zone",
    "/focus/position",
    "/image/exposure/param",
    "/image/color/param",
    "/image/picture/param",
    "/image/orientation/param",
    "/video/param",
    "/movement/speeds",
    "/misc/digitalzoom",
    "/misc/dvimode",
    "/misc/standbylight",
    "/ir/enable",
    "/osd/action",
    "/osd/language",
    "/streaming/rtmp",
    "/streaming/srt",
    "/streaming/multicast",
    "/streaming/onvif",
)
SLOW_INTERVAL_S = 30.0

POSITION = "/movement/absolute/position"

# After Power On / Standby, read the power state this often for this long.
POWER_WATCH_STEP_S = 2.0
POWER_WATCH_S = 60.0
# After a move, read the position this often until two reads agree.
MOVE_WATCH_STEP_S = 0.5
MOVE_WATCH_S = 20.0
# A value that does not read back as written is read once more after this.
READ_BACK_RETRY_S = 1.0

# resource -> (field path, state key, how the value is read).
#   str: as reported   int: a number (JSON number or numeric string)
#   onoff: "on" -> True   power: "on" -> "on", "off" -> "standby"
# The SRT password and the RTMP stream keys are in these answers and are
# never read into state.
READS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "/misc/standby": (("action", "power", "power"),),
    "/movement/absolute/position": (
        ("panPos", "pan_position", "int"),
        ("tiltPos", "tilt_position", "int"),
        ("zoomPos", "zoom_position", "int"),
    ),
    "/misc/record": (
        ("status", "recording", "onoff"),
        ("filename", "record_filename", "str"),
        ("mode", "record_mode", "str"),
    ),
    "/misc/freeze": (("freeze", "freeze", "onoff"),),
    "/tally/param": (
        ("action", "tally", "str"),
        ("enable", "tally_enabled", "onoff"),
        ("tallyOption", "tally_color", "str"),
    ),
    "/ai/param": (
        ("enable", "tracking", "onoff"),
        ("mode", "tracking_mode", "str"),
        ("debugMode", "tracking_debug_mode", "str"),
        ("hintCue", "tracking_hint", "onoff"),
        ("targetLostTimeOut", "tracking_lost_timeout", "int"),
        ("presenterTracking.figureSize", "presenter_figure_size", "str"),
        ("presenterTracking.customFigureSize", "presenter_custom_size", "str"),
        ("presenterTracking.figurePosition", "presenter_figure_position", "str"),
        ("presenterTracking.startPosition", "presenter_start_position", "str"),
        ("zoneTracking.startArea", "zone_start_area", "str"),
    ),
    "/focus/mode": (("mode", "focus_mode", "str"),),
    "/focus/sensitivity": (("sensitivity", "af_sensitivity", "str"),),
    "/focus/zone": (("zone", "af_zone", "str"),),
    "/focus/position": (("focusPos", "focus_position", "int"),),
    "/image/exposure/param": (
        ("exposureMode", "exposure_mode", "str"),
        ("shutter", "shutter", "str"),
        ("iris", "iris", "str"),
        ("gain", "gain", "str"),
        ("brightness", "brightness", "str"),
        ("gainLimit", "gain_limit", "str"),
        ("compensationMode", "exposure_compensation_mode", "onoff"),
        ("exposureCompensation", "exposure_compensation", "str"),
        ("backlight", "backlight", "onoff"),
        ("meter", "meter", "str"),
        ("dynamicRangeControl", "drc", "str"),
        ("antiFlicker", "anti_flicker", "str"),
    ),
    "/image/color/param": (
        ("whiteBalanceMode", "wb_mode", "str"),
        ("redGain", "red_gain", "str"),
        ("blueGain", "blue_gain", "str"),
        ("redGainTuning", "red_gain_tuning", "str"),
        ("blueGainTuning", "blue_gain_tuning", "str"),
        ("colorTemp", "color_temp", "str"),
        ("saturation", "saturation", "str"),
        ("hue", "hue", "str"),
    ),
    "/image/picture/param": (
        ("luminance", "luminance", "int"),
        ("contrast", "contrast", "int"),
        ("sharpness", "sharpness", "int"),
        ("noise3d", "noise_3d", "int"),
        ("gamma", "gamma", "str"),
    ),
    "/image/orientation/param": (
        ("horizontalFlip", "horizontal_flip", "onoff"),
        ("verticalFlip", "vertical_flip", "onoff"),
    ),
    "/video/param": (
        ("hardwareOutput.hdmi", "hdmi_output", "onoff"),
        ("hardwareOutput.3g-sdi", "sdi_3g_output", "onoff"),
        ("hardwareOutput.12g-sdi", "sdi_12g_output", "onoff"),
        ("hardwareOutput.usb", "usb_output", "onoff"),
        ("hardwareOutput.ip", "ip_output", "onoff"),
        ("hardwareOutputResolution", "output_resolution", "str"),
        ("stream1.encode", "stream1_codec", "str"),
        ("stream1.width", "stream1_width", "int"),
        ("stream1.height", "stream1_height", "int"),
        ("stream1.framerate", "stream1_frame_rate", "int"),
        ("stream1.bitrate", "stream1_bitrate", "int"),
        ("stream1.bitrateControl", "stream1_bitrate_control", "str"),
        ("stream2.encode", "stream2_codec", "str"),
        ("stream2.width", "stream2_width", "int"),
        ("stream2.height", "stream2_height", "int"),
        ("stream2.framerate", "stream2_frame_rate", "int"),
        ("stream2.bitrate", "stream2_bitrate", "int"),
        ("stream2.bitrateControl", "stream2_bitrate_control", "str"),
    ),
    "/movement/speeds": (
        ("normal.panSpeed", "pan_speed_limit", "int"),
        ("normal.tiltSpeed", "tilt_speed_limit", "int"),
        ("normal.zoomSpeed", "zoom_speed_limit", "int"),
    ),
    "/misc/digitalzoom": (("digitalzoom", "digital_zoom", "str"),),
    "/misc/dvimode": (("dvimode", "hdmi_mode", "str"),),
    "/misc/standbylight": (("status", "standby_light", "str"),),
    "/ir/enable": (("enable", "ir_receiver", "onoff"),),
    "/osd/action": (("action", "osd_menu", "onoff"),),
    "/osd/language": (("language", "osd_language", "str"),),
    "/streaming/rtmp": (
        ("stream1.enable", "rtmp1_enabled", "onoff"),
        ("stream1.active", "rtmp1_status", "str"),
        ("stream1.err_msg", "rtmp1_message", "str"),
        ("stream1.mrl", "rtmp1_url", "str"),
        ("stream2.enable", "rtmp2_enabled", "onoff"),
        ("stream2.active", "rtmp2_status", "str"),
        ("stream2.err_msg", "rtmp2_message", "str"),
        ("stream2.mrl", "rtmp2_url", "str"),
    ),
    "/streaming/srt": (
        ("enable", "srt_enabled", "onoff"),
        ("mode", "srt_mode", "str"),
        ("server", "srt_server", "str"),
        ("port", "srt_port", "int"),
        ("encryption", "srt_encryption", "str"),
        ("bandwidth", "srt_bandwidth", "int"),
        ("latency", "srt_latency", "int"),
        ("streamId", "srt_stream_id", "str"),
    ),
    "/streaming/multicast": (
        ("enable", "multicast_enabled", "onoff"),
        ("ip", "multicast_address", "str"),
        ("port", "multicast_port", "int"),
    ),
    "/streaming/onvif": (
        ("enable", "onvif_enabled", "onoff"),
        ("auth", "onvif_auth", "onoff"),
    ),
}

# What a refused read is called in Last Error.
RESOURCE_LABELS = {
    "/movement/absolute/position": "position",
    "/misc/record": "recording",
    "/misc/freeze": "freeze",
    "/tally/param": "tally",
    "/ai/param": "tracking",
    "/focus/mode": "focus mode",
    "/focus/sensitivity": "AF sensitivity",
    "/focus/zone": "AF zone",
    "/focus/position": "focus position",
    "/image/exposure/param": "exposure",
    "/image/color/param": "color",
    "/image/picture/param": "picture",
    "/image/orientation/param": "image flip",
    "/video/param": "video output",
    "/movement/speeds": "speed limits",
    "/misc/digitalzoom": "digital zoom",
    "/misc/dvimode": "HDMI mode",
    "/misc/standbylight": "standby light",
    "/ir/enable": "IR receiver",
    "/osd/action": "on-screen menu",
    "/osd/language": "menu language",
    "/streaming/rtmp": "RTMP",
    "/streaming/srt": "SRT",
    "/streaming/multicast": "multicast",
    "/streaming/onvif": "ONVIF",
}

# Device setting -> (resource, field path, how the value is written).
#   onoff: a boolean as "on" / "off"   str: as text   num: a number as text
#   int: a JSON integer (the fields the API types as integers)
SETTINGS: dict[str, tuple[str, str, str]] = {
    "focus_mode": ("/focus/mode", "mode", "str"),
    "af_sensitivity": ("/focus/sensitivity", "sensitivity", "str"),
    "af_zone": ("/focus/zone", "zone", "str"),
    "exposure_mode": ("/image/exposure/param", "exposureMode", "str"),
    "exposure_compensation_mode": ("/image/exposure/param", "compensationMode", "onoff"),
    "backlight": ("/image/exposure/param", "backlight", "onoff"),
    "meter": ("/image/exposure/param", "meter", "str"),
    "anti_flicker": ("/image/exposure/param", "antiFlicker", "str"),
    "wb_mode": ("/image/color/param", "whiteBalanceMode", "str"),
    "luminance": ("/image/picture/param", "luminance", "num"),
    "contrast": ("/image/picture/param", "contrast", "num"),
    "sharpness": ("/image/picture/param", "sharpness", "num"),
    "noise_3d": ("/image/picture/param", "noise3d", "num"),
    "horizontal_flip": ("/image/orientation/param", "horizontalFlip", "onoff"),
    "vertical_flip": ("/image/orientation/param", "verticalFlip", "onoff"),
    "digital_zoom": ("/misc/digitalzoom", "digitalzoom", "str"),
    "hdmi_mode": ("/misc/dvimode", "dvimode", "str"),
    "standby_light": ("/misc/standbylight", "status", "str"),
    "ir_receiver": ("/ir/enable", "enable", "onoff"),
    "tally_enabled": ("/tally/param", "enable", "onoff"),
    "tally_color": ("/tally/param", "tallyOption", "str"),
    "tracking_mode": ("/ai/param", "mode", "str"),
    "tracking_debug_mode": ("/ai/param", "debugMode", "str"),
    "tracking_hint": ("/ai/param", "hintCue", "onoff"),
    "tracking_lost_timeout": ("/ai/param", "targetLostTimeOut", "int"),
    "presenter_figure_position": ("/ai/param", "presenterTracking.figurePosition", "str"),
    "pan_speed_limit": ("/movement/speeds", "normal.panSpeed", "num"),
    "tilt_speed_limit": ("/movement/speeds", "normal.tiltSpeed", "num"),
    "zoom_speed_limit": ("/movement/speeds", "normal.zoomSpeed", "num"),
    "srt_mode": ("/streaming/srt", "mode", "str"),
    "srt_server": ("/streaming/srt", "server", "str"),
    "srt_port": ("/streaming/srt", "port", "int"),
    "srt_encryption": ("/streaming/srt", "encryption", "str"),
    "srt_bandwidth": ("/streaming/srt", "bandwidth", "int"),
    "srt_latency": ("/streaming/srt", "latency", "int"),
    "srt_stream_id": ("/streaming/srt", "streamId", "str"),
    "multicast_address": ("/streaming/multicast", "ip", "str"),
    "multicast_port": ("/streaming/multicast", "port", "int"),
    "onvif_enabled": ("/streaming/onvif", "enable", "onoff"),
    "onvif_auth": ("/streaming/onvif", "auth", "onoff"),
    "rtmp1_url": ("/streaming/rtmp", "stream1.mrl", "str"),
    "rtmp2_url": ("/streaming/rtmp", "stream2.mrl", "str"),
}

# The eight pan/tilt jogs -> the API's direction word.
PT_DIRECTIONS = {
    "pt_up": "up",
    "pt_down": "down",
    "pt_left": "left",
    "pt_right": "right",
    "pt_up_left": "upleft",
    "pt_up_right": "upright",
    "pt_down_left": "downleft",
    "pt_down_right": "downright",
}

# Image adjustments (PATCH): command -> (resource, the targets it may step).
ADJUST = {
    "exposure_adjust": (
        "/image/exposure/param",
        ("shutter", "iris", "gain", "brightness", "gainLimit", "exposureCompensation", "dynamicRangeControl"),
    ),
    "color_adjust": (
        "/image/color/param",
        ("redGain", "blueGain", "redGainTuning", "blueGainTuning", "colorTemp", "saturation", "hue"),
    ),
    "picture_adjust": (
        "/image/picture/param",
        ("luminance", "contrast", "sharpness", "noise3d"),
    ),
}

# Commands that turn a feature on or off: command -> (resource, field, the
# word written, the state key it reads back as, the value expected there,
# what it is called).
SWITCHES: dict[str, tuple[str, str, str, str, Any, str]] = {
    "tally_on": ("/tally/param", "action", "on", "tally", "on", "Tally On"),
    "tally_off": ("/tally/param", "action", "off", "tally", "off", "Tally Off"),
    "tally_blink": ("/tally/param", "action", "blink", "tally", "blink", "Tally Blink"),
    "tracking_on": ("/ai/param", "enable", "on", "tracking", True, "Tracking On"),
    "tracking_off": ("/ai/param", "enable", "off", "tracking", False, "Tracking Off"),
    "freeze_on": ("/misc/freeze", "freeze", "on", "freeze", True, "Freeze On"),
    "freeze_off": ("/misc/freeze", "freeze", "off", "freeze", False, "Freeze Off"),
    "focus_auto": ("/focus/mode", "mode", "auto", "focus_mode", "auto", "Auto Focus"),
    "focus_manual": ("/focus/mode", "mode", "manual", "focus_mode", "manual", "Manual Focus"),
    "srt_start": ("/streaming/srt", "enable", "on", "srt_enabled", True, "Start SRT"),
    "srt_stop": ("/streaming/srt", "enable", "off", "srt_enabled", False, "Stop SRT"),
    "multicast_start": ("/streaming/multicast", "enable", "on", "multicast_enabled", True, "Start Multicast"),
    "multicast_stop": ("/streaming/multicast", "enable", "off", "multicast_enabled", False, "Stop Multicast"),
}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _field(payload: Any, path: str) -> Any:
    """The value at a dotted path in an answer; None when it is not there."""
    node = payload
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _nested(path: str, value: Any) -> dict[str, Any]:
    """A request body holding one value at a dotted path."""
    parts = path.split(".")
    body: dict[str, Any] = {parts[-1]: value}
    for part in reversed(parts[:-1]):
        body = {part: body}
    return body


def _read_value(raw: Any, kind: str) -> Any:
    if raw is None or isinstance(raw, (dict, list)):
        return None
    if kind == "onoff":
        text = str(raw).strip().lower()
        if isinstance(raw, bool) or text in ("on", "off", "true", "false", "1", "0"):
            return _as_bool(raw)
        return None
    if kind == "power":
        text = str(raw).strip().lower()
        return {"on": "on", "off": "standby"}.get(text)
    if kind == "int":
        if isinstance(raw, bool):
            return None
        try:
            return int(float(str(raw).strip()))
        except ValueError:
            return None
    return str(raw).strip()


def _write_value(value: Any, kind: str) -> Any:
    if kind == "onoff":
        return "on" if _as_bool(value) else "off"
    if kind == "int":
        return _as_int(value, 0)
    if kind == "num":
        return str(_as_int(value, 0))
    return "" if value is None else str(value)


def _expected(value: Any, kind: str) -> Any:
    """What a written value reads back as."""
    if kind == "onoff":
        return _as_bool(value)
    if kind in ("int", "num"):
        return _as_int(value, 0)
    return "" if value is None else str(value)


def _same(reported: Any, expected: Any) -> bool:
    if isinstance(expected, str) and isinstance(reported, str):
        return reported.strip().lower() == expected.strip().lower()
    return reported == expected


class CameraRefused(Exception):
    """The camera answered, and said no (or not with the API's JSON)."""

    def __init__(self, message: str, *, http_status: int = 0) -> None:
        super().__init__(message)
        self.http_status = http_status


class EpiphanEC20Driver(BaseDriver):
    """Epiphan EC20 PTZ camera over the EC20 REST API (HTTP Digest)."""

    DRIVER_INFO = {
        "id": "epiphan_ec20",
        "name": "Epiphan EC20 PTZ Camera",
        "manufacturer": "Epiphan",
        "category": "camera",
        "version": "1.0.0",
        "min_platform_version": "0.36.0",
        "author": "OpenAVC",
        "description": (
            "Controls the Epiphan EC20 4K PTZ camera over its REST API: power and "
            "standby, pan, tilt, zoom and focus with position read-back, presets "
            "and home, AI auto-tracking and presenter selection, the tally light, "
            "recording, exposure, white balance and picture settings, and RTMP, "
            "SRT, multicast and ONVIF streaming. Offers the camera's RTSP stream "
            "as a panel preview."
        ),
        "source_url": "https://epiphan-video.github.io/pearl_api_swagger_ui/api/ec20/v1/openapi.yml",
        "tags": [
            "epiphan", "ec20", "ptz", "camera", "rest", "auto-tracking",
            "lecture-capture", "rtsp", "srt", "tally",
        ],
        "verified": False,
        "simulated": True,
        "protocols": ["epiphan_ec20_rest"],
        "ports": [80],
        "transport": "http",
        "discovery": {
            # Epiphan's IEEE block (MA-M 58:95:D8:7, Epiphan Systems Inc).
            # No identifying probe: every API path asks for Digest sign-in,
            # and neither the API nor the user guide shows the challenge's
            # realm or a page answered without signing in.
            "oui": ["58:95:d8:7"],
            "manufacturer_alias": ["Epiphan", "Epiphan Systems", "Epiphan Systems Inc", "Epiphan Video"],
        },
        "compatible_models": [
            {
                "manufacturer": "Epiphan",
                "models": ["EC20"],
                "confidence": "untested",
                "notes": (
                    "EC20 REST API v1 (firmware 3.6.44 was current when this "
                    "driver was written). Not yet tested on this hardware."
                ),
            },
        ],
        "help": {
            "overview": (
                "Controls an Epiphan EC20 PTZ camera: power and standby, pan, tilt, "
                "zoom and focus, presets and the home position, AI auto-tracking "
                "and presenter selection, the tally light, recording and freeze. "
                "Exposure, white balance, picture, focus, tracking and streaming "
                "settings appear on the device page and are read back from the "
                "camera, as are the pan, tilt, zoom and focus positions. The "
                "camera's RTSP stream is offered to the Video Panel as a preview. "
                "The Pan Speed, Tilt Speed, Zoom Speed and Focus Speed fields set "
                "the speed of a move that does not give its own."
            ),
            "setup": (
                "1. Connect the camera to the network. It takes an address from "
                "DHCP and shows it on its front screen; with DHCP off its address "
                "is 192.168.100.88.\n"
                "2. Enter the camera's IP address here with its web user name and "
                "password (the factory account is admin, password admin; change "
                "it on the camera's web page under System).\n"
                "3. If the camera's HTTP port was changed under Network > Port, "
                "enter it in Port.\n"
                "4. To show the camera on a panel, leave RTSP authentication off "
                "on the camera's Network > RTSP page, and set RTSP Port if it was "
                "changed from 554."
            ),
        },
        "default_config": {
            "host": "",
            "port": 80,
            "username": "admin",
            "password": "",
            "pan_speed": 12,
            "tilt_speed": 10,
            "zoom_speed": 4,
            "focus_speed": 3,
            "rtsp_port": 554,
            "preview_stream": "2",
            "poll_interval": 5,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {
                "type": "integer",
                "default": 80,
                "min": 1,
                "max": 65535,
                "label": "Port",
                "description": "The camera's HTTP port (Network > Port on the camera; 80 unless changed).",
            },
            "username": {
                "type": "string",
                "default": "admin",
                "required": True,
                "label": "Username",
            },
            "password": {
                "type": "string",
                "default": "",
                "required": True,
                "label": "Password",
                "secret": True,
                "description": "The camera's web password.",
            },
            "pan_speed": {
                "type": "integer",
                "default": 12,
                "min": 1,
                "max": 24,
                "label": "Pan Speed",
                "description": "Speed for pan moves that do not give their own (1 slowest, 24 fastest).",
            },
            "tilt_speed": {
                "type": "integer",
                "default": 10,
                "min": 1,
                "max": 20,
                "label": "Tilt Speed",
                "description": "Speed for tilt moves that do not give their own (1 slowest, 20 fastest).",
            },
            "zoom_speed": {
                "type": "integer",
                "default": 4,
                "min": 1,
                "max": 7,
                "label": "Zoom Speed",
                "description": "Speed for zoom moves that do not give their own (1 slowest, 7 fastest).",
            },
            "focus_speed": {
                "type": "integer",
                "default": 3,
                "min": 1,
                "max": 7,
                "label": "Focus Speed",
                "description": "Speed for focus moves that do not give their own (1 slowest, 7 fastest).",
            },
            "rtsp_port": {
                "type": "integer",
                "default": 554,
                "min": 1,
                "max": 65535,
                "label": "RTSP Port",
                "description": "The camera's RTSP port (Network > Port on the camera; 554 unless changed).",
            },
            "preview_stream": {
                "type": "enum",
                "values": ["2", "1"],
                "default": "2",
                "label": "Preview Stream",
                "description": "The stream a panel shows: 2 is the camera's lighter preview stream, 1 its main stream.",
            },
            "poll_interval": {
                "type": "integer",
                "default": 5,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "How often power, position, recording, tally and tracking are "
                    "read. 0 stops polling."
                ),
            },
        },
        "state_variables": {
            "power": {
                "type": "enum",
                "values": ["on", "standby"],
                "label": "Power",
                "help": "on while the camera is active, standby while it is in standby.",
                "control": True,
                "cloud_priority": "high",
            },
            "pan_position": {
                "type": "integer",
                "label": "Pan Position",
                "help": "From about -170 (left) to 170 (right).",
                "cloud_priority": "low",
            },
            "tilt_position": {
                "type": "integer",
                "label": "Tilt Position",
                "help": "From about -30 (down) to 90 (up).",
                "cloud_priority": "low",
            },
            "zoom_position": {
                "type": "integer",
                "label": "Zoom Position",
                "min": 0,
                "max": 4912,
                "help": "0 is fully wide, 4912 fully zoomed in.",
                "cloud_priority": "low",
            },
            "focus_position": {
                "type": "integer",
                "label": "Focus Position",
                "min": 236,
                "max": 4148,
                "cloud_priority": "low",
            },
            "recording": {
                "type": "boolean",
                "label": "Recording",
                "help": "True while the camera is recording.",
                "control": True,
                "cloud_priority": "high",
            },
            "record_filename": {"type": "string", "label": "Recording File Name"},
            "record_mode": {"type": "string", "label": "Recording Mode"},
            "freeze": {
                "type": "boolean",
                "label": "Freeze",
                "help": "True while the camera's video output is frozen.",
                "control": True,
            },
            "tally": {
                "type": "enum",
                "values": ["on", "off", "blink"],
                "label": "Tally Light",
                "control": True,
                "cloud_priority": "high",
            },
            "tally_enabled": {"type": "boolean", "label": "Tally Enabled"},
            "tally_color": {"type": "enum", "values": ["red", "green"], "label": "Tally Color"},
            "tracking": {
                "type": "boolean",
                "label": "Auto Tracking",
                "help": "True while AI auto-tracking is on.",
                "control": True,
                "cloud_priority": "high",
            },
            "tracking_mode": {
                "type": "enum",
                "values": ["tracking", "zone"],
                "label": "Tracking Mode",
                "help": "tracking follows a presenter; zone moves between the saved zone positions A to D.",
                "control": True,
            },
            "tracking_debug_mode": {
                "type": "enum",
                "values": ["default", "debug", "off"],
                "label": "Tracking Debug Mode",
            },
            "tracking_hint": {
                "type": "boolean",
                "label": "Tracking Hint",
                "help": "True while the tracking hint is drawn on the video output.",
            },
            "tracking_lost_timeout": {
                "type": "integer",
                "label": "Target Retention Time",
                "unit": "s",
                "help": "How long tracking waits for a lost presenter before returning to its start position.",
            },
            "presenter_figure_size": {"type": "string", "label": "Presenter Figure Size"},
            "presenter_custom_size": {"type": "string", "label": "Presenter Custom Size"},
            "presenter_figure_position": {
                "type": "enum",
                "values": ["left", "middle", "right"],
                "label": "Presenter Figure Position",
            },
            "presenter_start_position": {"type": "string", "label": "Tracking Start Position"},
            "zone_start_area": {"type": "string", "label": "Zone Start Area"},
            "focus_mode": {
                "type": "enum",
                "values": ["auto", "manual", "onepush"],
                "label": "Focus Mode",
                "control": True,
            },
            "af_sensitivity": {"type": "enum", "values": ["high", "normal", "low"], "label": "AF Sensitivity"},
            "af_zone": {"type": "enum", "values": ["top", "center", "bottom", "front"], "label": "AF Zone"},
            "exposure_mode": {
                "type": "enum",
                "values": ["fullAuto", "manual", "shutterPriority", "irisPriority", "bright"],
                "label": "Exposure Mode",
            },
            "shutter": {"type": "string", "label": "Shutter"},
            "iris": {"type": "string", "label": "Iris"},
            "gain": {"type": "string", "label": "Gain"},
            "brightness": {"type": "string", "label": "Exposure Brightness"},
            "gain_limit": {"type": "string", "label": "Gain Limit"},
            "exposure_compensation_mode": {"type": "boolean", "label": "Exposure Compensation On"},
            "exposure_compensation": {"type": "string", "label": "Exposure Compensation"},
            "backlight": {"type": "boolean", "label": "Backlight Compensation"},
            "meter": {"type": "enum", "values": ["average", "center", "smart", "top"], "label": "Metering"},
            "drc": {"type": "string", "label": "Dynamic Range Control"},
            "anti_flicker": {"type": "enum", "values": ["off", "50hz", "60hz"], "label": "Anti-Flicker"},
            "wb_mode": {
                "type": "enum",
                "values": ["auto", "indoor", "outdoor", "onepush", "manual", "var"],
                "label": "White Balance Mode",
            },
            "red_gain": {"type": "string", "label": "Red Gain"},
            "blue_gain": {"type": "string", "label": "Blue Gain"},
            "red_gain_tuning": {"type": "string", "label": "Red Gain Tuning"},
            "blue_gain_tuning": {"type": "string", "label": "Blue Gain Tuning"},
            "color_temp": {"type": "string", "label": "Color Temperature"},
            "saturation": {"type": "string", "label": "Saturation"},
            "hue": {"type": "string", "label": "Hue"},
            "luminance": {"type": "integer", "label": "Luminance"},
            "contrast": {"type": "integer", "label": "Contrast"},
            "sharpness": {"type": "integer", "label": "Sharpness"},
            "noise_3d": {"type": "integer", "label": "3D Noise Reduction"},
            "gamma": {"type": "string", "label": "Gamma"},
            "horizontal_flip": {"type": "boolean", "label": "Mirror (Horizontal Flip)"},
            "vertical_flip": {"type": "boolean", "label": "Flip (Vertical Flip)"},
            "digital_zoom": {
                "type": "enum",
                "values": ["off", "2x", "4x", "8x", "16x", "superZoom"],
                "label": "Digital Zoom Limit",
            },
            "hdmi_mode": {"type": "enum", "values": ["hdmi", "dvi"], "label": "HDMI Port Mode"},
            "standby_light": {
                "type": "enum",
                "values": ["flicker", "steadyOn", "normal"],
                "label": "Standby Light",
            },
            "ir_receiver": {"type": "boolean", "label": "IR Receiver", "help": "True while the camera takes commands from its IR remote."},
            "osd_menu": {"type": "boolean", "label": "On-Screen Menu Open"},
            "osd_language": {"type": "string", "label": "Menu Language"},
            "pan_speed_limit": {"type": "integer", "label": "Pan Speed Limit", "min": 0, "max": 24},
            "tilt_speed_limit": {"type": "integer", "label": "Tilt Speed Limit", "min": 0, "max": 20},
            "zoom_speed_limit": {"type": "integer", "label": "Zoom Speed Limit", "min": 0, "max": 7},
            "hdmi_output": {"type": "boolean", "label": "HDMI Output"},
            "sdi_3g_output": {"type": "boolean", "label": "3G-SDI Output"},
            "sdi_12g_output": {"type": "boolean", "label": "12G-SDI Output"},
            "usb_output": {"type": "boolean", "label": "USB Output"},
            "ip_output": {"type": "boolean", "label": "IP Output"},
            "output_resolution": {"type": "string", "label": "Output Format", "help": "The HDMI and SDI output format, such as 2160p60."},
            "stream1_codec": {"type": "string", "label": "Stream 1 Codec"},
            "stream1_width": {"type": "integer", "label": "Stream 1 Width"},
            "stream1_height": {"type": "integer", "label": "Stream 1 Height"},
            "stream1_frame_rate": {"type": "integer", "label": "Stream 1 Frame Rate", "unit": "fps"},
            "stream1_bitrate": {"type": "integer", "label": "Stream 1 Bitrate", "unit": "kbps"},
            "stream1_bitrate_control": {"type": "string", "label": "Stream 1 Bitrate Control"},
            "stream2_codec": {"type": "string", "label": "Stream 2 Codec"},
            "stream2_width": {"type": "integer", "label": "Stream 2 Width"},
            "stream2_height": {"type": "integer", "label": "Stream 2 Height"},
            "stream2_frame_rate": {"type": "integer", "label": "Stream 2 Frame Rate", "unit": "fps"},
            "stream2_bitrate": {"type": "integer", "label": "Stream 2 Bitrate", "unit": "kbps"},
            "stream2_bitrate_control": {"type": "string", "label": "Stream 2 Bitrate Control"},
            "rtmp1_enabled": {"type": "boolean", "label": "RTMP Stream 1", "control": True},
            "rtmp1_status": {"type": "string", "label": "RTMP Stream 1 Status"},
            "rtmp1_message": {"type": "string", "label": "RTMP Stream 1 Message"},
            "rtmp1_url": {"type": "string", "label": "RTMP Stream 1 Server"},
            "rtmp2_enabled": {"type": "boolean", "label": "RTMP Stream 2", "control": True},
            "rtmp2_status": {"type": "string", "label": "RTMP Stream 2 Status"},
            "rtmp2_message": {"type": "string", "label": "RTMP Stream 2 Message"},
            "rtmp2_url": {"type": "string", "label": "RTMP Stream 2 Server"},
            "srt_enabled": {"type": "boolean", "label": "SRT Streaming", "control": True},
            "srt_mode": {"type": "enum", "values": ["listener", "caller"], "label": "SRT Mode"},
            "srt_server": {"type": "string", "label": "SRT Server"},
            "srt_port": {"type": "integer", "label": "SRT Port"},
            "srt_encryption": {
                "type": "enum",
                "values": ["none", "aes128", "aes192", "aes256"],
                "label": "SRT Encryption",
            },
            "srt_bandwidth": {"type": "integer", "label": "SRT Bandwidth Overhead", "unit": "%"},
            "srt_latency": {"type": "integer", "label": "SRT Latency", "unit": "ms"},
            "srt_stream_id": {"type": "string", "label": "SRT Stream ID"},
            "multicast_enabled": {"type": "boolean", "label": "Multicast Streaming", "control": True},
            "multicast_address": {"type": "string", "label": "Multicast Address"},
            "multicast_port": {"type": "integer", "label": "Multicast Port"},
            "onvif_enabled": {"type": "boolean", "label": "ONVIF"},
            "onvif_auth": {"type": "boolean", "label": "ONVIF Authentication"},
            "preview_url": {"type": "string", "label": "Preview URL"},
            "preview_format": {"type": "string", "label": "Preview Format"},
            "preview_status": {"type": "string", "label": "Preview Status"},
            "preview_status_detail": {"type": "string", "label": "Preview Status Detail"},
            "last_error": {
                "type": "string",
                "label": "Last Error",
                "help": "A value the camera did not answer on the last poll; cleared by the next clean poll.",
            },
        },
        "commands": {
            "power_on": {
                "label": "Power On",
                "help": "Wakes the camera from standby.",
                "params": {},
                "sets": {"power": "on"},
            },
            "power_off": {
                "label": "Standby",
                "help": "Puts the camera into standby.",
                "params": {},
                "sets": {"power": "standby"},
            },
            "pt_up": {
                "label": "Tilt Up",
                "help": "Tilts the camera up until Pan/Tilt Stop.",
                "params": {
                    "tilt_speed": {"type": "integer", "min": 1, "max": 20, "label": "Tilt Speed"},
                },
            },
            "pt_down": {
                "label": "Tilt Down",
                "help": "Tilts the camera down until Pan/Tilt Stop.",
                "params": {
                    "tilt_speed": {"type": "integer", "min": 1, "max": 20, "label": "Tilt Speed"},
                },
            },
            "pt_left": {
                "label": "Pan Left",
                "help": "Pans the camera left until Pan/Tilt Stop.",
                "params": {
                    "pan_speed": {"type": "integer", "min": 1, "max": 24, "label": "Pan Speed"},
                },
            },
            "pt_right": {
                "label": "Pan Right",
                "help": "Pans the camera right until Pan/Tilt Stop.",
                "params": {
                    "pan_speed": {"type": "integer", "min": 1, "max": 24, "label": "Pan Speed"},
                },
            },
            "pt_up_left": {
                "label": "Pan Up-Left",
                "help": "Moves the camera up and left until Pan/Tilt Stop.",
                "params": {
                    "pan_speed": {"type": "integer", "min": 1, "max": 24, "label": "Pan Speed"},
                    "tilt_speed": {"type": "integer", "min": 1, "max": 20, "label": "Tilt Speed"},
                },
            },
            "pt_up_right": {
                "label": "Pan Up-Right",
                "help": "Moves the camera up and right until Pan/Tilt Stop.",
                "params": {
                    "pan_speed": {"type": "integer", "min": 1, "max": 24, "label": "Pan Speed"},
                    "tilt_speed": {"type": "integer", "min": 1, "max": 20, "label": "Tilt Speed"},
                },
            },
            "pt_down_left": {
                "label": "Pan Down-Left",
                "help": "Moves the camera down and left until Pan/Tilt Stop.",
                "params": {
                    "pan_speed": {"type": "integer", "min": 1, "max": 24, "label": "Pan Speed"},
                    "tilt_speed": {"type": "integer", "min": 1, "max": 20, "label": "Tilt Speed"},
                },
            },
            "pt_down_right": {
                "label": "Pan Down-Right",
                "help": "Moves the camera down and right until Pan/Tilt Stop.",
                "params": {
                    "pan_speed": {"type": "integer", "min": 1, "max": 24, "label": "Pan Speed"},
                    "tilt_speed": {"type": "integer", "min": 1, "max": 20, "label": "Tilt Speed"},
                },
            },
            "pt_stop": {"label": "Pan/Tilt Stop", "help": "Stops pan and tilt movement.", "params": {}},
            "pt_home": {
                "label": "Home",
                "help": "Returns the camera to its home position.",
                "params": {},
            },
            "pt_absolute": {
                "label": "Move To Position",
                "help": (
                    "Moves the camera to a pan (about -170 to 170), a tilt (about -30 "
                    "to 90) and a zoom (0 wide to 4912)."
                ),
                "params": {
                    "pan": {"type": "integer", "required": True, "label": "Pan"},
                    "tilt": {"type": "integer", "required": True, "label": "Tilt"},
                    "zoom": {"type": "integer", "required": True, "min": 0, "max": 4912, "label": "Zoom"},
                    "pan_speed": {"type": "integer", "min": 0, "max": 24, "label": "Pan Speed"},
                    "tilt_speed": {"type": "integer", "min": 0, "max": 20, "label": "Tilt Speed"},
                    "zoom_speed": {"type": "integer", "min": 0, "max": 7, "label": "Zoom Speed"},
                },
            },
            "pt_relative": {
                "label": "Move By",
                "help": "Moves the camera's pan and tilt by the amounts given from where it is now.",
                "params": {
                    "pan": {"type": "integer", "required": True, "label": "Pan"},
                    "tilt": {"type": "integer", "required": True, "label": "Tilt"},
                    "pan_speed": {"type": "integer", "min": 0, "max": 24, "label": "Pan Speed"},
                    "tilt_speed": {"type": "integer", "min": 0, "max": 20, "label": "Tilt Speed"},
                },
            },
            "pt_recalibrate": {
                "label": "Recalibrate Pan/Tilt",
                "help": "Re-initializes the camera's pan/tilt head.",
                "params": {},
            },
            "zoom_in": {
                "label": "Zoom In",
                "help": "Zooms in (tele) until Zoom Stop.",
                "params": {"speed": {"type": "integer", "min": 1, "max": 7, "label": "Speed"}},
            },
            "zoom_out": {
                "label": "Zoom Out",
                "help": "Zooms out (wide) until Zoom Stop.",
                "params": {"speed": {"type": "integer", "min": 1, "max": 7, "label": "Speed"}},
            },
            "zoom_stop": {"label": "Zoom Stop", "help": "Stops zooming.", "params": {}},
            "zoom_direct": {
                "label": "Zoom To Position",
                "help": "Zooms to a position, 0 fully wide to 4912, keeping pan and tilt where they are.",
                "params": {
                    "position": {"type": "integer", "required": True, "min": 0, "max": 4912, "label": "Zoom Position"},
                },
            },
            "focus_auto": {
                "label": "Auto Focus",
                "help": "Turns auto focus on.",
                "params": {},
                "sets": {"focus_mode": "auto"},
            },
            "focus_manual": {
                "label": "Manual Focus",
                "help": "Turns auto focus off so focus holds where it is.",
                "params": {},
                "sets": {"focus_mode": "manual"},
            },
            "focus_one_push": {
                "label": "One Push Focus",
                "help": "Focuses once on the current picture.",
                "params": {},
            },
            "focus_near": {
                "label": "Focus Near",
                "help": "Moves focus nearer until Focus Stop (manual focus).",
                "params": {"speed": {"type": "integer", "min": 1, "max": 7, "label": "Speed"}},
            },
            "focus_far": {
                "label": "Focus Far",
                "help": "Moves focus farther until Focus Stop (manual focus).",
                "params": {"speed": {"type": "integer", "min": 1, "max": 7, "label": "Speed"}},
            },
            "focus_stop": {"label": "Focus Stop", "help": "Stops a focus move.", "params": {}},
            "focus_direct": {
                "label": "Focus To Position",
                "help": "Sets the focus position, 236 to 4148 (manual focus).",
                "params": {
                    "position": {"type": "integer", "required": True, "min": 236, "max": 4148, "label": "Focus Position"},
                },
            },
            "preset_recall": {
                "label": "Recall Preset",
                "help": "Moves the camera to a stored preset position.",
                "params": {
                    "preset": {"type": "integer", "required": True, "min": 0, "max": 254, "label": "Preset"},
                },
            },
            "preset_save": {
                "label": "Save Preset",
                "help": "Stores the camera's current position as a preset, replacing what was stored there.",
                "params": {
                    "preset": {"type": "integer", "required": True, "min": 0, "max": 254, "label": "Preset"},
                },
            },
            "preset_clear": {
                "label": "Delete Preset",
                "help": "Deletes a stored preset position.",
                "confirm": "Deletes the camera position stored in this preset.",
                "params": {
                    "preset": {"type": "integer", "required": True, "min": 0, "max": 254, "label": "Preset"},
                },
            },
            "preset_speed": {
                "label": "Set Preset Speed",
                "help": "Sets how fast the camera moves to a recalled preset, 0 to 24.",
                "params": {
                    "level": {"type": "integer", "required": True, "min": 0, "max": 24, "label": "Speed"},
                },
            },
            "tracking_on": {
                "label": "Tracking On",
                "help": "Turns AI auto-tracking on.",
                "params": {},
                "sets": {"tracking": True},
            },
            "tracking_off": {
                "label": "Tracking Off",
                "help": "Turns AI auto-tracking off.",
                "params": {},
                "sets": {"tracking": False},
            },
            "select_presenter": {
                "label": "Select Presenter",
                "help": (
                    "Moves the tracking selection to the person on the left or right "
                    "of the one selected now, or confirms the selected person as the "
                    "one to follow."
                ),
                "params": {
                    "selection": {
                        "type": "enum",
                        "required": True,
                        "label": "Selection",
                        "values": [
                            {"value": "left", "label": "Left"},
                            {"value": "right", "label": "Right"},
                            {"value": "confirm", "label": "Confirm"},
                        ],
                        "default": "confirm",
                    },
                },
            },
            "tally_on": {
                "label": "Tally On",
                "help": "Lights the camera's tally light.",
                "params": {},
                "sets": {"tally": "on"},
            },
            "tally_off": {
                "label": "Tally Off",
                "help": "Turns the camera's tally light off.",
                "params": {},
                "sets": {"tally": "off"},
            },
            "tally_blink": {
                "label": "Tally Blink",
                "help": "Makes the camera's tally light blink.",
                "params": {},
                "sets": {"tally": "blink"},
            },
            "record_start": {
                "label": "Start Recording",
                "help": "Starts recording on the camera. With no file name the camera names the file by date and time.",
                "params": {
                    "filename": {"type": "string", "label": "File Name"},
                },
                "sets": {"recording": True},
            },
            "record_stop": {
                "label": "Stop Recording",
                "help": "Stops recording.",
                "params": {},
                "sets": {"recording": False},
            },
            "freeze_on": {
                "label": "Freeze On",
                "help": "Freezes the camera's video output on the current picture.",
                "params": {},
                "sets": {"freeze": True},
            },
            "freeze_off": {
                "label": "Freeze Off",
                "help": "Returns the camera's video output to live.",
                "params": {},
                "sets": {"freeze": False},
            },
            "wb_one_push_trigger": {
                "label": "One Push White Balance",
                "help": "Sets white balance once from what the camera sees now (One Push white balance mode).",
                "params": {},
            },
            "exposure_adjust": {
                "label": "Adjust Exposure",
                "help": "Steps one exposure value up or down, or returns it to its default.",
                "params": {
                    "target": {
                        "type": "enum",
                        "required": True,
                        "label": "Value",
                        "values": [
                            {"value": "shutter", "label": "Shutter"},
                            {"value": "iris", "label": "Iris"},
                            {"value": "gain", "label": "Gain"},
                            {"value": "brightness", "label": "Brightness"},
                            {"value": "gainLimit", "label": "Gain Limit"},
                            {"value": "exposureCompensation", "label": "Exposure Compensation"},
                            {"value": "dynamicRangeControl", "label": "Dynamic Range Control"},
                        ],
                    },
                    "op": {
                        "type": "enum",
                        "required": True,
                        "label": "Change",
                        "values": [
                            {"value": "inc", "label": "Up"},
                            {"value": "dec", "label": "Down"},
                            {"value": "reset", "label": "Default"},
                        ],
                    },
                    "step": {"type": "integer", "min": 1, "default": 1, "label": "Steps"},
                },
            },
            "color_adjust": {
                "label": "Adjust Color",
                "help": "Steps one color value up or down, or returns it to its default.",
                "params": {
                    "target": {
                        "type": "enum",
                        "required": True,
                        "label": "Value",
                        "values": [
                            {"value": "redGain", "label": "Red Gain"},
                            {"value": "blueGain", "label": "Blue Gain"},
                            {"value": "redGainTuning", "label": "Red Gain Tuning"},
                            {"value": "blueGainTuning", "label": "Blue Gain Tuning"},
                            {"value": "colorTemp", "label": "Color Temperature"},
                            {"value": "saturation", "label": "Saturation"},
                            {"value": "hue", "label": "Hue"},
                        ],
                    },
                    "op": {
                        "type": "enum",
                        "required": True,
                        "label": "Change",
                        "values": [
                            {"value": "inc", "label": "Up"},
                            {"value": "dec", "label": "Down"},
                            {"value": "reset", "label": "Default"},
                        ],
                    },
                    "step": {"type": "integer", "min": 1, "default": 1, "label": "Steps"},
                },
            },
            "picture_adjust": {
                "label": "Adjust Picture",
                "help": "Steps one picture value up or down, or returns it to its default.",
                "params": {
                    "target": {
                        "type": "enum",
                        "required": True,
                        "label": "Value",
                        "values": [
                            {"value": "luminance", "label": "Luminance"},
                            {"value": "contrast", "label": "Contrast"},
                            {"value": "sharpness", "label": "Sharpness"},
                            {"value": "noise3d", "label": "3D Noise Reduction"},
                        ],
                    },
                    "op": {
                        "type": "enum",
                        "required": True,
                        "label": "Change",
                        "values": [
                            {"value": "inc", "label": "Up"},
                            {"value": "dec", "label": "Down"},
                            {"value": "reset", "label": "Default"},
                        ],
                    },
                    "step": {"type": "integer", "min": 1, "default": 1, "label": "Steps"},
                },
            },
            "osd_menu": {
                "label": "On-Screen Menu",
                "help": "Opens, closes or moves through the camera's on-screen menu.",
                "params": {
                    "action": {
                        "type": "enum",
                        "required": True,
                        "label": "Action",
                        "values": [
                            {"value": "on", "label": "Open"},
                            {"value": "off", "label": "Close"},
                            {"value": "toggle", "label": "Open or Close"},
                            {"value": "enter", "label": "Enter"},
                            {"value": "back", "label": "Back"},
                            {"value": "up", "label": "Up"},
                            {"value": "down", "label": "Down"},
                            {"value": "left", "label": "Left"},
                            {"value": "right", "label": "Right"},
                        ],
                    },
                },
            },
            "srt_start": {
                "label": "Start SRT",
                "help": "Starts the camera's SRT stream with its current SRT settings.",
                "params": {},
                "sets": {"srt_enabled": True},
            },
            "srt_stop": {
                "label": "Stop SRT",
                "help": "Stops the camera's SRT stream.",
                "params": {},
                "sets": {"srt_enabled": False},
            },
            "multicast_start": {
                "label": "Start Multicast",
                "help": "Starts the camera's multicast stream to its multicast address and port.",
                "params": {},
                "sets": {"multicast_enabled": True},
            },
            "multicast_stop": {
                "label": "Stop Multicast",
                "help": "Stops the camera's multicast stream.",
                "params": {},
                "sets": {"multicast_enabled": False},
            },
            "rtmp_start": {
                "label": "Start RTMP",
                "help": (
                    "Starts publishing an RTMP stream to its server. The stream key is "
                    "entered on the camera's Network > RTMP(S) page."
                ),
                "params": {
                    "stream": {
                        "type": "enum",
                        "required": True,
                        "label": "Stream",
                        "values": [{"value": "1", "label": "Stream 1"}, {"value": "2", "label": "Stream 2"}],
                        "default": "1",
                    },
                },
            },
            "rtmp_stop": {
                "label": "Stop RTMP",
                "help": "Stops publishing an RTMP stream.",
                "params": {
                    "stream": {
                        "type": "enum",
                        "required": True,
                        "label": "Stream",
                        "values": [{"value": "1", "label": "Stream 1"}, {"value": "2", "label": "Stream 2"}],
                        "default": "1",
                    },
                },
            },
            "save_settings": {
                "label": "Save Settings",
                "help": "Saves the camera's current settings.",
                "params": {},
            },
            "restore_defaults": {
                "label": "Restore Default Settings",
                "help": "Returns the camera's settings to their factory values.",
                "confirm": "Returns the camera's settings to their factory values.",
                "params": {},
            },
            "factory_reset": {
                "label": "Factory Reset",
                "help": (
                    "Resets the whole camera to its factory state. Keep Network keeps "
                    "its IP address and network settings; Everything resets those too."
                ),
                "confirm": (
                    "Resets the whole camera to its factory state. Everything also "
                    "resets the IP address, so the camera may leave this network."
                ),
                # Not measured: whether the camera restarts after a factory
                # reset, and for how long, waits for a unit on the bench.
                "restarts_device_for": 120,
                "params": {
                    "keep_network": {
                        "type": "enum",
                        "required": True,
                        "label": "What To Reset",
                        "values": [
                            {"value": "true", "label": "Keep Network"},
                            {"value": "false", "label": "Everything"},
                        ],
                        "default": "true",
                    },
                },
            },
        },
        "device_settings": {
            "focus_mode": {
                "type": "enum", "label": "Focus Mode", "state_key": "focus_mode",
                "values": ["auto", "manual"], "default": "auto", "setup": False,
                "help": "auto: the camera focuses by itself. manual: focus holds where it is set.",
            },
            "af_sensitivity": {
                "type": "enum", "label": "AF Sensitivity", "state_key": "af_sensitivity",
                "values": ["high", "normal", "low"], "default": "normal", "setup": False,
                "help": "How quickly auto focus reacts to a change in the picture.",
            },
            "af_zone": {
                "type": "enum", "label": "AF Zone", "state_key": "af_zone",
                "values": ["top", "center", "bottom", "front"], "default": "center", "setup": False,
                "help": "The part of the picture auto focus works on.",
            },
            "exposure_mode": {
                "type": "enum", "label": "Exposure Mode", "state_key": "exposure_mode",
                "values": ["fullAuto", "manual", "shutterPriority", "irisPriority", "bright"],
                "default": "fullAuto", "setup": False,
                "help": (
                    "fullAuto: the camera sets exposure. shutterPriority / irisPriority: "
                    "that one is fixed. manual: shutter, iris and gain are fixed. bright: "
                    "brightness is set by hand."
                ),
            },
            "exposure_compensation_mode": {
                "type": "boolean", "label": "Exposure Compensation On", "state_key": "exposure_compensation_mode",
                "default": False, "setup": False,
                "help": "Turns on exposure compensation, set with Adjust Exposure (auto exposure).",
            },
            "backlight": {
                "type": "boolean", "label": "Backlight Compensation", "state_key": "backlight",
                "default": False, "setup": False,
                "help": "Brightens a subject in front of a bright background.",
            },
            "meter": {
                "type": "enum", "label": "Metering", "state_key": "meter",
                "values": ["average", "center", "smart", "top"], "default": "average", "setup": False,
                "help": "The part of the picture auto exposure measures.",
            },
            "anti_flicker": {
                "type": "enum", "label": "Anti-Flicker", "state_key": "anti_flicker",
                "values": ["off", "50hz", "60hz"], "default": "off", "setup": False,
                "help": "Matches the shutter to the mains frequency of the room's lighting.",
            },
            "wb_mode": {
                "type": "enum", "label": "White Balance Mode", "state_key": "wb_mode",
                "values": ["auto", "indoor", "outdoor", "onepush", "manual", "var"],
                "default": "auto", "setup": False,
                "help": (
                    "auto, indoor or outdoor: preset balance. onepush: set once with One "
                    "Push White Balance. manual: red and blue gain by hand. var: color "
                    "temperature by hand."
                ),
            },
            "luminance": {
                "type": "integer", "label": "Luminance", "state_key": "luminance",
                "default": 7, "setup": False,
                "help": "The brightness of the picture.",
            },
            "contrast": {
                "type": "integer", "label": "Contrast", "state_key": "contrast",
                "default": 7, "setup": False,
                "help": "The contrast of the picture.",
            },
            "sharpness": {
                "type": "integer", "label": "Sharpness", "state_key": "sharpness",
                "default": 6, "setup": False,
                "help": "The sharpness of the picture.",
            },
            "noise_3d": {
                "type": "integer", "label": "3D Noise Reduction", "state_key": "noise_3d",
                "default": 6, "setup": False,
                "help": "How strongly the camera reduces noise in the picture.",
            },
            "horizontal_flip": {
                "type": "boolean", "label": "Mirror (Horizontal Flip)", "state_key": "horizontal_flip",
                "default": False, "setup": False,
                "help": "Mirrors the picture left to right.",
            },
            "vertical_flip": {
                "type": "boolean", "label": "Flip (Vertical Flip)", "state_key": "vertical_flip",
                "default": False, "setup": False,
                "help": "Turns the picture upside down, for a camera mounted upside down.",
            },
            "digital_zoom": {
                "type": "enum", "label": "Digital Zoom Limit", "state_key": "digital_zoom",
                "values": ["off", "2x", "4x", "8x", "16x", "superZoom"], "default": "off", "setup": False,
                "help": "How far zoom may go past the optical zoom. off: optical zoom only.",
            },
            "hdmi_mode": {
                "type": "enum", "label": "HDMI Port Mode", "state_key": "hdmi_mode",
                "values": ["hdmi", "dvi"], "default": "hdmi", "setup": False,
                "help": "The signal the HDMI port sends: hdmi, or dvi for a display that needs it.",
            },
            "standby_light": {
                "type": "enum", "label": "Standby Light", "state_key": "standby_light",
                "values": ["flicker", "steadyOn", "normal"], "default": "normal", "setup": False,
                "help": "How the front light shows standby.",
            },
            "ir_receiver": {
                "type": "boolean", "label": "IR Receiver", "state_key": "ir_receiver",
                "default": True, "setup": False,
                "help": "Whether the camera takes commands from its IR remote.",
            },
            "tally_enabled": {
                "type": "boolean", "label": "Tally Enabled", "state_key": "tally_enabled",
                "default": True, "setup": False,
                "help": "Whether the camera's tally light is in use.",
            },
            "tally_color": {
                "type": "enum", "label": "Tally Color", "state_key": "tally_color",
                "values": ["red", "green"], "default": "red", "setup": False,
                "help": "The color of the tally light.",
            },
            "tracking_mode": {
                "type": "enum", "label": "Tracking Mode", "state_key": "tracking_mode",
                "values": ["tracking", "zone"], "default": "tracking", "setup": False,
                "help": "tracking: follows a presenter. zone: moves between the saved zone positions A to D.",
            },
            "tracking_debug_mode": {
                "type": "enum", "label": "Tracking Debug Mode", "state_key": "tracking_debug_mode",
                "values": ["default", "debug", "off"], "default": "default", "setup": False,
                "help": "How the camera picks a presenter when several people are in view.",
            },
            "tracking_hint": {
                "type": "boolean", "label": "Tracking Hint", "state_key": "tracking_hint",
                "default": False, "setup": False,
                "help": "Draws the tracking hint on the video output.",
            },
            "tracking_lost_timeout": {
                "type": "integer", "label": "Target Retention Time", "state_key": "tracking_lost_timeout",
                "max": 10, "default": 6, "setup": False,
                "help": "Seconds tracking waits for a lost presenter before returning to its start position, up to 10.",
            },
            "presenter_figure_position": {
                "type": "enum", "label": "Presenter Figure Position", "state_key": "presenter_figure_position",
                "values": ["left", "middle", "right"], "default": "middle", "setup": False,
                "help": "Where the presenter is kept in the frame.",
            },
            "pan_speed_limit": {
                "type": "integer", "label": "Pan Speed Limit", "state_key": "pan_speed_limit",
                "min": 0, "max": 24, "default": 24, "setup": False,
                "help": "The camera's pan speed setting, 0 to 24.",
            },
            "tilt_speed_limit": {
                "type": "integer", "label": "Tilt Speed Limit", "state_key": "tilt_speed_limit",
                "min": 0, "max": 20, "default": 20, "setup": False,
                "help": "The camera's tilt speed setting, 0 to 20.",
            },
            "zoom_speed_limit": {
                "type": "integer", "label": "Zoom Speed Limit", "state_key": "zoom_speed_limit",
                "min": 0, "max": 7, "default": 5, "setup": False,
                "help": "The camera's zoom speed setting, 0 to 7.",
            },
            "srt_mode": {
                "type": "enum", "label": "SRT Mode", "state_key": "srt_mode",
                "values": ["listener", "caller"], "default": "listener", "setup": False,
                "help": "listener: the receiver connects to the camera. caller: the camera connects to the SRT Server.",
            },
            "srt_server": {
                "type": "string", "label": "SRT Server", "state_key": "srt_server",
                "default": "", "setup": False,
                "help": "The address the camera sends to in caller mode.",
            },
            "srt_port": {
                "type": "integer", "label": "SRT Port", "state_key": "srt_port",
                "min": 1, "max": 65535, "default": 4578, "setup": False,
                "help": "The SRT stream's network port.",
            },
            "srt_encryption": {
                "type": "enum", "label": "SRT Encryption", "state_key": "srt_encryption",
                "values": ["none", "aes128", "aes192", "aes256"], "default": "none", "setup": False,
                "help": "Must match the receiver. The password is entered on the camera's Network > SRT page.",
            },
            "srt_bandwidth": {
                "type": "integer", "label": "SRT Bandwidth Overhead", "state_key": "srt_bandwidth",
                "default": 25, "setup": False,
                "help": "The percentage of bandwidth kept for recovering lost packets.",
            },
            "srt_latency": {
                "type": "integer", "label": "SRT Latency", "state_key": "srt_latency",
                "default": 500, "setup": False,
                "help": "SRT latency in milliseconds. Must match the receiver.",
            },
            "srt_stream_id": {
                "type": "string", "label": "SRT Stream ID", "state_key": "srt_stream_id",
                "default": "", "setup": False,
                "help": "Must match the receiver, when it asks for one.",
            },
            "multicast_address": {
                "type": "string", "label": "Multicast Address", "state_key": "multicast_address",
                "default": "", "setup": False,
                "help": "The group address the multicast stream is sent to.",
            },
            "multicast_port": {
                "type": "integer", "label": "Multicast Port", "state_key": "multicast_port",
                "min": 1, "max": 65535, "default": 6688, "setup": False,
                "help": "The multicast stream's network port.",
            },
            "onvif_enabled": {
                "type": "boolean", "label": "ONVIF", "state_key": "onvif_enabled",
                "default": False, "setup": False,
                "help": "Whether the camera answers ONVIF clients.",
            },
            "onvif_auth": {
                "type": "boolean", "label": "ONVIF Authentication", "state_key": "onvif_auth",
                "default": False, "setup": False,
                "help": "Whether ONVIF clients must sign in.",
            },
            "rtmp1_url": {
                "type": "string", "label": "RTMP Stream 1 Server", "state_key": "rtmp1_url",
                "default": "", "setup": False,
                "help": "The RTMP or RTMPS server URL stream 1 publishes to. The stream key is entered on the camera.",
            },
            "rtmp2_url": {
                "type": "string", "label": "RTMP Stream 2 Server", "state_key": "rtmp2_url",
                "default": "", "setup": False,
                "help": "The RTMP or RTMPS server URL stream 2 publishes to. The stream key is entered on the camera.",
            },
        },
        "quick_actions": ["power_on", "power_off", "pt_home", "tracking_on", "tracking_off"],
        "actions": [
            {"id": "power_on", "kind": "command", "icon": "power"},
            {"id": "power_off", "kind": "command", "icon": "moon"},
            {"id": "pt_home", "kind": "command", "icon": "home"},
            {"id": "tracking_on", "kind": "command", "icon": "scan-face"},
            {"id": "tracking_off", "kind": "command", "icon": "scan"},
        ],
    }

    HEALTH_FAULT_MESSAGE = "The camera stopped answering its REST API."

    _client: httpx.AsyncClient | None = None

    def __init__(self, device_id: str, config: dict[str, Any], state: Any, events: Any) -> None:
        super().__init__(device_id, config, state, events)
        self._client = None
        self._auth: httpx.DigestAuth | None = None
        self._lock = asyncio.Lock()
        self._slow_due = 0.0
        # Resources this camera answered 404; not asked again on this connection.
        self._unsupported: set[str] = set()
        # The last answer from each resource.
        self._values: dict[str, dict[str, Any]] = {}
        # Reads the camera refused, by resource: named in Last Error by every
        # poll until the resource answers again.
        self._refused: dict[str, str] = {}
        self._power_task: asyncio.Task | None = None
        self._move_task: asyncio.Task | None = None

    # ── Config accessors ──

    @property
    def _host(self) -> str:
        return str(self.config.get("host", "") or "").strip()

    @property
    def _port(self) -> int:
        return _as_int(self.config.get("port"), 80) or 80

    def _speed(self, params: dict[str, Any], param: str, config_key: str, default: int, low: int, top: int) -> int:
        """The move's own speed parameter, else the configured speed."""
        speed = params.get(param)
        if speed is None or speed == "":
            speed = self.config.get(config_key, default)
        return max(low, min(top, _as_int(speed, default)))

    def _config_speed(self, config_key: str, default: int, low: int, top: int) -> int:
        return self._speed({}, "", config_key, default, low, top)

    def _auth_fault(self) -> ConnectionFaultError:
        if not str(self.config.get("password", "") or ""):
            return ConnectionFaultError("The camera needs its web password and none is entered.", code="auth_failed")
        return ConnectionFaultError("The camera refused the user name and password.", code="auth_failed")

    # ── Connection lifecycle ──

    async def _create_transport(self, transport_type: str) -> None:
        if not self._host:
            raise ConnectionFaultError("No IP address is entered.", code="invalid_config")
        username = str(self.config.get("username", "") or "")
        password = str(self.config.get("password", "") or "")
        if not username or not password:
            # Never send a sign-in that cannot succeed: every account on the
            # camera has a password.
            raise self._auth_fault()
        if not await self._verify_reachable(self._host, self._port):
            raise ConnectionError(f"{self._host}:{self._port} is not responding")
        self._auth = httpx.DigestAuth(username, password)
        self._client = httpx.AsyncClient(
            base_url=f"http://{self._host}:{self._port}",
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=False,
        )

    async def _post_connect(self) -> None:
        self._unsupported = set()
        self._values = {}
        self._refused = {}
        self._slow_due = 0.0
        try:
            await self._read(CORE)
        except CameraRefused as exc:
            if exc.http_status == 403:
                raise self._auth_fault() from exc
            if 300 <= exc.http_status < 400:
                raise ConnectionFaultError(
                    f"The camera redirected the request ({exc}). Check the port.",
                    code="invalid_config",
                ) from exc
            if exc.http_status == 404 or exc.http_status == 200:
                raise ConnectionFaultError(
                    f"The device at {self._host} answered, but not with the EC20 REST API ({exc}).",
                    code="invalid_config",
                ) from exc
            raise ConnectionError(f"The camera answered its status read with {exc}.") from exc
        log.info(f"[{self.device_id}] Connected to Epiphan EC20 at {self._host}:{self._port}")
        self._update_preview()

    async def _initial_sync(self) -> None:
        await self.poll()

    def _link_alive(self) -> bool:
        return self._client is not None

    async def _close_session(self) -> None:
        self._cancel_watch("_power_task")
        self._cancel_watch("_move_task")
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _liveness_probe(self) -> None:
        # The platform holds the deadline: on a reconnect it waits up to
        # HEALTH_INTERVAL_S for a camera still starting up, so this request
        # is given as long.
        try:
            await self._request("GET", CORE, timeout=self.HEALTH_INTERVAL_S)
        except CameraRefused:
            return  # it answered: the link is up

    # ── HTTP plumbing ──

    async def _request(
        self,
        method: str,
        resource: str,
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any] | None:
        """One request under the lock. A GET returns the answer's JSON object;
        a write returns None once the camera answers 2xx. A 401 raises the
        typed auth_failed; any other answer raises CameraRefused; a dead link
        raises ConnectionError."""
        client = self._client
        if client is None:
            raise ConnectionError("Not connected")
        kwargs: dict[str, Any] = {"auth": self._auth}
        if body is not None:
            kwargs["json"] = body
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(timeout, connect=5.0)
        async with self._lock:
            try:
                resp = await client.request(method, API + resource, **kwargs)
            except httpx.TransportError as exc:
                raise ConnectionError(f"{self._host} is not responding: {exc}") from exc
        if resp.status_code == 401:
            raise self._auth_fault()
        if not 200 <= resp.status_code < 300:
            text = resp.text.strip()
            detail = f": {text}" if text and len(text) <= 160 and not text.startswith("<") else ""
            raise CameraRefused(f"HTTP {resp.status_code}{detail}", http_status=resp.status_code)
        if method != "GET":
            return None
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise CameraRefused("an answer that is not the API's JSON", http_status=resp.status_code)
        return payload

    # ── Reads ──

    async def _read(self, resource: str) -> dict[str, Any] | None:
        """Read one resource into state. None when this camera has no such
        resource (it is not asked again on this connection)."""
        if resource in self._unsupported:
            return None
        try:
            payload = await self._request("GET", resource)
        except CameraRefused as exc:
            if exc.http_status == 404 and resource != CORE:
                log.info(f"[{self.device_id}] This camera has no {resource}; not asked again")
                self._unsupported.add(resource)
                self._refused.pop(resource, None)
                return None
            raise
        if payload is None:
            return None
        self._values[resource] = payload
        self._refused.pop(resource, None)
        self._apply(resource, payload)
        return payload

    def _apply(self, resource: str, payload: dict[str, Any]) -> None:
        updates: dict[str, Any] = {}
        for path, key, kind in READS.get(resource, ()):
            value = _read_value(_field(payload, path), kind)
            if value is not None:
                updates[key] = value
        if updates:
            self.set_states(updates)
        if resource == "/video/param":
            self._update_preview()

    def _update_preview(self) -> None:
        """The camera's RTSP stream for the Video Panel."""
        if not self._host:
            return
        ip_output = _field(self._values.get("/video/param", {}), "hardwareOutput.ip")
        if ip_output is not None and not _as_bool(ip_output):
            self.set_states({
                "preview_url": "",
                "preview_format": "",
                "preview_status": "unavailable",
                "preview_status_detail": "The camera's IP output is turned off.",
            })
            return
        stream = "1" if str(self.config.get("preview_stream", "2")).strip() == "1" else "2"
        port = _as_int(self.config.get("rtsp_port"), 554) or 554
        where = self._host if port == 554 else f"{self._host}:{port}"
        self.set_states({
            "preview_url": f"rtsp://{where}/{stream}",
            "preview_format": "rtsp",
            "preview_status": "",
            "preview_status_detail": "",
        })

    async def poll(self) -> None:
        """Power, position, recording, freeze, tally and tracking every poll;
        the rest every 30 s. A dead link, or a refused status read, raises
        (the platform counts it); any other read the camera refuses is named
        in last_error, written again by every poll while it holds."""
        if self._client is None:
            return
        now = time.monotonic()
        slow = now >= self._slow_due
        resources = FAST + SLOW if slow else FAST
        for resource in resources:
            try:
                await self._read(resource)
            except CameraRefused as exc:
                if resource == CORE:
                    raise ConnectionError(f"The camera answered its status read with {exc}.") from exc
                self._refused[resource] = str(exc)
        if slow:
            self._slow_due = now + SLOW_INTERVAL_S
        if self._refused:
            what = ", ".join(
                f"{RESOURCE_LABELS.get(resource, resource)} ({why})" for resource, why in self._refused.items()
            )
            self.set_state("last_error", f"The camera did not answer {what}; those values are not updated.")

    # ── Watches ──

    def _cancel_watch(self, attr: str) -> None:
        task = getattr(self, attr)
        setattr(self, attr, None)
        if task is not None and not task.done():
            task.cancel()

    def _watch_power(self, target: str) -> None:
        """Read the power state every few seconds until the camera reports
        ``target``."""
        self._cancel_watch("_power_task")

        async def _run() -> None:
            deadline = time.monotonic() + POWER_WATCH_S
            while time.monotonic() < deadline:
                await asyncio.sleep(POWER_WATCH_STEP_S)
                try:
                    await self._read(CORE)
                except Exception as exc:  # the poll reports a dead link
                    log.debug(f"[{self.device_id}] Power read-back stopped: {exc}")
                    return
                if self.get_state("power") == target:
                    return

        self._power_task = asyncio.create_task(_run())

    def _watch_move(self, resource: str = POSITION) -> None:
        """Read a position (pan/tilt/zoom, or focus) until two reads in a row
        agree (the camera has stopped), so a move's end position shows
        without waiting for a poll."""
        self._cancel_watch("_move_task")

        async def _run() -> None:
            deadline = time.monotonic() + MOVE_WATCH_S
            last: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                await asyncio.sleep(MOVE_WATCH_STEP_S)
                try:
                    payload = await self._read(resource)
                except Exception as exc:  # the poll reports a dead link
                    log.debug(f"[{self.device_id}] Position read-back stopped: {exc}")
                    return
                if payload is None or payload == last:
                    return
                last = payload

        self._move_task = asyncio.create_task(_run())

    # ── Writes ──

    async def _write(
        self,
        method: str,
        resource: str,
        body: dict[str, Any] | None,
        what: str,
        read_back: str | None = None,
        expect: tuple[str, Any] | None = None,
    ) -> None:
        """Send a write, then read back ``read_back`` (the resource itself by
        default for a POST with a body). With ``expect`` (a state key and the
        value it should read), a write the camera answers and does not apply
        is refused, after one more read a second later."""
        try:
            await self._request(method, resource, body)
        except CameraRefused as exc:
            raise CommandRefusedError(f"The camera refused {what} ({exc}).") from exc
        target = read_back if read_back is not None else (resource if body is not None else None)
        if not target:
            return
        try:
            await self._read(target)
        except CameraRefused as exc:
            log.debug(f"[{self.device_id}] Read-back of {target} not answered: {exc}")
            return
        if expect is None:
            return
        key, wanted = expect
        if _same(self.get_state(key), wanted):
            return
        await asyncio.sleep(READ_BACK_RETRY_S)
        try:
            await self._read(target)
        except CameraRefused:
            return
        reported = self.get_state(key)
        if reported is not None and not _same(reported, wanted):
            raise CommandRefusedError(f"The camera accepted {what} but still reports {reported}.")

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        if command in ("power_on", "power_off"):
            target = "on" if command == "power_on" else "standby"
            await self._write(
                "POST", CORE, {"action": "on" if target == "on" else "off"},
                "Power On" if target == "on" else "Standby", read_back="",
            )
            try:
                await self._read(CORE)
            except CameraRefused as exc:
                log.debug(f"[{self.device_id}] Power read-back not answered: {exc}")
            if self.get_state("power") != target:
                self._watch_power(target)
        elif command in PT_DIRECTIONS:
            body = {
                "action": PT_DIRECTIONS[command],
                "pan_speed": str(self._speed(params, "pan_speed", "pan_speed", 12, 1, 24)),
                "tilt_speed": str(self._speed(params, "tilt_speed", "tilt_speed", 10, 1, 20)),
            }
            await self._write("POST", "/movement/relative/pan-tilt", body, "the pan/tilt move", read_back="")
        elif command == "pt_stop":
            await self._write("POST", "/movement/relative/pan-tilt", {"action": "stop"}, "Pan/Tilt Stop", read_back="")
            self._watch_move()
        elif command == "pt_home":
            await self._write("POST", "/presets/home", None, "Home")
            self._watch_move()
        elif command == "pt_absolute":
            body = {
                "panPos": str(_as_int(params.get("pan"), 0)),
                "tiltPos": str(_as_int(params.get("tilt"), 0)),
                "zoomPos": str(_as_int(params.get("zoom"), 0)),
                "panSpeed": str(self._speed(params, "pan_speed", "pan_speed", 12, 0, 24)),
                "tiltSpeed": str(self._speed(params, "tilt_speed", "tilt_speed", 10, 0, 20)),
                "zoomSpeed": str(self._speed(params, "zoom_speed", "zoom_speed", 4, 0, 7)),
            }
            await self._write("POST", POSITION, body, "the move to a position", read_back="")
            self._watch_move()
        elif command == "pt_relative":
            body = {
                "panPos": str(_as_int(params.get("pan"), 0)),
                "tiltPos": str(_as_int(params.get("tilt"), 0)),
                "panSpeed": str(self._speed(params, "pan_speed", "pan_speed", 12, 0, 24)),
                "tiltSpeed": str(self._speed(params, "tilt_speed", "tilt_speed", 10, 0, 20)),
            }
            await self._write("POST", "/movement/relative/position", body, "the move", read_back="")
            self._watch_move()
        elif command == "pt_recalibrate":
            await self._write("POST", "/movement/reset", None, "Recalibrate Pan/Tilt")
            self._watch_move()
        elif command in ("zoom_in", "zoom_out"):
            body = {
                "action": "tele" if command == "zoom_in" else "wide",
                "speed": str(self._speed(params, "speed", "zoom_speed", 4, 1, 7)),
                "type": "variable",
            }
            await self._write("POST", "/movement/relative/zoom", body, "the zoom", read_back="")
        elif command == "zoom_stop":
            await self._write("POST", "/movement/relative/zoom", {"action": "stop"}, "Zoom Stop", read_back="")
            self._watch_move()
        elif command == "zoom_direct":
            position = _as_int(params.get("position"), -1)
            if not 0 <= position <= 4912:
                raise ValueError("Zoom position must be 0 to 4912.")
            # An absolute move takes pan and tilt too: send the ones the
            # camera reports now so that only zoom moves.
            try:
                current = await self._read(POSITION) or {}
            except CameraRefused as exc:
                raise CommandRefusedError(f"The camera did not report its position, so nothing was sent ({exc}).") from exc
            pan, tilt = _field(current, "panPos"), _field(current, "tiltPos")
            if pan is None or tilt is None:
                raise CommandRefusedError("The camera did not report its pan and tilt, so nothing was sent.")
            body = {
                "panPos": str(_as_int(pan, 0)),
                "tiltPos": str(_as_int(tilt, 0)),
                "zoomPos": str(position),
                "panSpeed": str(self._config_speed("pan_speed", 12, 0, 24)),
                "tiltSpeed": str(self._config_speed("tilt_speed", 10, 0, 20)),
                "zoomSpeed": str(self._config_speed("zoom_speed", 4, 0, 7)),
            }
            await self._write("POST", POSITION, body, f"zoom position {position}", read_back="")
            self._watch_move()
        elif command == "focus_one_push":
            await self._write("POST", "/focus/mode", {"mode": "onepush"}, "One Push Focus", read_back="/focus/mode")
            self._watch_move("/focus/position")
        elif command in ("focus_near", "focus_far"):
            body = {
                "action": "near" if command == "focus_near" else "far",
                "speed": str(self._speed(params, "speed", "focus_speed", 3, 1, 7)),
                "type": "variable",
            }
            await self._write("POST", "/movement/relative/focus", body, "the focus move", read_back="")
        elif command == "focus_stop":
            await self._write("POST", "/movement/relative/focus", {"action": "stop"}, "Focus Stop", read_back="/focus/position")
        elif command == "focus_direct":
            position = _as_int(params.get("position"), -1)
            if not 236 <= position <= 4148:
                raise ValueError("Focus position must be 236 to 4148.")
            await self._write(
                "POST", "/focus/position", {"focusPos": str(position)}, f"focus position {position}", read_back="",
            )
            self._watch_move("/focus/position")
        elif command in ("preset_recall", "preset_save", "preset_clear"):
            preset = _as_int(params.get("preset"), -1)
            if not 0 <= preset <= 254:
                raise ValueError("Preset must be 0 to 254.")
            path, what = {
                "preset_recall": ("/presets/recall", "recalling preset"),
                "preset_save": ("/presets/save", "saving preset"),
                "preset_clear": ("/presets/reset", "deleting preset"),
            }[command]
            await self._write("POST", path, {"presetNumber": str(preset)}, f"{what} {preset}", read_back="")
            if command == "preset_recall":
                self._watch_move()
        elif command == "preset_speed":
            level = _as_int(params.get("level"), -1)
            if not 0 <= level <= 24:
                raise ValueError("Preset speed must be 0 to 24.")
            # The API has no read of the preset speed, so there is nothing to
            # read back.
            await self._write("POST", "/presets/speed", {"level": str(level)}, f"preset speed {level}", read_back="")
        elif command in SWITCHES:
            resource, field, word, key, wanted, what = SWITCHES[command]
            try:
                await self._write("POST", resource, {field: word}, what, expect=(key, wanted))
            except CommandRefusedError as exc:
                if key == "tally" and self.get_state("tally_enabled") is False:
                    raise CommandRefusedError(f"{exc} Tally Enabled is off on the camera; turn it on first.") from exc
                raise
        elif command == "select_presenter":
            selection = str(params.get("selection", "") or "").strip()
            if selection not in ("left", "right", "confirm"):
                raise ValueError("Selection must be left, right or confirm.")
            await self._write("POST", "/ai/presenter/select", {"selection": selection}, f"presenter selection {selection}", read_back="/ai/param")
        elif command == "record_start":
            body = {"status": "on"}
            filename = str(params.get("filename", "") or "").strip()
            if filename:
                body["filename"] = filename
            await self._write("POST", "/misc/record", body, "Start Recording", expect=("recording", True))
        elif command == "record_stop":
            await self._write("POST", "/misc/record", {"status": "off"}, "Stop Recording", expect=("recording", False))
        elif command == "wb_one_push_trigger":
            await self._write("POST", "/image/color/onepush", None, "One Push White Balance", read_back="/image/color/param")
        elif command in ADJUST:
            resource, targets = ADJUST[command]
            target = str(params.get("target", "") or "").strip()
            op = str(params.get("op", "") or "").strip()
            if target not in targets:
                raise ValueError(f"Unknown value to adjust: {target}")
            if op not in ("inc", "dec", "reset"):
                raise ValueError("Change must be inc, dec or reset.")
            body = {"target": target, "op": op}
            if op != "reset":
                body["value"] = str(max(1, _as_int(params.get("step"), 1)))
            await self._write("PATCH", resource, body, f"the {target} {op}", read_back=resource)
        elif command == "osd_menu":
            action = str(params.get("action", "") or "").strip()
            if action not in ("on", "off", "toggle", "enter", "back", "up", "down", "left", "right"):
                raise ValueError(f"Unknown menu action: {action}")
            await self._write("POST", "/osd/action", {"action": action}, f"the menu action {action}", read_back="/osd/action")
        elif command in ("rtmp_start", "rtmp_stop"):
            stream = str(params.get("stream", "") or "").strip()
            if stream not in ("1", "2"):
                raise ValueError("Stream must be 1 or 2.")
            on = command == "rtmp_start"
            await self._write(
                "POST", "/streaming/rtmp", {f"stream{stream}": {"enable": "on" if on else "off"}},
                f"{'Start' if on else 'Stop'} RTMP stream {stream}", expect=(f"rtmp{stream}_enabled", on),
            )
        elif command == "save_settings":
            await self._write("POST", "/osd/save", None, "Save Settings")
        elif command == "restore_defaults":
            await self._write("POST", "/osd/restoredefault", None, "Restore Default Settings")
            self._slow_due = 0.0
        elif command == "factory_reset":
            keep = str(params.get("keep_network", "") or "").strip().lower()
            if keep not in ("true", "false"):
                raise ValueError("Choose Keep Network (true) or Everything (false).")
            await self._write("POST", "/misc/reset/system", {"keepNetwork": keep}, "the factory reset", read_back="")
            self._slow_due = 0.0
        else:
            raise ValueError(f"Unknown command: {command}")
        return None

    # ── Device settings ──

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        spec = SETTINGS.get(key)
        if spec is None:
            raise ValueError(f"Unknown device setting: {key}")
        resource, path, kind = spec
        info = self.DRIVER_INFO["device_settings"][key]
        allowed = info.get("values")
        if allowed and str(value) not in allowed:
            raise ValueError(f"{info['label']} must be one of {', '.join(allowed)}.")
        wire = _write_value(value, kind)
        await self._write(
            "POST", resource, _nested(path, wire), f"{info['label']} = {wire}",
            expect=(info["state_key"], _expected(value, kind)),
        )
