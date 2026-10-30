"""
OpenAVC Marshall PTZ Camera Driver (Marshall CGI).

Controls Marshall's IP PTZ cameras through the HTTP CGI the camera serves on
port 80 (or HTTPS on 81): pan, tilt, zoom and focus, presets and the home
position, power and standby, tally, exposure, white balance and picture
settings, focus behaviour, the pan/tilt limits, SRT and RTMP streaming, and
the camera's identity. Every value is read back from the camera, including the
live pan, tilt, zoom and focus positions, and the camera's RTSP stream is
offered to a panel as a preview.

Protocol
--------
Marshall's "CGI Command V3" workbook (linked from every current Marshall IP
PTZ product page). Two request shapes, both GET with HTTP Digest sign-in
(realm "Secure", default account ``admin``):

* ``/command/inquiry.cgi?inqjs=<group>`` answers one ``var Name="value"`` line
  per parameter in that group (system, camera, imaging, ptzf, tally,
  presetposition, network, srt, rtmp, user, ...).
* ``/command/<group>.cgi?<Name>=<value>`` sets one parameter and answers
  ``{"STATUS":"OK"}`` (200), or ``{"STATUS":"NG"}`` (400) for a value or
  parameter it refuses. An unknown CGI answers 403.

Push vs poll
------------
Poll only. The workbook documents no event, subscription or callback
mechanism. (FreeD, which the camera can stream to a UDP destination, is
camera-tracking data for virtual production, not a control channel.) Power,
position and tally are read every ``poll_interval`` (default 5 s); the picture,
focus, motion, network and streaming groups every 30 s, and straight after any
change this driver makes to them. After Power On or Standby the power state is
read every 2 s until the camera reports the new state (it takes about 15 s to
enter standby and about 19 s to wake on a CV620-WI2).

Why Python
----------
One inquiry answers a whole group: ``inqjs=camera`` is about 50 values in one
body. A YAML HTTP driver hands a reply body to its response rules as one
message and stops at the first rule that matches, so it would read one value
per group per poll. The camera also acknowledges values it does not apply
(``TallyControl=maybe`` and ``SmartAF=on`` both answer OK on a CV620-WI2), so
every change is read back and a change the camera did not take is reported as
refused. Pan and tilt arrive as signed 32-bit hex in a six-field position
string.

Hardware notes (CV620-WI2, firmware VUG115_VUH109, 2026-10-30)
------------------------------------------------------------
Where the camera and the workbook disagree, the camera won:

* ``AbsolutePTZF`` reports pan and tilt as well as zoom and focus
  (``ffffc653,0000,0d5b,0396,0005,0005``); the workbook shows the pan and tilt
  fields as zeros. The last two fields are not documented and not read.
* ``HomePos=recall`` returns pan and tilt to centre on this mechanical PTZ;
  the workbook describes it for ePTZ cameras only.
* ``ImageMode`` reads ``standard`` and refuses the workbook's ``default`` and
  ``custom``, so image mode is reported but not offered as a setting.
* ``AbsoluteZoom`` takes 0000-4000 (optical) and refuses 5000 while the
  digital zoom limit is x1. Presets 0-255 store and recall; 256 is refused.
* In standby the CGI keeps answering and reports ``Power="standby"``, and
  pan/tilt moves are acknowledged and ignored, so this driver refuses motion
  commands while the camera reports standby. Reboot takes the camera off the
  network about 8 s after the command; it answers again after about 48 s.
* Shutter, iris and gain are acknowledged and ignored outside the exposure
  modes they belong to, red and blue gain outside manual white balance, and the
  four limit angles while the limits are off; any change to PTZ Assist outside
  manual focus is refused with HTTP 500, turning it off included. This driver
  refuses those up front with a sentence naming the mode. In shutter- or iris-priority exposure a write to
  ``ExposureExposureTime`` / ``ExposureIris`` lands in the read-only
  ``ExposureExposureTimePri`` / ``ExposureIrisPri``, which is what is reported.
* ``CameraName`` keeps 16 characters (the workbook says 12) and drops the rest
  without an error; the factory name ``CV620-BI2_WI2`` is 13.
* SRT starts only with a destination in caller mode (or in listener mode);
  RTMP only with a server and a stream key, the key being entered on the
  camera's web page.

Source:
    Marshall CGI Command V3 (Marshall Electronics):
    https://marshall-usa.com/cameras/software/Marshall_CGI_Command-V3.zip
"""

from __future__ import annotations

import asyncio
import re
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

# Read every poll / every SLOW_INTERVAL_S.
FAST_GROUPS = ("system", "ptzf", "tally")
SLOW_GROUPS = ("camera", "imaging", "presetposition", "network", "srt", "rtmp", "user")
SLOW_INTERVAL_S = 30.0

# After Power On / Standby, read the power state this often for this long.
# Measured on a CV620-WI2: 12-15 s into standby, 15-19 s back to on.
POWER_WATCH_STEP_S = 2.0
POWER_WATCH_S = 40.0

# A setting the camera applies a moment after acknowledging it is read back
# after this pause (the bench unit applied every one within it).
READ_BACK_DELAY_S = 0.4

_VAR_LINE = re.compile(r'^\s*var\s+([A-Za-z0-9_]+)\s*=\s*"(.*)"\s*;?\s*$')

# (inquiry group, camera parameter) -> (state key, how the value is read).
#   str: as reported   int: a decimal integer   onoff: "on" -> True
#   freeze: CallMode "freeze" -> True (Motionless Preset)
_READS: dict[tuple[str, str], tuple[str, str]] = {
    ("system", "Power"): ("power", "str"),
    ("system", "ModelName"): ("model", "str"),
    ("system", "FWVersion"): ("firmware_version", "str"),
    ("system", "Serial"): ("serial_number", "str"),
    ("system", "Uptime"): ("uptime", "str"),
    ("system", "IRReceive"): ("ir_receive", "onoff"),
    ("tally", "TallyControl"): ("tally", "onoff"),
    ("tally", "TallyLevel"): ("tally_level", "str"),
    ("tally", "TallyCMMDMode"): ("tally_mode", "str"),
    ("camera", "FocusMode"): ("focus_mode", "str"),
    ("camera", "AFSensitivity"): ("af_sensitivity", "str"),
    ("camera", "AFFrame"): ("af_frame", "str"),
    ("camera", "PresetAF"): ("preset_af", "onoff"),
    ("camera", "PTZAssist"): ("ptz_assist", "onoff"),
    ("camera", "PresetSpeed"): ("preset_speed", "int"),
    ("camera", "PTZSpeedComp"): ("ptz_speed_comp", "onoff"),
    ("camera", "PTZMotionSync"): ("ptz_motion_sync", "onoff"),
    ("camera", "PanFlip"): ("pan_flip", "onoff"),
    ("camera", "TiltFlip"): ("tilt_flip", "onoff"),
    ("camera", "PanTiltLimit"): ("pan_tilt_limit", "onoff"),
    ("camera", "PanLeftLimit"): ("pan_left_limit", "int"),
    ("camera", "PanRightLimit"): ("pan_right_limit", "int"),
    ("camera", "TiltUpLimit"): ("tilt_up_limit", "int"),
    ("camera", "TiltDownLimit"): ("tilt_down_limit", "int"),
    ("camera", "DZoomLimit"): ("d_zoom_limit", "str"),
    ("camera", "InitPosition"): ("init_position", "str"),
    ("camera", "Mirror"): ("mirror", "str"),
    ("camera", "PictureEffect"): ("picture_effect", "str"),
    ("camera", "AudioIn"): ("audio_in", "onoff"),
    ("camera", "MicLineSelect"): ("audio_input", "str"),
    ("camera", "AudioInVolume"): ("audio_volume", "int"),
    ("camera", "Resolution"): ("video_format", "str"),
    ("camera", "OutputSource"): ("output_source", "str"),
    ("camera", "ImageCodec1"): ("stream1_codec", "str"),
    ("camera", "ImageSize1"): ("stream1_resolution", "str"),
    ("camera", "FrameRate1"): ("stream1_frame_rate", "str"),
    ("camera", "BitRate1"): ("stream1_bitrate", "int"),
    ("camera", "ImageCodec2"): ("stream2_codec", "str"),
    ("camera", "ImageSize2"): ("stream2_resolution", "str"),
    ("camera", "FrameRate2"): ("stream2_frame_rate", "str"),
    ("camera", "BitRate2"): ("stream2_bitrate", "int"),
    ("imaging", "ExposureMode"): ("exposure_mode", "str"),
    ("imaging", "ExposureCompensation"): ("exposure_compensation", "int"),
    ("imaging", "ExposureExposureTime"): ("shutter", "int"),
    ("imaging", "ExposureIris"): ("iris", "int"),
    ("imaging", "ExposureGain"): ("gain", "int"),
    ("imaging", "ExposureMaxGain"): ("max_gain", "int"),
    ("imaging", "VisibilityEnhancerLevel"): ("wdr", "int"),
    ("imaging", "WhiteBalanceMode"): ("wb_mode", "str"),
    ("imaging", "WhiteBalanceCrGain"): ("wb_red_gain", "int"),
    ("imaging", "WhiteBalanceCbGain"): ("wb_blue_gain", "int"),
    ("imaging", "DetailLevel"): ("sharpness", "int"),
    ("imaging", "NoiseReduction2DLevel"): ("nr_2d", "int"),
    ("imaging", "NoiseReduction3DLevel"): ("nr_3d", "int"),
    ("imaging", "DigitalBrightLevel"): ("brightness", "int"),
    ("imaging", "ColorHue"): ("hue", "int"),
    ("imaging", "ColorSaturation"): ("saturation", "int"),
    ("imaging", "GammaLevel"): ("gamma", "int"),
    ("imaging", "ImageMode"): ("image_mode", "str"),
    ("presetposition", "CallMode"): ("motionless_preset", "freeze"),
    ("network", "CameraName"): ("camera_name", "str"),
    ("network", "MacAddress"): ("mac_address", "str"),
    ("srt", "SRTEnable"): ("srt_enabled", "onoff"),
    ("srt", "SRTConnectMode"): ("srt_mode", "str"),
    ("srt", "SRTUrl"): ("srt_url", "str"),
    ("srt", "SRTPort"): ("srt_port", "int"),
    ("srt", "SRTLatency"): ("srt_latency", "int"),
    ("srt", "SRTEncryption"): ("srt_encryption", "str"),
    ("rtmp", "RtmpEnable"): ("rtmp_enabled", "onoff"),
    ("rtmp", "RtmpServerUrl1"): ("rtmp_url", "str"),
    ("rtmp", "RtmpStatus"): ("rtmp_status", "str"),
    ("user", "AuthenRTSP"): ("rtsp_auth", "onoff"),
}

# Device setting -> (CGI, parameter, how the value is written).
_SETTINGS: dict[str, tuple[str, str, str]] = {
    "exposure_mode": ("imaging", "ExposureMode", "str"),
    "exposure_compensation": ("imaging", "ExposureCompensation", "int"),
    "shutter": ("imaging", "ExposureExposureTime", "int"),
    "iris": ("imaging", "ExposureIris", "int"),
    "gain": ("imaging", "ExposureGain", "int"),
    "max_gain": ("imaging", "ExposureMaxGain", "int"),
    "wdr": ("imaging", "VisibilityEnhancerLevel", "int"),
    "wb_mode": ("imaging", "WhiteBalanceMode", "str"),
    "wb_red_gain": ("imaging", "WhiteBalanceCrGain", "int"),
    "wb_blue_gain": ("imaging", "WhiteBalanceCbGain", "int"),
    "sharpness": ("imaging", "DetailLevel", "int"),
    "nr_2d": ("imaging", "NoiseReduction2DLevel", "int"),
    "nr_3d": ("imaging", "NoiseReduction3DLevel", "int"),
    "brightness": ("imaging", "DigitalBrightLevel", "int"),
    "hue": ("imaging", "ColorHue", "int"),
    "saturation": ("imaging", "ColorSaturation", "int"),
    "gamma": ("imaging", "GammaLevel", "int"),
    "focus_mode": ("camera", "FocusMode", "str"),
    "af_sensitivity": ("camera", "AFSensitivity", "str"),
    "af_frame": ("camera", "AFFrame", "str"),
    "preset_af": ("camera", "PresetAF", "onoff"),
    "ptz_assist": ("camera", "PTZAssist", "onoff"),
    "preset_speed": ("camera", "PresetSpeed", "int"),
    "ptz_speed_comp": ("camera", "PTZSpeedComp", "onoff"),
    "ptz_motion_sync": ("camera", "PTZMotionSync", "onoff"),
    "pan_flip": ("camera", "PanFlip", "onoff"),
    "tilt_flip": ("camera", "TiltFlip", "onoff"),
    "pan_tilt_limit": ("camera", "PanTiltLimit", "onoff"),
    "pan_left_limit": ("camera", "PanLeftLimit", "int"),
    "pan_right_limit": ("camera", "PanRightLimit", "int"),
    "tilt_up_limit": ("camera", "TiltUpLimit", "int"),
    "tilt_down_limit": ("camera", "TiltDownLimit", "int"),
    "d_zoom_limit": ("camera", "DZoomLimit", "str"),
    "init_position": ("camera", "InitPosition", "str"),
    "mirror": ("camera", "Mirror", "str"),
    "picture_effect": ("camera", "PictureEffect", "str"),
    "audio_in": ("camera", "AudioIn", "onoff"),
    "audio_input": ("camera", "MicLineSelect", "str"),
    "audio_volume": ("camera", "AudioInVolume", "int"),
    "motionless_preset": ("presetposition", "CallMode", "freeze"),
    "tally_level": ("tally", "TallyLevel", "str"),
    "tally_mode": ("tally", "TallyCMMDMode", "str"),
    "ir_receive": ("system", "IRReceive", "onoff"),
    "camera_name": ("network", "CameraName", "str"),
    "srt_mode": ("srt", "SRTConnectMode", "str"),
    "srt_url": ("srt", "SRTUrl", "str"),
    "srt_port": ("srt", "SRTPort", "int"),
    "srt_latency": ("srt", "SRTLatency", "int"),
    "rtmp_url": ("rtmp", "RtmpServerUrl1", "str"),
}

# Settings the camera acknowledges and ignores outside a mode (measured on a
# CV620-WI2): setting -> (state key holding the mode, the modes it applies in,
# the sentence a refusal gives). Checked against a fresh read of that group.
_REQUIRES: dict[str, tuple[str, tuple[Any, ...], str]] = {
    "shutter": ("exposure_mode", ("shutter", "manual"), "Shutter applies only in shutter-priority or manual exposure."),
    "iris": ("exposure_mode", ("iris", "manual"), "Iris applies only in iris-priority or manual exposure."),
    "gain": ("exposure_mode", ("manual",), "Gain applies only in manual exposure."),
    "wb_red_gain": ("wb_mode", ("manual",), "Red gain applies only in manual white balance."),
    "wb_blue_gain": ("wb_mode", ("manual",), "Blue gain applies only in manual white balance."),
    # In auto focus any change is refused with HTTP 500, off included, and an
    # assist left on from manual focus stays on (bench, CV620-WI2).
    "ptz_assist": ("focus_mode", ("manual",), "PTZ Assist can be changed only in manual focus."),
    "pan_left_limit": ("pan_tilt_limit", (True,), "The limit angles apply only while Pan/Tilt Limits On is on."),
    "pan_right_limit": ("pan_tilt_limit", (True,), "The limit angles apply only while Pan/Tilt Limits On is on."),
    "tilt_up_limit": ("pan_tilt_limit", (True,), "The limit angles apply only while Pan/Tilt Limits On is on."),
    "tilt_down_limit": ("pan_tilt_limit", (True,), "The limit angles apply only while Pan/Tilt Limits On is on."),
}

# In shutter- or iris-priority exposure the camera keeps the value a write to
# ExposureExposureTime / ExposureIris lands in under a second name, readable
# only (a write to the ...Pri name answers NG). Exposure mode -> (parameter
# written, parameter that reports it).
_PRIORITY_FIELDS = {
    "shutter": ("ExposureExposureTime", "ExposureExposureTimePri"),
    "iris": ("ExposureIris", "ExposureIrisPri"),
}

# The eight pan/tilt jog commands -> the camera's Move direction word.
_PT_DIRECTIONS = {
    "pt_up": "up",
    "pt_down": "down",
    "pt_left": "left",
    "pt_right": "right",
    "pt_up_left": "up-left",
    "pt_up_right": "up-right",
    "pt_down_left": "down-left",
    "pt_down_right": "down-right",
}

# Commands the camera acknowledges and ignores while in standby (measured).
# The stops are left out: a button's release sends one after its press, and
# a stop has nothing to refuse.
_MOTION_COMMANDS = set(_PT_DIRECTIONS) | {
    "pt_home", "zoom_in", "zoom_out", "zoom_direct",
    "focus_near", "focus_far", "focus_one_push",
    "preset_recall", "preset_save",
}


class CameraRefused(Exception):
    """The camera answered, and said no (400 NG, 403, or another status)."""

    def __init__(self, message: str, *, http_status: int = 0) -> None:
        super().__init__(message)
        self.http_status = http_status


def parse_inquiry(text: str) -> dict[str, str]:
    """``var Name="value"`` lines -> {Name: value}. Other lines are ignored."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        m = _VAR_LINE.match(line)
        if m:
            values[m.group(1)] = m.group(2)
    return values


def _position(field: str) -> int | None:
    """One AbsolutePTZF field. The camera prints a 32-bit position as
    unpadded hex, so a negative pan or tilt is eight digits starting
    ``ffff`` (``ffffc653`` = -14765) and a positive one is as short as four
    (``1b30``)."""
    try:
        value = int(field, 16)
    except ValueError:
        return None
    if len(field) == 8 and value >= 0x80000000:
        value -= 1 << 32
    return value


def parse_ptzf(text: str) -> dict[str, int]:
    """``AbsolutePTZF`` -> pan, tilt, zoom and focus positions."""
    fields = text.split(",")
    out: dict[str, int] = {}
    for key, field in zip(("pan_position", "tilt_position", "zoom_position", "focus_position"), fields):
        value = _position(field.strip())
        if value is not None:
            out[key] = value
    return out


def _read_value(raw: str, kind: str) -> Any:
    if kind == "onoff":
        return raw.strip().lower() == "on"
    if kind == "freeze":
        return raw.strip().lower() == "freeze"
    if kind == "int":
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return raw


def _write_value(value: Any, kind: str) -> str:
    if kind == "onoff":
        return "on" if _as_bool(value) else "off"
    if kind == "freeze":
        return "freeze" if _as_bool(value) else "normal"
    if kind == "int":
        return str(int(value))
    return str(value)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "on", "yes")


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class MarshallPTZDriver(BaseDriver):
    """Marshall IP PTZ cameras over the Marshall CGI (HTTP Digest)."""

    DRIVER_INFO = {
        "id": "marshall_ptz",
        "name": "Marshall PTZ Camera",
        "manufacturer": "Marshall",
        "category": "camera",
        "version": "1.0.1",
        # confirm on preset_clear / factory_reset needs 0.36.0.
        "min_platform_version": "0.36.0",
        "author": "OpenAVC",
        "description": (
            "Controls Marshall IP PTZ cameras over the camera's HTTP CGI: pan, "
            "tilt, zoom and focus with live position read-back, presets and "
            "home, power and standby, tally, exposure, white balance and picture "
            "settings, focus behaviour, pan/tilt limits, SRT and RTMP streaming, "
            "and the camera's RTSP stream as a panel preview."
        ),
        "source_url": "https://marshall-usa.com/cameras/software/Marshall_CGI_Command-V3.zip",
        "tags": ["ptz", "camera", "marshall", "cgi", "rtsp", "srt", "tally"],
        "verified": True,
        "simulated": True,
        "protocols": ["marshall_cgi"],
        "ports": [80],
        "transport": "http",
        "discovery": {
            # The HTTPS port answers every request with the Digest challenge
            # (realm "Secure") under a certificate whose subject names Lumens,
            # the maker of the camera platform. Seen on two CV620-WI2s,
            # firmware VUG115_VUH109.
            #
            # Not cross_vendor: these cameras also advertise Dante, whose
            # service ports are chosen at random, and on one of the two a
            # Dante service took 4455, the OBS Studio port. A cross-vendor
            # probe is handed to whichever driver the narrowest hint names,
            # so that scan identified the camera as OBS Studio. As a
            # vendor-specific probe it identifies this driver whatever ports
            # the camera happens to have open.
            "tcp_probe": {
                "port": 81,
                "tls": True,
                "send_ascii": "GET / HTTP/1.1\r\nHost: camera\r\nConnection: close\r\n\r\n",
                "expect": "realm=\"Secure\"",
                "cert_subject": "Lumens",
                "timeout_ms": 3000,
            },
            # Marshall Electronics' MA-L block (IEEE registry).
            "oui": ["00:23:a8"],
            "manufacturer_alias": ["marshall", "marshall electronics"],
        },
        "compatible_models": [
            {
                "manufacturer": "Marshall",
                "models": ["CV620-BI2", "CV620-WI2"],
                "confidence": "full",
                "notes": (
                    "Verified on a CV620-WI2 (firmware VUG115_VUH109): every command "
                    "and setting read back from the camera. Image mode is reported "
                    "but fixed at standard on this model; Smart AF is not available."
                ),
            },
            {
                "manufacturer": "Marshall",
                "models": ["CV625-TB", "CV625-TW", "CV625-TBN", "CV625-TWN"],
                "confidence": "untested",
                "notes": (
                    "Marshall publishes the same CGI command document for these "
                    "cameras. Not yet tested on this hardware."
                ),
            },
            {
                "manufacturer": "Marshall",
                "models": ["CV620-BI", "CV620-WI", "CV620-TBI", "CV620-TWI"],
                "confidence": "untested",
                "notes": (
                    "Earlier CV620 models for which Marshall publishes the same CGI "
                    "command document. Not yet tested on this hardware."
                ),
            },
            {
                "manufacturer": "Marshall",
                "models": [
                    "CV355-30X-IP", "CV355-30X-NDI", "CV420-30X", "CV420-30X-IP",
                    "CV420-30X-NDI", "CV420e", "CV630-IP", "CV630-NDI", "CV730",
                    "CV730-NDI", "CV730-HN",
                ],
                "confidence": "untested",
                "notes": (
                    "Named in Marshall's CGI command document, which marks the "
                    "commands each model lacks (the CV355-30X and CV420-30X take "
                    "no pan/tilt jog, and the CV420e no focus or iris control). A "
                    "command the camera does not support is reported as refused. "
                    "Not yet tested on this hardware."
                ),
            },
        ],
        "help": {
            "overview": (
                "Controls a Marshall IP PTZ camera over its web API: pan, tilt, "
                "zoom and focus, presets and the home position, power and standby, "
                "and tally. Exposure, white balance, picture, focus, motion and "
                "streaming settings appear on the device page and are read back "
                "from the camera, as are the live pan, tilt, zoom and focus "
                "positions. The camera's RTSP stream is offered to the Video Panel "
                "as a preview. The Pan/Tilt Speed, Zoom Speed and Focus Speed fields "
                "set the speed of a move that does not give its own."
            ),
            "setup": (
                "1. Connect the camera to the network and find its address on the "
                "camera's on-screen menu (Menu > Ethernet). With DHCP off, Marshall's "
                "manual gives 192.168.100.100 as the factory address.\n"
                "2. Enter the camera's IP address here, with the web user name and "
                "password (the camera's factory account is admin, password 9999; "
                "change it on the camera's web page under System > User).\n"
                "3. Leave the port at 80. To use HTTPS instead, turn on Use HTTPS "
                "and set the port to 81.\n"
                "4. To show the camera on a panel, leave RTSP authentication off on "
                "the camera's Stream page."
            ),
        },
        "default_config": {
            "host": "",
            "port": 80,
            "ssl": False,
            "verify_ssl": False,
            "username": "admin",
            "password": "",
            "pt_speed": 12,
            "zoom_speed": 4,
            "focus_speed": 3,
            "poll_interval": 5,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "default": 80, "min": 1, "max": 65535, "label": "Port"},
            "ssl": {
                "type": "boolean",
                "default": False,
                "label": "Use HTTPS",
                "description": "The camera serves HTTPS on port 81 with a self-signed certificate.",
            },
            "verify_ssl": {
                "type": "boolean",
                "default": False,
                "label": "Verify SSL Certificate",
                "description": "Leave off unless the camera has a certificate this computer trusts.",
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
            "pt_speed": {
                "type": "integer",
                "default": 12,
                "min": 0,
                "max": 23,
                "label": "Pan/Tilt Speed",
                "description": "Speed for pan and tilt moves that do not give their own (0 slowest, 23 fastest).",
            },
            "zoom_speed": {
                "type": "integer",
                "default": 4,
                "min": 0,
                "max": 7,
                "label": "Zoom Speed",
                "description": "Speed for zoom moves that do not give their own (0 slowest, 7 fastest).",
            },
            "focus_speed": {
                "type": "integer",
                "default": 3,
                "min": 0,
                "max": 7,
                "label": "Focus Speed",
                "description": "Speed for manual focus moves that do not give their own (0 slowest, 7 fastest).",
            },
            "poll_interval": {
                "type": "integer",
                "default": 5,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "How often power, position and tally are read. Picture, focus "
                    "and streaming settings are read every 30 seconds. 0 stops polling."
                ),
            },
        },
        "state_variables": {
            "power": {
                "type": "enum",
                "values": ["on", "standby"],
                "label": "Power",
                "help": "on, or standby. The camera takes about 20 seconds to wake and about 15 to enter standby.",
                "control": True,
                "cloud_priority": "high",
            },
            "model": {"type": "string", "label": "Model"},
            "firmware_version": {"type": "string", "label": "Firmware Version"},
            "serial_number": {"type": "string", "label": "Serial Number"},
            "uptime": {"type": "string", "label": "Uptime", "cloud_priority": "low"},
            "mac_address": {"type": "string", "label": "MAC Address"},
            "camera_name": {"type": "string", "label": "Camera Name"},
            "pan_position": {
                "type": "integer",
                "label": "Pan Position",
                "help": "The camera's pan position counter; 0 is centre, negative is left.",
                "cloud_priority": "low",
            },
            "tilt_position": {
                "type": "integer",
                "label": "Tilt Position",
                "help": "The camera's tilt position counter; 0 is level, negative is down.",
                "cloud_priority": "low",
            },
            "zoom_position": {
                "type": "integer",
                "label": "Zoom Position",
                "help": "0 is fully wide, 16384 is full optical zoom; higher values are digital zoom.",
                "min": 0,
                "max": 31424,
                "cloud_priority": "low",
            },
            "focus_position": {
                "type": "integer",
                "label": "Focus Position",
                "cloud_priority": "low",
            },
            "tally": {
                "type": "boolean",
                "label": "Tally Lamp",
                "control": True,
                "cloud_priority": "high",
            },
            "tally_level": {
                "type": "enum",
                "values": ["off", "low", "high"],
                "label": "Tally Brightness",
            },
            "tally_mode": {
                "type": "enum",
                "values": ["link", "normal"],
                "label": "Tally Mode",
                "help": "link: the lamp and its brightness switch together. normal: set separately.",
            },
            "focus_mode": {
                "type": "enum",
                "values": ["auto", "manual"],
                "label": "Focus Mode",
                "control": True,
            },
            "af_sensitivity": {"type": "enum", "values": ["low", "middle", "high"], "label": "AF Sensitivity"},
            "af_frame": {"type": "enum", "values": ["center", "full", "auto"], "label": "AF Frame"},
            "preset_af": {"type": "boolean", "label": "Auto Focus After Preset"},
            "ptz_assist": {"type": "boolean", "label": "PTZ Assist"},
            "preset_speed": {"type": "integer", "label": "Preset Speed", "min": 0, "max": 7, "step": 1},
            "ptz_speed_comp": {"type": "boolean", "label": "PTZ Speed Compensation"},
            "ptz_motion_sync": {"type": "boolean", "label": "PTZ Motion Sync"},
            "pan_flip": {"type": "boolean", "label": "Pan Flip"},
            "tilt_flip": {"type": "boolean", "label": "Tilt Flip"},
            "pan_tilt_limit": {"type": "boolean", "label": "Pan/Tilt Limits On"},
            "pan_left_limit": {"type": "integer", "label": "Pan Left Limit", "min": -170, "max": 0, "unit": "°"},
            "pan_right_limit": {"type": "integer", "label": "Pan Right Limit", "min": 0, "max": 170, "unit": "°"},
            "tilt_up_limit": {"type": "integer", "label": "Tilt Up Limit", "min": 0, "max": 90, "unit": "°"},
            "tilt_down_limit": {"type": "integer", "label": "Tilt Down Limit", "min": -30, "max": 0, "unit": "°"},
            "d_zoom_limit": {
                "type": "enum",
                "values": ["x1", "x2", "x3", "x4", "x5", "x6", "x7", "x8", "x9", "x10", "x11", "x12"],
                "label": "Digital Zoom Limit",
            },
            "init_position": {"type": "enum", "values": ["lastmem", "1stpreset"], "label": "Position At Power On"},
            "motionless_preset": {"type": "boolean", "label": "Motionless Preset"},
            "mirror": {"type": "enum", "values": ["off", "mirror", "flip", "mirror+flip"], "label": "Mirror / Flip"},
            "picture_effect": {"type": "enum", "values": ["off", "neg", "bw"], "label": "Picture Effect"},
            "image_mode": {"type": "string", "label": "Image Mode"},
            "exposure_mode": {
                "type": "enum",
                "values": ["auto", "shutter", "iris", "manual"],
                "label": "Exposure Mode",
                "control": True,
            },
            "exposure_compensation": {"type": "integer", "label": "Exposure Compensation", "min": 0, "max": 10, "step": 1},
            "shutter": {"type": "integer", "label": "Shutter", "min": 0, "max": 21, "step": 1},
            "iris": {"type": "integer", "label": "Iris", "min": 0, "max": 15, "step": 1},
            "gain": {"type": "integer", "label": "Gain", "min": 1, "max": 16, "step": 1},
            "max_gain": {"type": "integer", "label": "Gain Limit", "min": 4, "max": 16, "step": 1},
            "wdr": {"type": "integer", "label": "WDR", "min": 0, "max": 3, "step": 1},
            "wb_mode": {
                "type": "enum",
                "values": ["auto", "atw", "indoor", "outdoor", "onepushwb", "sodiumlamp", "manual"],
                "label": "White Balance Mode",
                "control": True,
            },
            "wb_red_gain": {"type": "integer", "label": "WB Red Gain", "min": 0, "max": 128, "step": 1},
            "wb_blue_gain": {"type": "integer", "label": "WB Blue Gain", "min": 0, "max": 128, "step": 1},
            "sharpness": {"type": "integer", "label": "Sharpness", "min": 0, "max": 14, "step": 1},
            "nr_2d": {"type": "integer", "label": "2D Noise Reduction", "min": 0, "max": 3, "step": 1},
            "nr_3d": {"type": "integer", "label": "3D Noise Reduction", "min": 0, "max": 3, "step": 1},
            "brightness": {"type": "integer", "label": "Brightness", "min": 0, "max": 15, "step": 1},
            "hue": {"type": "integer", "label": "Hue", "min": 0, "max": 15, "step": 1},
            "saturation": {"type": "integer", "label": "Saturation", "min": 0, "max": 15, "step": 1},
            "gamma": {"type": "integer", "label": "Gamma", "min": 0, "max": 3, "step": 1},
            "audio_in": {"type": "boolean", "label": "Audio In"},
            "audio_input": {"type": "enum", "values": ["mic", "line"], "label": "Audio Input"},
            "audio_volume": {"type": "integer", "label": "Audio Volume", "min": 0, "max": 10, "step": 1},
            "ir_receive": {"type": "boolean", "label": "IR Remote"},
            "video_format": {"type": "string", "label": "Video Format"},
            "output_source": {"type": "string", "label": "Output"},
            "stream1_codec": {"type": "string", "label": "Stream 1 Codec"},
            "stream1_resolution": {"type": "string", "label": "Stream 1 Resolution"},
            "stream1_frame_rate": {"type": "string", "label": "Stream 1 Frame Rate"},
            "stream1_bitrate": {"type": "integer", "label": "Stream 1 Bitrate", "unit": "kbps"},
            "stream2_codec": {"type": "string", "label": "Stream 2 Codec"},
            "stream2_resolution": {"type": "string", "label": "Stream 2 Resolution"},
            "stream2_frame_rate": {"type": "string", "label": "Stream 2 Frame Rate"},
            "stream2_bitrate": {"type": "integer", "label": "Stream 2 Bitrate", "unit": "kbps"},
            "rtsp_auth": {"type": "boolean", "label": "RTSP Authentication"},
            "srt_enabled": {"type": "boolean", "label": "SRT Streaming", "control": True},
            "srt_mode": {"type": "enum", "values": ["caller", "listener"], "label": "SRT Mode"},
            "srt_url": {"type": "string", "label": "SRT Destination"},
            "srt_port": {"type": "integer", "label": "SRT Port", "min": 1024, "max": 65534},
            "srt_latency": {"type": "integer", "label": "SRT Latency", "min": 20, "max": 8000, "unit": "ms"},
            "srt_encryption": {"type": "string", "label": "SRT Encryption"},
            "rtmp_enabled": {"type": "boolean", "label": "RTMP Streaming", "control": True},
            "rtmp_url": {"type": "string", "label": "RTMP Server"},
            "rtmp_status": {"type": "string", "label": "RTMP Status"},
            "preview_url": {"type": "string", "label": "Preview URL"},
            "preview_format": {"type": "string", "label": "Preview Format"},
            "preview_status": {"type": "string", "label": "Preview Status"},
            "preview_status_detail": {"type": "string", "label": "Preview Status Detail"},
        },
        "commands": {
            "power_on": {
                "label": "Power On",
                "help": "Wakes the camera from standby. It takes about 20 seconds.",
                "params": {},
                "sets": {"power": "on"},
            },
            "power_off": {
                "label": "Standby",
                "help": "Puts the camera into standby. Its web page stays reachable.",
                "params": {},
                "sets": {"power": "standby"},
            },
            "reboot": {
                "label": "Reboot",
                "help": "Restarts the camera. It is off the network for about 40 seconds.",
                "params": {},
                "restarts_device_for": 60,
            },
            "pt_up": {
                "label": "Tilt Up",
                "help": "Tilts the camera up until Pan/Tilt Stop.",
                "params": {"speed": {"type": "integer", "min": 0, "max": 23, "label": "Speed"}},
            },
            "pt_down": {
                "label": "Tilt Down",
                "help": "Tilts the camera down until Pan/Tilt Stop.",
                "params": {"speed": {"type": "integer", "min": 0, "max": 23, "label": "Speed"}},
            },
            "pt_left": {
                "label": "Pan Left",
                "help": "Pans the camera left until Pan/Tilt Stop.",
                "params": {"speed": {"type": "integer", "min": 0, "max": 23, "label": "Speed"}},
            },
            "pt_right": {
                "label": "Pan Right",
                "help": "Pans the camera right until Pan/Tilt Stop.",
                "params": {"speed": {"type": "integer", "min": 0, "max": 23, "label": "Speed"}},
            },
            "pt_up_left": {
                "label": "Pan Up-Left",
                "help": "Moves the camera up and left until Pan/Tilt Stop.",
                "params": {"speed": {"type": "integer", "min": 0, "max": 23, "label": "Speed"}},
            },
            "pt_up_right": {
                "label": "Pan Up-Right",
                "help": "Moves the camera up and right until Pan/Tilt Stop.",
                "params": {"speed": {"type": "integer", "min": 0, "max": 23, "label": "Speed"}},
            },
            "pt_down_left": {
                "label": "Pan Down-Left",
                "help": "Moves the camera down and left until Pan/Tilt Stop.",
                "params": {"speed": {"type": "integer", "min": 0, "max": 23, "label": "Speed"}},
            },
            "pt_down_right": {
                "label": "Pan Down-Right",
                "help": "Moves the camera down and right until Pan/Tilt Stop.",
                "params": {"speed": {"type": "integer", "min": 0, "max": 23, "label": "Speed"}},
            },
            "pt_stop": {"label": "Pan/Tilt Stop", "help": "Stops pan and tilt movement.", "params": {}},
            "pt_home": {
                "label": "Home",
                "help": "Returns the camera to its centre pan and tilt position.",
                "params": {},
                "sets": {"pan_position": 0, "tilt_position": 0},
            },
            "zoom_in": {
                "label": "Zoom In",
                "help": "Zooms in (tele) until Zoom Stop.",
                "params": {"speed": {"type": "integer", "min": 0, "max": 7, "label": "Speed"}},
            },
            "zoom_out": {
                "label": "Zoom Out",
                "help": "Zooms out (wide) until Zoom Stop.",
                "params": {"speed": {"type": "integer", "min": 0, "max": 7, "label": "Speed"}},
            },
            "zoom_stop": {"label": "Zoom Stop", "help": "Stops zooming.", "params": {}},
            "zoom_direct": {
                "label": "Zoom To Position",
                "help": (
                    "Zooms to a position: 0 is fully wide, 16384 is full optical zoom, "
                    "and up to 31424 is digital zoom when the Digital Zoom Limit allows it."
                ),
                "params": {
                    "position": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 31424,
                        "label": "Zoom Position",
                    },
                },
                "sets": {"zoom_position": "{position}"},
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
            "focus_near": {
                "label": "Focus Near",
                "help": "Moves focus nearer until Focus Stop (manual focus only).",
                "params": {"speed": {"type": "integer", "min": 0, "max": 7, "label": "Speed"}},
            },
            "focus_far": {
                "label": "Focus Far",
                "help": "Moves focus farther until Focus Stop (manual focus only).",
                "params": {"speed": {"type": "integer", "min": 0, "max": 7, "label": "Speed"}},
            },
            "focus_stop": {"label": "Focus Stop", "help": "Stops a manual focus move.", "params": {}},
            "focus_one_push": {
                "label": "One Push Auto Focus",
                "help": "Focuses once on the current picture, then holds (manual focus only).",
                "params": {},
            },
            "preset_recall": {
                "label": "Recall Preset",
                "help": "Moves the camera to a stored preset position.",
                "params": {
                    "preset": {"type": "integer", "required": True, "min": 0, "max": 255, "label": "Preset"},
                },
            },
            "preset_save": {
                "label": "Save Preset",
                "help": "Stores the camera's current position as a preset, replacing what was stored there.",
                "params": {
                    "preset": {"type": "integer", "required": True, "min": 0, "max": 255, "label": "Preset"},
                },
            },
            "preset_clear": {
                "label": "Delete Preset",
                "help": "Deletes a stored preset position.",
                "confirm": "Deletes the camera position stored in this preset.",
                "params": {
                    "preset": {"type": "integer", "required": True, "min": 0, "max": 255, "label": "Preset"},
                },
            },
            "tally_on": {
                "label": "Tally On",
                "help": "Lights the camera's tally lamp.",
                "params": {},
                "sets": {"tally": True},
            },
            "tally_off": {
                "label": "Tally Off",
                "help": "Turns the camera's tally lamp off.",
                "params": {},
                "sets": {"tally": False},
            },
            "wb_one_push_trigger": {
                "label": "One Push White Balance",
                "help": "Sets white balance once from what the camera sees now (One Push WB mode only).",
                "params": {},
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
            "rtmp_start": {
                "label": "Start RTMP",
                "help": "Starts publishing to the camera's RTMP server.",
                "params": {},
                "sets": {"rtmp_enabled": True},
            },
            "rtmp_stop": {
                "label": "Stop RTMP",
                "help": "Stops publishing to the RTMP server.",
                "params": {},
                "sets": {"rtmp_enabled": False},
            },
            "factory_reset": {
                "label": "Factory Reset",
                "help": (
                    "Returns the camera's settings to factory values. Keep Network "
                    "keeps its IP address and network settings; Everything resets those too."
                ),
                "confirm": (
                    "Returns every camera setting to its factory value. Everything "
                    "also resets the IP address, so the camera may leave this network."
                ),
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": ["soft", "hard"],
                        "label": "What To Reset",
                        "help": "soft: everything except network settings. hard: everything.",
                    },
                },
            },
        },
        "device_settings": {
            "exposure_mode": {
                "type": "enum", "label": "Exposure Mode", "state_key": "exposure_mode",
                "values": ["auto", "shutter", "iris", "manual"], "default": "auto", "setup": False,
                "help": "auto: full auto. shutter / iris: that one fixed. manual: shutter, iris and gain fixed.",
            },
            "exposure_compensation": {
                "type": "integer", "label": "Exposure Compensation", "state_key": "exposure_compensation",
                "min": 0, "max": 10, "default": 5, "setup": False,
                "help": "Exposure bias: 5 is none, 10 is +5 dB, 0 is -5 dB.",
            },
            "shutter": {
                "type": "integer", "label": "Shutter", "state_key": "shutter",
                "min": 0, "max": 21, "default": 6, "setup": False,
                "help": "Shutter speed, in shutter-priority and manual exposure only: 0 is 1/10000, 15 is 1/60 (1/50), 21 is 1/1.",
            },
            "iris": {
                "type": "integer", "label": "Iris", "state_key": "iris",
                "min": 0, "max": 15, "default": 11, "setup": False,
                "help": "Iris, in iris-priority and manual exposure only: 15 is closed, 14 is F1.6, 0 is F18.",
            },
            "gain": {
                "type": "integer", "label": "Gain", "state_key": "gain",
                "min": 1, "max": 16, "default": 1, "setup": False,
                "help": "Gain, in manual exposure only: 1 is 0 dB, each step adds 3 dB, 16 is +45 dB.",
            },
            "max_gain": {
                "type": "integer", "label": "Gain Limit", "state_key": "max_gain",
                "min": 4, "max": 16, "default": 16, "setup": False,
                "help": "Highest gain auto exposure may use: 4 is +9 dB, 16 is +45 dB.",
            },
            "wdr": {
                "type": "integer", "label": "WDR", "state_key": "wdr",
                "min": 0, "max": 3, "default": 0, "setup": False,
                "help": "Wide dynamic range level, 0 (off) to 3.",
            },
            "wb_mode": {
                "type": "enum", "label": "White Balance Mode", "state_key": "wb_mode",
                "values": ["auto", "atw", "indoor", "outdoor", "onepushwb", "sodiumlamp", "manual"],
                "default": "auto", "setup": False,
                "help": "manual uses the red and blue gains below; onepushwb uses One Push White Balance.",
            },
            "wb_red_gain": {
                "type": "integer", "label": "WB Red Gain", "state_key": "wb_red_gain",
                "min": 0, "max": 128, "default": 64, "setup": False,
                "help": "Red gain, in manual white balance only.",
            },
            "wb_blue_gain": {
                "type": "integer", "label": "WB Blue Gain", "state_key": "wb_blue_gain",
                "min": 0, "max": 128, "default": 64, "setup": False,
                "help": "Blue gain, in manual white balance only.",
            },
            "sharpness": {
                "type": "integer", "label": "Sharpness", "state_key": "sharpness",
                "min": 0, "max": 14, "default": 7, "setup": False,
            },
            "nr_2d": {
                "type": "integer", "label": "2D Noise Reduction", "state_key": "nr_2d",
                "min": 0, "max": 3, "default": 1, "setup": False,
            },
            "nr_3d": {
                "type": "integer", "label": "3D Noise Reduction", "state_key": "nr_3d",
                "min": 0, "max": 3, "default": 2, "setup": False,
            },
            "brightness": {
                "type": "integer", "label": "Brightness", "state_key": "brightness",
                "min": 0, "max": 15, "default": 7, "setup": False,
            },
            "hue": {
                "type": "integer", "label": "Hue", "state_key": "hue",
                "min": 0, "max": 15, "default": 7, "setup": False,
            },
            "saturation": {
                "type": "integer", "label": "Saturation", "state_key": "saturation",
                "min": 0, "max": 15, "default": 7, "setup": False,
            },
            "gamma": {
                "type": "integer", "label": "Gamma", "state_key": "gamma",
                "min": 0, "max": 3, "default": 3, "setup": False,
            },
            "focus_mode": {
                "type": "enum", "label": "Focus Mode", "state_key": "focus_mode",
                "values": ["auto", "manual"], "default": "auto", "setup": False,
            },
            "af_sensitivity": {
                "type": "enum", "label": "AF Sensitivity", "state_key": "af_sensitivity",
                "values": ["low", "middle", "high"], "default": "middle", "setup": False,
                "help": "How readily auto focus refocuses; high refocuses most easily.",
            },
            "af_frame": {
                "type": "enum", "label": "AF Frame", "state_key": "af_frame",
                "values": ["center", "full", "auto"], "default": "auto", "setup": False,
                "help": "The part of the picture auto focus uses.",
            },
            "preset_af": {
                "type": "boolean", "label": "Auto Focus After Preset", "state_key": "preset_af",
                "default": True, "setup": False,
                "help": "Focus once after recalling a preset.",
            },
            "ptz_assist": {
                "type": "boolean", "label": "PTZ Assist", "state_key": "ptz_assist",
                "default": False, "setup": False,
                "help": "Focus once after each pan, tilt or zoom move. Manual focus only.",
            },
            "preset_speed": {
                "type": "integer", "label": "Preset Speed", "state_key": "preset_speed",
                "min": 0, "max": 7, "default": 5, "setup": False,
                "help": "Pan/tilt speed when recalling a preset: 0 is 5°/s, 5 is 160°/s, 7 is 300°/s.",
            },
            "ptz_speed_comp": {
                "type": "boolean", "label": "PTZ Speed Compensation", "state_key": "ptz_speed_comp",
                "default": False, "setup": False,
                "help": "Slow pan and tilt as the camera zooms in.",
            },
            "ptz_motion_sync": {
                "type": "boolean", "label": "PTZ Motion Sync", "state_key": "ptz_motion_sync",
                "default": False, "setup": False,
                "help": "Pan, tilt and zoom arrive together when recalling a preset.",
            },
            "pan_flip": {
                "type": "boolean", "label": "Pan Flip", "state_key": "pan_flip",
                "default": False, "setup": False, "help": "Reverse the pan direction.",
            },
            "tilt_flip": {
                "type": "boolean", "label": "Tilt Flip", "state_key": "tilt_flip",
                "default": False, "setup": False, "help": "Reverse the tilt direction.",
            },
            "pan_tilt_limit": {
                "type": "boolean", "label": "Pan/Tilt Limits On", "state_key": "pan_tilt_limit",
                "default": False, "setup": False,
                "help": "Keep the camera inside the four limit angles below.",
            },
            "pan_left_limit": {
                "type": "integer", "label": "Pan Left Limit", "state_key": "pan_left_limit",
                "min": -170, "max": 0, "default": -170, "setup": False, "help": "Degrees, -170 to 0, while Pan/Tilt Limits On is on.",
            },
            "pan_right_limit": {
                "type": "integer", "label": "Pan Right Limit", "state_key": "pan_right_limit",
                "min": 0, "max": 170, "default": 170, "setup": False, "help": "Degrees, 0 to 170, while Pan/Tilt Limits On is on.",
            },
            "tilt_up_limit": {
                "type": "integer", "label": "Tilt Up Limit", "state_key": "tilt_up_limit",
                "min": 0, "max": 90, "default": 90, "setup": False, "help": "Degrees, 0 to 90, while Pan/Tilt Limits On is on.",
            },
            "tilt_down_limit": {
                "type": "integer", "label": "Tilt Down Limit", "state_key": "tilt_down_limit",
                "min": -30, "max": 0, "default": -30, "setup": False, "help": "Degrees, -30 to 0, while Pan/Tilt Limits On is on.",
            },
            "d_zoom_limit": {
                "type": "enum", "label": "Digital Zoom Limit", "state_key": "d_zoom_limit",
                "values": ["x1", "x2", "x3", "x4", "x5", "x6", "x7", "x8", "x9", "x10", "x11", "x12"],
                "default": "x1", "setup": False, "help": "x1 turns digital zoom off.",
            },
            "init_position": {
                "type": "enum", "label": "Position At Power On", "state_key": "init_position",
                "values": ["lastmem", "1stpreset"], "default": "lastmem", "setup": False,
                "help": "lastmem: where it was. 1stpreset: preset 0.",
            },
            "motionless_preset": {
                "type": "boolean", "label": "Motionless Preset", "state_key": "motionless_preset",
                "default": False, "setup": False,
                "help": "Freeze the picture while the camera moves to a preset.",
            },
            "mirror": {
                "type": "enum", "label": "Mirror / Flip", "state_key": "mirror",
                "values": ["off", "mirror", "flip", "mirror+flip"], "default": "off", "setup": False,
                "help": "Use flip for a ceiling-mounted camera.",
            },
            "picture_effect": {
                "type": "enum", "label": "Picture Effect", "state_key": "picture_effect",
                "values": ["off", "neg", "bw"], "default": "off", "setup": False,
            },
            "audio_in": {
                "type": "boolean", "label": "Audio In", "state_key": "audio_in",
                "default": False, "setup": False, "help": "Embed the audio input in the camera's outputs.",
            },
            "audio_input": {
                "type": "enum", "label": "Audio Input", "state_key": "audio_input",
                "values": ["mic", "line"], "default": "line", "setup": False,
            },
            "audio_volume": {
                "type": "integer", "label": "Audio Volume", "state_key": "audio_volume",
                "min": 0, "max": 10, "default": 6, "setup": False,
            },
            "tally_level": {
                "type": "enum", "label": "Tally Brightness", "state_key": "tally_level",
                "values": ["off", "low", "high"], "default": "high", "setup": False,
            },
            "tally_mode": {
                "type": "enum", "label": "Tally Mode", "state_key": "tally_mode",
                "values": ["link", "normal"], "default": "link", "setup": False,
                "help": "link: the lamp and its brightness switch together. normal: set separately.",
            },
            "ir_receive": {
                "type": "boolean", "label": "IR Remote", "state_key": "ir_receive",
                "default": True, "setup": False, "help": "Accept the IR remote control.",
            },
            "camera_name": {
                "type": "string", "label": "Camera Name", "state_key": "camera_name",
                "default": "", "setup": False, "help": "1 to 16 characters.",
            },
            "srt_mode": {
                "type": "enum", "label": "SRT Mode", "state_key": "srt_mode",
                "values": ["caller", "listener"], "default": "caller", "setup": False,
                "help": "caller: the camera sends to SRT Destination. listener: a decoder connects to the camera.",
            },
            "srt_url": {
                "type": "string", "label": "SRT Destination", "state_key": "srt_url",
                "default": "", "setup": False, "help": "IP address or host name to send to (caller mode).",
            },
            "srt_port": {
                "type": "integer", "label": "SRT Port", "state_key": "srt_port",
                "min": 1024, "max": 65534, "default": 1025, "setup": False,
            },
            "srt_latency": {
                "type": "integer", "label": "SRT Latency (ms)", "state_key": "srt_latency",
                "min": 20, "max": 8000, "default": 120, "setup": False,
            },
            "rtmp_url": {
                "type": "string", "label": "RTMP Server", "state_key": "rtmp_url",
                "default": "", "setup": False,
                "help": "The server's RTMP URL, e.g. rtmp://a.rtmp.youtube.com/live2. Enter the stream key on the camera's web page.",
            },
        },
        "quick_actions": ["power_on", "power_off", "pt_home", "tally_on", "tally_off"],
    }

    HEALTH_FAULT_MESSAGE = "The camera stopped answering its web API."

    _client: httpx.AsyncClient | None = None

    def __init__(self, device_id: str, config: dict[str, Any], state: Any, events: Any) -> None:
        super().__init__(device_id, config, state, events)
        self._client = None
        self._auth: httpx.DigestAuth | None = None
        self._lock = asyncio.Lock()
        self._slow_due = 0.0
        self._unsupported: set[str] = set()
        self._values: dict[str, dict[str, str]] = {}
        self._power_task: asyncio.Task | None = None

    # ── Config accessors ──

    @property
    def _host(self) -> str:
        return str(self.config.get("host", "") or "").strip()

    @property
    def _port(self) -> int:
        return _as_int(self.config.get("port"), 80) or 80

    def _base_url(self) -> str:
        scheme = "https" if _as_bool(self.config.get("ssl", False)) else "http"
        return f"{scheme}://{self._host}:{self._port}"

    def _speed(self, params: dict[str, Any], config_key: str, default: int, top: int) -> int:
        speed = params.get("speed")
        if speed is None or speed == "":
            speed = self.config.get(config_key, default)
        return max(0, min(top, _as_int(speed, default)))

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
            # Never send a sign-in that cannot succeed: the camera has no
            # account without a password.
            raise self._auth_fault()
        if not await self._verify_reachable(self._host, self._port):
            raise ConnectionError(f"{self._host}:{self._port} is not responding")
        self._auth = httpx.DigestAuth(username, password)
        self._client = httpx.AsyncClient(
            base_url=self._base_url(),
            verify=_as_bool(self.config.get("verify_ssl", False)),
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=False,
        )

    async def _post_connect(self) -> None:
        self._unsupported = set()
        self._values = {}
        self._slow_due = 0.0
        try:
            await self._read_group("system")
        except CameraRefused as exc:
            raise ConnectionFaultError(
                f"The device at {self._host} answered, but not with the Marshall camera API ({exc}).",
                code="invalid_config",
            ) from exc
        log.info(
            f"[{self.device_id}] Connected to Marshall {self.get_state('model') or 'camera'} "
            f"{self.get_state('serial_number') or ''} at {self._host}:{self._port} "
            f"(firmware {self.get_state('firmware_version') or 'unknown'})"
        )

    async def _initial_sync(self) -> None:
        await self.poll()

    def _link_alive(self) -> bool:
        return self._client is not None

    async def _close_session(self) -> None:
        self._cancel_power_watch()
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _liveness_probe(self) -> None:
        try:
            await self._get("/command/inquiry.cgi?inqjs=system")
        except CameraRefused:
            return  # it answered: the link is up

    # ── HTTP plumbing ──

    async def _get(self, path: str) -> str:
        """One GET under the lock. Returns the body of a 200. A 401 raises the
        typed auth_failed; any other refusal raises CameraRefused; a dead
        link raises ConnectionError."""
        client = self._client
        if client is None:
            raise ConnectionError("Not connected")
        async with self._lock:
            try:
                resp = await client.get(path, auth=self._auth)
            except httpx.TransportError as exc:
                raise ConnectionError(f"{self._host} is not responding: {exc}") from exc
        if resp.status_code == 401:
            raise self._auth_fault()
        if resp.status_code != 200:
            raise CameraRefused(f"HTTP {resp.status_code}", http_status=resp.status_code)
        return resp.text

    async def _set(self, cgi: str, param: str, value: str, what: str) -> None:
        """``/command/<cgi>.cgi?<param>=<value>``. Raises CommandRefusedError
        when the camera answers NG (or anything but OK)."""
        path = f"/command/{cgi}.cgi?{param}={quote(value, safe=',')}"
        try:
            body = await self._get(path)
        except CameraRefused as exc:
            raise CommandRefusedError(f"The camera refused {what} ({exc}).") from exc
        if '"NG"' in body:
            raise CommandRefusedError(f"The camera refused {what}.")

    async def _read_group(self, group: str) -> dict[str, str] | None:
        """Read one inquiry group into state. None when this camera has no
        such group (it is not asked again on this connection)."""
        if group in self._unsupported:
            return None
        try:
            body = await self._get(f"/command/inquiry.cgi?inqjs={group}")
        except CameraRefused as exc:
            if group == "system":
                raise
            if exc.http_status in (400, 403, 404):
                self._unsupported.add(group)
                return None
            raise ConnectionError(f"The camera answered the {group} read with {exc}") from exc
        values = parse_inquiry(body)
        self._values[group] = values
        self._apply(group, values)
        return values

    def _apply(self, group: str, values: dict[str, str]) -> None:
        updates: dict[str, Any] = {}
        for name, raw in values.items():
            target = _READS.get((group, name))
            if target is None:
                continue
            key, kind = target
            value = _read_value(raw, kind)
            if value is not None:
                updates[key] = value
        if group == "ptzf" and "AbsolutePTZF" in values:
            updates.update(parse_ptzf(values["AbsolutePTZF"]))
        if group == "imaging":
            # Shutter and iris report the value in effect: the priority field
            # in that priority mode, the manual one otherwise.
            fields = _PRIORITY_FIELDS.get(values.get("ExposureMode", ""))
            if fields and fields[1] in values:
                key = "shutter" if fields[0] == "ExposureExposureTime" else "iris"
                value = _read_value(values[fields[1]], "int")
                if value is not None:
                    updates[key] = value
        if updates:
            self.set_states(updates)
        if group in ("camera", "network", "user"):
            self._update_preview()

    def _update_preview(self) -> None:
        """The camera's RTSP stream for the Video Panel: stream 2 (the lighter
        one) when it is on, else stream 1."""
        camera = self._values.get("camera", {})
        network = self._values.get("network", {})
        if not camera or not network:
            return
        if _as_bool(self._values.get("user", {}).get("AuthenRTSP", "off")):
            self.set_states({
                "preview_url": "",
                "preview_format": "",
                "preview_status": "unavailable",
                "preview_status_detail": (
                    "RTSP authentication is on at the camera. Turn it off on the "
                    "camera's Stream page to show the camera on a panel."
                ),
            })
            return
        stream = "2" if camera.get("ImageCodec2", "off") not in ("", "off") else "1"
        if camera.get(f"ImageCodec{stream}", "off") in ("", "off"):
            self.set_states({
                "preview_url": "",
                "preview_format": "",
                "preview_status": "unavailable",
                "preview_status_detail": "Both of the camera's streams are turned off.",
            })
            return
        port = network.get(f"RTSPPort{stream}") or ("8556" if stream == "2" else "8557")
        self.set_states({
            "preview_url": f"rtsp://{self._host}:{port}/h264",
            "preview_format": "rtsp",
            "preview_status": "",
            "preview_status_detail": "",
        })

    # ── Polling ──

    async def poll(self) -> None:
        if self._client is None:
            return
        groups = list(FAST_GROUPS)
        now = time.monotonic()
        if now >= self._slow_due:
            groups.extend(SLOW_GROUPS)
            self._slow_due = now + SLOW_INTERVAL_S
        for group in groups:
            try:
                await self._read_group(group)
            except CameraRefused as exc:
                # Only the status read raises this (an optional group the camera
                # refuses is skipped). A typed ConnectionError counts as a missed
                # poll; anything else is logged as unexpected and never counted.
                raise ConnectionError(f"The camera answered its status read with {exc}.") from exc

    def _watch_power(self, target: str) -> None:
        """Read the power state every few seconds until the camera reports
        ``target`` (it changes 12-19 s after the command)."""
        self._cancel_power_watch()

        async def _run() -> None:
            deadline = time.monotonic() + POWER_WATCH_S
            while time.monotonic() < deadline:
                await asyncio.sleep(POWER_WATCH_STEP_S)
                try:
                    await self._read_group("system")
                except Exception as exc:  # the poll reports a dead link
                    log.debug(f"[{self.device_id}] Power read-back stopped: {exc}")
                    return
                if self.get_state("power") == target:
                    return

        self._power_task = asyncio.create_task(_run())

    def _cancel_power_watch(self) -> None:
        task, self._power_task = self._power_task, None
        if task is not None and not task.done():
            task.cancel()

    # ── Commands ──

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        if command in _MOTION_COMMANDS and self.get_state("power") == "standby":
            raise CommandRefusedError("The camera is in standby. Turn it on first.")

        if command in _PT_DIRECTIONS:
            speed = self._speed(params, "pt_speed", 12, 23)
            await self._set("ptzf", "Move", f"{_PT_DIRECTIONS[command]},{speed}", "the pan/tilt move")
        elif command == "pt_stop":
            await self._set("ptzf", "Move", "stop,motor", "Pan/Tilt Stop")
            await self._read_group("ptzf")
        elif command == "pt_home":
            await self._set("presetposition", "HomePos", "recall", "Home")
        elif command == "zoom_in":
            await self._set("ptzf", "Move", f"tele,{self._speed(params, 'zoom_speed', 4, 7)}", "Zoom In")
        elif command == "zoom_out":
            await self._set("ptzf", "Move", f"wide,{self._speed(params, 'zoom_speed', 4, 7)}", "Zoom Out")
        elif command == "zoom_stop":
            await self._set("ptzf", "Move", "stop,zoom", "Zoom Stop")
            await self._read_group("ptzf")
        elif command == "zoom_direct":
            position = max(0, min(0x7AC0, _as_int(params.get("position"), 0)))
            # The camera answers once the zoom has arrived (about 1.2 s end to
            # end on a CV620-WI2), so the position read straight after is final.
            await self._set("ptzf", "AbsoluteZoom", f"{position:04X}", f"zoom position {position}")
            await self._read_group("ptzf")
        elif command in ("focus_auto", "focus_manual"):
            await self._write_setting("focus_mode", "auto" if command == "focus_auto" else "manual")
        elif command == "focus_near":
            await self._set("ptzf", "Move", f"near,{self._speed(params, 'focus_speed', 3, 7)}", "Focus Near")
        elif command == "focus_far":
            await self._set("ptzf", "Move", f"far,{self._speed(params, 'focus_speed', 3, 7)}", "Focus Far")
        elif command == "focus_stop":
            await self._set("ptzf", "Move", "stop,focus", "Focus Stop")
        elif command == "focus_one_push":
            await self._set("ptzf", "Move", "onepushaf,0", "One Push Auto Focus")
        elif command in ("preset_recall", "preset_save", "preset_clear"):
            preset = _as_int(params.get("preset"), -1)
            if not 0 <= preset <= 255:
                raise ValueError("Preset must be 0 to 255.")
            param, what = {
                "preset_recall": ("PresetCall", "recalling preset"),
                "preset_save": ("PresetSet", "saving preset"),
                "preset_clear": ("PresetClear", "deleting preset"),
            }[command]
            await self._set("presetposition", param, str(preset), f"{what} {preset}")
        elif command in ("tally_on", "tally_off"):
            await self._write_setting_raw("tally", "TallyControl", "tally", command == "tally_on", "onoff", "the tally lamp")
        elif command == "wb_one_push_trigger":
            await self._set("imaging", "WhiteBalanceOnePushTrg", "on", "One Push White Balance")
        elif command in ("srt_start", "srt_stop"):
            try:
                await self._write_setting_raw("srt", "SRTEnable", "srt_enabled", command == "srt_start", "onoff", "SRT streaming")
            except CommandRefusedError as exc:
                await self._read_group("srt")
                srt = self._values.get("srt", {})
                if command == "srt_start" and srt.get("SRTConnectMode") == "caller" and not srt.get("SRTUrl"):
                    # Refused 400 on a CV620-WI2 in caller mode with no destination.
                    raise CommandRefusedError("The camera refused to start SRT: no SRT Destination is set.") from exc
                raise
        elif command in ("rtmp_start", "rtmp_stop"):
            try:
                await self._write_setting_raw("rtmp", "RtmpEnable", "rtmp_enabled", command == "rtmp_start", "onoff", "RTMP streaming")
            except CommandRefusedError as exc:
                await self._read_group("rtmp")
                rtmp = self._values.get("rtmp", {})
                if command == "rtmp_start" and not rtmp.get("RtmpServerUrl1"):
                    raise CommandRefusedError("The camera refused to start RTMP: no RTMP server is set.") from exc
                if command == "rtmp_start" and rtmp.get("RtmpStreamKey1Used") == "0":
                    # Refused 400 on a CV620-WI2 with a server set and no key.
                    raise CommandRefusedError(
                        "The camera refused to start RTMP: no stream key is set. "
                        "Enter it on the camera's web page."
                    ) from exc
                raise
        elif command in ("power_on", "power_off"):
            target = "on" if command == "power_on" else "standby"
            await self._set("main", "System", target, "Power On" if target == "on" else "Standby")
            self._watch_power(target)
        elif command == "reboot":
            await self._set("main", "System", "reboot", "Reboot")
        elif command == "factory_reset":
            mode = str(params.get("mode", "") or "")
            if mode not in ("soft", "hard"):
                raise ValueError("Choose soft (keep network settings) or hard (everything).")
            await self._set("main", "FactoryDefault", mode, "the factory reset")
        else:
            raise ValueError(f"Unknown command: {command}")

    # ── Device settings ──

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        if key not in _SETTINGS:
            raise ValueError(f"Unknown device setting: {key}")
        # The workbook says 12; the camera keeps 16 and drops the rest without
        # saying so, and its factory name (CV620-BI2_WI2) is 13 (CV620-WI2).
        if key == "camera_name" and not 1 <= len(str(value)) <= 16:
            raise ValueError("Camera name must be 1 to 16 characters.")
        required = _REQUIRES.get(key)
        if required is not None:
            mode_key, modes, sentence = required
            await self._read_group(_SETTINGS[mode_key][0])
            if self.get_state(mode_key) not in modes:
                raise CommandRefusedError(sentence)
        await self._write_setting(key, value)

    async def _write_setting(self, key: str, value: Any) -> None:
        cgi, param, kind = _SETTINGS[key]
        label = self.DRIVER_INFO["device_settings"][key]["label"]
        await self._write_setting_raw(cgi, param, key, value, kind, label)

    async def _write_setting_raw(
        self, cgi: str, param: str, state_key: str, value: Any, kind: str, what: str,
    ) -> None:
        """Write one parameter, then read its group back. The camera answers
        OK to values it does not apply (bench: TallyControl=maybe, SmartAF=on
        on a CV620-WI2), so a write is only reported done once the camera
        reports the new value."""
        wire = _write_value(value, kind)
        await self._set(cgi, param, wire, f"{what} = {wire}")
        await asyncio.sleep(READ_BACK_DELAY_S)
        await self._read_group(cgi)
        values = self._values.get(cgi, {})
        fields = _PRIORITY_FIELDS.get(values.get("ExposureMode", "")) if cgi == "imaging" else None
        reported = values.get(fields[1] if fields and fields[0] == param else param)
        if reported is not None and reported != wire:
            raise CommandRefusedError(
                f"The camera accepted {what} = {wire} but still reports {reported}."
            )
