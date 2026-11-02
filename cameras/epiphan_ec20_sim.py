"""
Epiphan EC20 PTZ Camera (EC20 REST API) — Simulator.

Simulates an Epiphan EC20's REST API as Epiphan's OpenAPI document (v1)
describes it: ``GET`` of each resource under ``/api/v1`` answers its JSON
object with the document's example values, ``POST`` takes a JSON object of the
fields to change (values as strings, or numbers where the document types them
as integers) and answers 200 with an empty body, and ``PATCH`` of an image
resource steps one field (``{"target", "op": "inc" | "dec" | "reset",
"value"}``). A field the resource does not have, a read-only field, or a value
outside the document's list answers 400. A POST may carry any subset of a
resource's fields, as the document's exposure and color examples do.

Motion is computed from when a move started, so positions advance between
reads with no background task: a jog runs until its stop, an absolute or
relative move travels at its speed and stops at the target, Home and a preset
recall travel to the stored position. The travel limits are the document's
(pan about -170 to 170, tilt -30 to 90, zoom 0 to 4912, focus 236 to 4148);
the rates per speed step are this simulator's own, since the document gives
none. Standby and wake take ``standby_delay`` and ``wake_delay`` seconds
(default 2 and 4; not documented). A factory reset makes the camera answer
nothing for ``reboot_downtime`` seconds (default 5; whether a real EC20
restarts is not documented). ``/movement/current-position`` answers the
same numbers as the absolute position; the document's example (500, 500,
1200) does not say what scale it uses.

What the document does not say and this simulator chooses: an RTMP stream
turned on reports ``active`` "success" and no message, turned off "Stream
disabled"; a recording started with no file name is named
``ec20_ep0000000_<date>_<time>``; One Push focus leaves the focus mode as it
was and moves focus once; saturation and hue (absent from the document's GET
example) read "7".

Sign-in: HTTP Digest, MD5, qop auth, realm "EC20" (the simulator's own: the
documents do not give the camera's). User ``admin`` (or the configured
``username``). With a ``password`` configured only that password is accepted;
with none, any password but ``invalid`` is.

Error modes: ``communication_timeout`` (answers nothing), ``password_changed``
(every request is refused 401) and ``exposure_read_refused`` (the exposure
read answers 500).

Driver: epiphan_ec20
Transport: http
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from openavc.simulator.http_simulator import HTTPSimulator

REALM = "EC20"
API = "/api/v1"

ONOFF = ("on", "off")
INT = "int"
ANY = "any"

# resource -> (field path, state key, how it is answered, what a POST may
# write: a tuple of words, INT, ANY, or None for read-only).
#   s: a JSON string   n: a JSON number
RESOURCES: dict[str, tuple[tuple[str, str, str, Any], ...]] = {
    "/misc/standby": (("action", "power", "s", ONOFF),),
    "/misc/standbylight": (("status", "standby_light", "s", ("flicker", "steadyOn", "normal")),),
    "/misc/digitalzoom": (("digitalzoom", "digital_zoom", "s", ("off", "2x", "4x", "8x", "16x", "superZoom")),),
    "/misc/dvimode": (("dvimode", "dvi_mode", "s", ("hdmi", "dvi")),),
    "/misc/record": (
        ("status", "record_status", "s", ONOFF),
        ("mode", "record_mode", "s", None),
        ("filename", "record_filename", "s", ANY),
        ("fileID", "record_file_id", "s", None),
        ("format", "record_format", "s", None),
    ),
    "/misc/freeze": (("freeze", "freeze", "s", ONOFF),),
    "/ir/enable": (("enable", "ir_enable", "s", ONOFF + ("toggle",)),),
    "/osd/action": (("action", "osd", "s", ONOFF + ("toggle", "enter", "back", "up", "down", "left", "right")),),
    "/osd/language": (("language", "osd_language", "s", None),),
    "/movement/speeds": (
        ("normal.panSpeed", "pan_speed", "s", INT),
        ("normal.tiltSpeed", "tilt_speed", "s", INT),
        ("normal.zoomSpeed", "zoom_speed", "s", INT),
    ),
    "/focus/mode": (("mode", "focus_mode", "s", ("auto", "manual", "onepush", "toggle")),),
    "/focus/sensitivity": (("sensitivity", "af_sensitivity", "s", ("high", "normal", "low")),),
    "/focus/zone": (("zone", "af_zone", "s", ("top", "center", "bottom", "front")),),
    "/focus/position": (("focusPos", "focus", "s", INT),),
    "/image/color/param": (
        ("whiteBalanceMode", "wb_mode", "s", ("auto", "indoor", "outdoor", "onepush", "manual", "var")),
        ("redGain", "red_gain", "s", ANY),
        ("blueGain", "blue_gain", "s", ANY),
        ("redGainTuning", "red_gain_tuning", "s", ANY),
        ("blueGainTuning", "blue_gain_tuning", "s", ANY),
        ("colorTemp", "color_temp", "s", ANY),
        ("saturation", "saturation", "s", ANY),
        ("hue", "hue", "s", ANY),
    ),
    "/image/exposure/param": (
        ("exposureMode", "exposure_mode", "s", ("fullAuto", "manual", "shutterPriority", "irisPriority", "bright")),
        ("shutter", "shutter", "s", ANY),
        ("iris", "iris", "s", ANY),
        ("gain", "gain", "s", ANY),
        ("brightness", "brightness", "s", ANY),
        ("gainLimit", "gain_limit", "s", ANY),
        ("compensationMode", "compensation_mode", "s", ONOFF),
        ("exposureCompensation", "exposure_compensation", "s", ANY),
        ("backlight", "backlight", "s", ONOFF),
        ("meter", "meter", "s", ("average", "center", "smart", "top")),
        ("dynamicRangeControl", "drc", "s", ANY),
        ("antiFlicker", "anti_flicker", "s", ("off", "50hz", "60hz")),
    ),
    "/image/picture/param": (
        ("luminance", "luminance", "n", INT),
        ("contrast", "contrast", "n", INT),
        ("sharpness", "sharpness", "n", INT),
        ("noise3d", "noise3d", "n", INT),
        ("gamma", "gamma", "s", ANY),
    ),
    "/image/orientation/param": (
        ("horizontalFlip", "horizontal_flip", "s", ONOFF),
        ("verticalFlip", "vertical_flip", "s", ONOFF),
    ),
    "/video/param": (
        ("hardwareOutput.hdmi", "out_hdmi", "s", None),
        ("hardwareOutput.3g-sdi", "out_3g_sdi", "s", None),
        ("hardwareOutput.12g-sdi", "out_12g_sdi", "s", None),
        ("hardwareOutput.usb", "out_usb", "s", None),
        ("hardwareOutput.ip", "out_ip", "s", None),
        ("hardwareOutputResolution", "output_resolution", "s", ANY),
        ("stream1.encode", "s1_encode", "s", ("h264", "h265", "mjpeg")),
        ("stream1.framerate", "s1_framerate", "n", INT),
        ("stream1.bitrateControl", "s1_bitrate_control", "s", ("cbr", "vbr")),
        ("stream1.slice", "s1_slice", "s", ANY),
        ("stream1.width", "s1_width", "n", INT),
        ("stream1.height", "s1_height", "n", INT),
        ("stream1.gop", "s1_gop", "n", INT),
        ("stream1.bitrate", "s1_bitrate", "n", INT),
        ("stream1.qFactor", "s1_qfactor", "n", INT),
        ("stream1.maxQ", "s1_maxq", "n", INT),
        ("stream1.minQ", "s1_minq", "n", INT),
        ("stream2.encode", "s2_encode", "s", ("h264", "h265", "mjpeg")),
        ("stream2.framerate", "s2_framerate", "n", INT),
        ("stream2.bitrateControl", "s2_bitrate_control", "s", ("cbr", "vbr")),
        ("stream2.width", "s2_width", "n", INT),
        ("stream2.height", "s2_height", "n", INT),
        ("stream2.gop", "s2_gop", "n", INT),
        ("stream2.bitrate", "s2_bitrate", "n", INT),
    ),
    "/ai/param": (
        ("enable", "ai_enable", "s", ONOFF),
        ("mode", "ai_mode", "s", ("tracking", "zone")),
        ("debugMode", "ai_debug_mode", "s", ("debug", "off", "default")),
        ("hintCue", "ai_hint_cue", "s", ONOFF),
        ("targetLostTimeOut", "ai_lost_timeout", "n", INT),
        ("presenterTracking.figureSize", "ai_figure_size", "s", ANY),
        ("presenterTracking.customFigureSize", "ai_custom_figure_size", "s", ANY),
        ("presenterTracking.figurePosition", "ai_figure_position", "s", ("left", "middle", "right")),
        ("presenterTracking.startPosition", "ai_start_position", "s", ANY),
        ("zoneTracking.startArea", "ai_start_area", "s", ANY),
    ),
    "/tally/param": (
        ("enable", "tally_enable", "s", ONOFF),
        ("action", "tally_action", "s", ("on", "off", "blink")),
        ("status", "tally_status", "s", ONOFF),
        ("tallyOption", "tally_option", "s", ("red", "green")),
    ),
    "/streaming/rtmp": (
        ("stream1.enable", "rtmp1_enable", "s", ONOFF),
        ("stream1.mrl", "rtmp1_mrl", "s", ANY),
        ("stream1.key", "rtmp1_key", "s", ANY),
        ("stream1.video_en", "rtmp1_video", "s", ONOFF),
        ("stream1.audio_en", "rtmp1_audio", "s", ONOFF),
        ("stream1.active", "rtmp1_active", "s", None),
        ("stream1.err_msg", "rtmp1_err_msg", "s", None),
        ("stream2.enable", "rtmp2_enable", "s", ONOFF),
        ("stream2.mrl", "rtmp2_mrl", "s", ANY),
        ("stream2.key", "rtmp2_key", "s", ANY),
        ("stream2.video_en", "rtmp2_video", "s", ONOFF),
        ("stream2.audio_en", "rtmp2_audio", "s", ONOFF),
        ("stream2.active", "rtmp2_active", "s", None),
        ("stream2.err_msg", "rtmp2_err_msg", "s", None),
    ),
    "/streaming/srt": (
        ("enable", "srt_enable", "s", ONOFF),
        ("mode", "srt_mode", "s", ("listener", "caller")),
        ("server", "srt_server", "s", ANY),
        ("port", "srt_port", "n", INT),
        ("encryption", "srt_encryption", "s", ("none", "aes128", "aes192", "aes256")),
        ("password", "srt_password", "s", ANY),
        ("bandwidth", "srt_bandwidth", "n", INT),
        ("latency", "srt_latency", "n", INT),
        ("streamId", "srt_stream_id", "s", ANY),
    ),
    "/streaming/multicast": (
        ("enable", "multicast_enable", "s", ONOFF),
        ("ip", "multicast_ip", "s", ANY),
        ("port", "multicast_port", "n", INT),
    ),
    "/streaming/onvif": (
        ("enable", "onvif_enable", "s", ONOFF),
        ("auth", "onvif_auth", "s", ONOFF),
    ),
}

# Image values PATCH may step: resource -> field -> state key.
STEPPABLE = {
    "/image/exposure/param": {
        "shutter": "shutter", "iris": "iris", "gain": "gain", "brightness": "brightness",
        "gainLimit": "gain_limit", "exposureCompensation": "exposure_compensation",
        "dynamicRangeControl": "drc",
    },
    "/image/color/param": {
        "redGain": "red_gain", "blueGain": "blue_gain", "redGainTuning": "red_gain_tuning",
        "blueGainTuning": "blue_gain_tuning", "colorTemp": "color_temp", "saturation": "saturation",
        "hue": "hue",
    },
    "/image/picture/param": {
        "luminance": "luminance", "contrast": "contrast", "sharpness": "sharpness", "noise3d": "noise3d",
    },
}

# Travel limits and this simulator's rate per speed step (units per second).
LIMITS = {"pan": (-170, 170), "tilt": (-30, 90), "zoom": (0, 4912), "focus": (236, 4148)}
RATE = {"pan": 4.0, "tilt": 3.0, "zoom": 700.0, "focus": 400.0}
STATE_KEY = {"pan": "pan", "tilt": "tilt", "zoom": "zoom", "focus": "focus"}
JOG = {
    "up": (0, 1), "down": (0, -1), "left": (-1, 0), "right": (1, 0),
    "upleft": (-1, 1), "upright": (1, 1), "downleft": (-1, -1), "downright": (1, -1),
}

_INT_TEXT = re.compile(r"^-?\d+$")


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INT_TEXT.match(value.strip()):
        return int(value.strip())
    return None


def _leaves(body: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Every (dotted path, value) in a request body."""
    if not isinstance(body, dict):
        return []
    out: list[tuple[str, Any]] = []
    for key, value in body.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out.extend(_leaves(value, path + "."))
        else:
            out.append((path, value))
    return out


class EpiphanEC20Simulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "epiphan_ec20",
        "name": "Epiphan EC20 PTZ Camera Simulator",
        "category": "camera",
        "transport": "http",
        "default_port": 80,
        "initial_state": {
            # The document's example values where it gives them.
            "power": "on", "standby_light": "steadyOn", "digital_zoom": "off", "dvi_mode": "hdmi",
            "record_status": "off", "record_mode": "realTime",
            "record_filename": "ec20_ep0000000_20260805_180004", "record_file_id": "", "record_format": "",
            "freeze": "off", "ir_enable": "on", "osd": "off", "osd_language": "English",
            "pan_speed": "24", "tilt_speed": "20", "zoom_speed": "5",
            "pan": -36, "tilt": -25, "zoom": 0, "focus": 3200,
            "focus_mode": "auto", "af_sensitivity": "normal", "af_zone": "center",
            "wb_mode": "auto", "red_gain": "128", "blue_gain": "128", "red_gain_tuning": "0",
            "blue_gain_tuning": "0", "color_temp": "30", "saturation": "7", "hue": "7",
            "exposure_mode": "fullAuto", "shutter": "8", "iris": "4", "gain": "4", "brightness": "10",
            "gain_limit": "10", "compensation_mode": "off", "exposure_compensation": "7",
            "backlight": "off", "meter": "center", "drc": "4", "anti_flicker": "60hz",
            "luminance": 7, "contrast": 7, "sharpness": 6, "noise3d": 6, "gamma": "default",
            "horizontal_flip": "off", "vertical_flip": "off",
            "out_hdmi": "on", "out_3g_sdi": "off", "out_12g_sdi": "off", "out_usb": "on", "out_ip": "on",
            "output_resolution": "2160p60",
            "s1_encode": "h264", "s1_framerate": 60, "s1_bitrate_control": "cbr", "s1_slice": "off",
            "s1_width": 1920, "s1_height": 1080, "s1_gop": 180, "s1_bitrate": 3000, "s1_qfactor": 80,
            "s1_maxq": 51, "s1_minq": 1,
            "s2_encode": "h264", "s2_framerate": 30, "s2_bitrate_control": "cbr", "s2_width": 640,
            "s2_height": 360, "s2_gop": 30, "s2_bitrate": 2048,
            "ai_enable": "off", "ai_mode": "tracking", "ai_debug_mode": "default", "ai_hint_cue": "off",
            "ai_lost_timeout": 6, "ai_figure_size": "full", "ai_custom_figure_size": "80",
            "ai_figure_position": "middle", "ai_start_position": "current", "ai_start_area": "a",
            "tally_enable": "on", "tally_action": "off", "tally_status": "off", "tally_option": "red",
            "rtmp1_enable": "off", "rtmp1_mrl": "rtmps://", "rtmp1_key": "", "rtmp1_video": "on",
            "rtmp1_audio": "on", "rtmp1_active": "fail", "rtmp1_err_msg": "Stream disabled",
            "rtmp2_enable": "off", "rtmp2_mrl": "rtmps://", "rtmp2_key": "", "rtmp2_video": "on",
            "rtmp2_audio": "on", "rtmp2_active": "fail", "rtmp2_err_msg": "Stream disabled",
            "srt_enable": "off", "srt_mode": "listener", "srt_server": "192.168.100.1", "srt_port": 4578,
            "srt_encryption": "none", "srt_password": "1234567890", "srt_bandwidth": 25,
            "srt_latency": 500, "srt_stream_id": "",
            "multicast_enable": "off", "multicast_ip": "224.1.2.3", "multicast_port": 6688,
            "onvif_enable": "off", "onvif_auth": "off",
            # simulator bookkeeping
            "preset_speed": "12", "last_preset": "", "last_presenter_select": "",
        },
        "delays": {"command_response": 0.02},
        "controls": [
            {"type": "select", "key": "power", "label": "Power (on = active)", "options": ["on", "off"]},
            {"type": "select", "key": "tally_action", "label": "Tally light", "options": ["on", "off", "blink"]},
            {"type": "select", "key": "tally_enable", "label": "Tally enabled", "options": ["on", "off"]},
            {"type": "select", "key": "ai_enable", "label": "Auto tracking", "options": ["on", "off"]},
            {"type": "select", "key": "record_status", "label": "Recording", "options": ["on", "off"]},
            {"type": "select", "key": "freeze", "label": "Freeze", "options": ["on", "off"]},
            {"type": "select", "key": "focus_mode", "label": "Focus mode", "options": ["auto", "manual"]},
            {"type": "select", "key": "exposure_mode", "label": "Exposure mode",
             "options": ["fullAuto", "manual", "shutterPriority", "irisPriority", "bright"]},
            {"type": "select", "key": "out_ip", "label": "IP output", "options": ["on", "off"]},
            {"type": "indicator", "key": "pan", "label": "Pan"},
            {"type": "indicator", "key": "tilt", "label": "Tilt"},
            {"type": "indicator", "key": "zoom", "label": "Zoom"},
            {"type": "indicator", "key": "focus", "label": "Focus"},
            {"type": "indicator", "key": "last_preset", "label": "Last preset recalled"},
            {"type": "indicator", "key": "last_presenter_select", "label": "Last presenter selection"},
        ],
        "error_modes": {
            "communication_timeout": {
                "description": "Camera stops answering",
                "behavior": "no_response",
            },
            "password_changed": {
                "description": "The camera's password was changed: every request is refused (HTTP 401)",
                "behavior": "custom",
            },
            "exposure_read_refused": {
                "description": "The camera refuses the exposure read (HTTP 500)",
                "behavior": "custom",
            },
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        cfg = self.config or {}
        self._username = str(cfg.get("username", "") or "admin")
        self._password = str(cfg.get("password", "") or "")
        self._standby_delay = float(cfg.get("standby_delay", 2.0) or 0)
        self._wake_delay = float(cfg.get("wake_delay", 4.0) or 0)
        self._reboot_downtime = float(cfg.get("reboot_downtime", 5.0) or 0)
        self._nonces: set[str] = set()
        self._presets: dict[int, tuple[int, int, int, int]] = {}
        # axis -> (signed units per second, start time, start position, stop
        # position or None for a jog)
        self._moves: dict[str, tuple[float, float, float, float | None]] = {}
        self._power_change: tuple[str, float] | None = None
        self._down_until = 0.0
        self.requests: list[str] = []

    # ── Positions and power, advanced lazily ──

    def _axis(self, axis: str) -> int:
        move = self._moves.get(axis)
        if move is None:
            return int(self.state.get(STATE_KEY[axis], 0))
        rate, t0, p0, stop = move
        lo, hi = LIMITS[axis]
        position = p0 + rate * (time.monotonic() - t0)
        if stop is not None:
            position = min(position, stop) if rate > 0 else max(position, stop)
        return int(round(max(lo, min(hi, position))))

    def _halt(self, axis: str) -> None:
        if axis in self._moves:
            self.set_state(STATE_KEY[axis], self._axis(axis))
            del self._moves[axis]

    def _jog(self, axis: str, direction: int, speed: int) -> None:
        self._halt(axis)
        if direction:
            rate = direction * RATE[axis] * max(1, speed)
            self._moves[axis] = (rate, time.monotonic(), self._axis(axis), None)

    def _travel(self, axis: str, target: int, speed: int) -> None:
        self._halt(axis)
        lo, hi = LIMITS[axis]
        target = max(lo, min(hi, target))
        here = self._axis(axis)
        if target == here:
            return
        rate = RATE[axis] * max(1, speed) * (1 if target > here else -1)
        self._moves[axis] = (rate, time.monotonic(), here, target)

    def _settle(self) -> None:
        """Finish moves that have arrived and a power change that is due, and
        keep the Simulator UI's indicators moving."""
        for axis in list(self._moves):
            rate, t0, p0, stop = self._moves[axis]
            position = self._axis(axis)
            if stop is not None and position == int(round(stop)):
                del self._moves[axis]
            self.set_state(STATE_KEY[axis], position)
        if self._power_change and time.monotonic() >= self._power_change[1]:
            self.set_state("power", self._power_change[0])
            self._power_change = None

    # ── Authentication ──

    def _challenge(self) -> tuple[int, str, dict[str, str]]:
        nonce = secrets.token_hex(8)
        self._nonces.add(nonce)
        return 401, "401 Unauthorized", {
            "WWW-Authenticate": f'Digest algorithm="MD5", qop="auth", realm="{REALM}", nonce="{nonce}"',
        }

    def _authorized(self, method: str, headers: dict[str, str]) -> bool:
        auth = next((v for k, v in headers.items() if k.lower() == "authorization"), "")
        if not auth.startswith("Digest "):
            return False
        fields = {k.lower(): v.strip('"') for k, v in re.findall(r'(\w+)=("[^"]*"|[^,\s]+)', auth[7:])}
        if fields.get("username") != self._username or fields.get("nonce") not in self._nonces:
            return False

        def expected(password: str) -> str:
            ha1 = hashlib.md5(f"{self._username}:{REALM}:{password}".encode()).hexdigest()
            ha2 = hashlib.md5(f"{method}:{fields.get('uri', '')}".encode()).hexdigest()
            tail = f"{fields.get('nc', '')}:{fields.get('cnonce', '')}:{fields.get('qop', '')}:" if fields.get("qop") else ""
            return hashlib.md5(f"{ha1}:{fields['nonce']}:{tail}{ha2}".encode()).hexdigest()

        answer = fields.get("response", "")
        if self._password:
            return answer == expected(self._password)
        return answer != expected("invalid")

    # ── Dispatch ──

    def handle_request(self, method: str, path: str, headers: dict[str, str], body: str):
        if time.monotonic() < self._down_until:
            return 503, "Service Unavailable"
        if "password_changed" in self.active_errors or not self._authorized(method, headers):
            return self._challenge()
        self.requests.append(f"{method} {path}" + (f" {body}" if body else ""))
        del self.requests[:-200]
        self._settle()
        resource = urlsplit(path).path
        if not resource.startswith(API + "/"):
            return 404, "Not Found"
        resource = resource[len(API):]
        payload: Any = None
        if body.strip():
            try:
                payload = json.loads(body)
            except ValueError:
                return 400, {"error": "body is not JSON"}
        if method == "GET":
            return self._get(resource)
        if method == "PATCH":
            return self._patch(resource, payload)
        if method == "POST":
            return self._post(resource, payload)
        return 405, "Method Not Allowed"

    def _get(self, resource: str):
        if resource == "/image/exposure/param" and "exposure_read_refused" in self.active_errors:
            return 500, "Internal Server Error"
        if resource == "/movement/absolute/position":
            return 200, {
                "panPos": self._axis("pan"), "panSpeed": 0,
                "tiltPos": self._axis("tilt"), "tiltSpeed": 0,
                "zoomPos": self._axis("zoom"), "zoomSpeed": 0,
            }
        if resource == "/movement/current-position":
            return 200, {
                "currentPanPosition": self._axis("pan"),
                "currentTiltPosition": self._axis("tilt"),
                "currentZoomPosition": self._axis("zoom"),
            }
        if resource == "/focus/position":
            return 200, {"focusPos": str(self._axis("focus"))}
        fields = RESOURCES.get(resource)
        if fields is None:
            return 404, "Not Found"
        out: dict[str, Any] = {}
        for path, key, kind, _accepts in fields:
            value = self.state.get(key, "")
            if isinstance(value, bool):  # a Simulator UI toggle stores a real boolean
                value = "on" if value else "off"
            if kind == "n":
                value = _int(value) if _int(value) is not None else value
            else:
                value = str(value)
            node = out
            parts = path.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
        return 200, out

    def _accepts(self, accepts: Any, value: Any) -> bool:
        if accepts is None:
            return False
        if accepts == ANY:
            return isinstance(value, (str, int)) and not isinstance(value, bool)
        if accepts == INT:
            return _int(value) is not None
        return isinstance(value, str) and value in accepts

    def _post(self, resource: str, payload: Any):
        ok, bad = (200, ""), (400, {"error": "invalid request"})
        handler = {
            "/movement/relative/pan-tilt": self._pan_tilt,
            "/movement/absolute/position": self._absolute,
            "/movement/relative/position": self._relative,
            "/movement/relative/zoom": self._zoom_focus,
            "/movement/relative/focus": self._zoom_focus,
            "/movement/reset": self._recalibrate,
            "/presets/home": self._home,
            "/presets/save": self._preset,
            "/presets/recall": self._preset,
            "/presets/reset": self._preset,
            "/presets/speed": self._preset_speed,
            "/ai/presenter/select": self._presenter,
            "/image/color/onepush": lambda r, p: ok,
            "/osd/save": lambda r, p: ok,
            "/osd/restoredefault": self._restore_defaults,
            "/misc/reset/system": self._factory_reset,
            "/misc/reset/network": lambda r, p: ok,
            "/misc/restore": lambda r, p: ok,
        }.get(resource)
        if handler is not None:
            return handler(resource, payload)
        fields = RESOURCES.get(resource)
        if fields is None:
            return 404, "Not Found"
        leaves = _leaves(payload)
        if not leaves:
            return bad
        by_path = {path: (key, kind, accepts) for path, key, kind, accepts in fields}
        for path, value in leaves:
            spec = by_path.get(path)
            if spec is None or not self._accepts(spec[2], value):
                return bad
            if spec[0] == "focus" and not 236 <= (_int(value) or 0) <= 4148:
                return bad
        for path, value in leaves:
            key, kind, _accepts = by_path[path]
            self._write_field(resource, path, key, kind, value)
        return ok

    def _write_field(self, resource: str, path: str, key: str, kind: str, value: Any) -> None:
        if resource == "/misc/standby":
            target = str(value)
            delay = self._wake_delay if target == "on" else self._standby_delay
            if target == self.state.get("power"):
                self._power_change = None
            elif delay <= 0:
                self.set_state("power", target)
            else:
                self._power_change = (target, time.monotonic() + delay)
            return
        if value == "toggle":
            if key == "focus_mode":
                value = "manual" if self.state.get(key) == "auto" else "auto"
            else:
                value = "off" if self.state.get(key) == "on" else "on"
        if key == "focus_mode" and value == "onepush":
            self._travel("focus", 2600, 7)
            return
        if key == "osd" and value not in ("on", "off"):
            return  # moving through the menu changes nothing reported
        if key == "focus":
            self._travel("focus", _int(value) or 0, 7)
            return
        if key == "record_status" and value == "on" and self.state.get("record_status") != "on":
            self.set_state("record_filename", datetime.now().strftime("ec20_ep0000000_%Y%m%d_%H%M%S"))
        if key.startswith("rtmp") and key.endswith("_enable"):
            stream = key[:5]
            on = value == "on"
            self.set_state(f"{stream}_active", "success" if on else "fail")
            self.set_state(f"{stream}_err_msg", "" if on else "Stream disabled")
        self.set_state(key, _int(value) if kind == "n" else str(value))

    def _patch(self, resource: str, payload: Any):
        bad = (400, {"error": "invalid request"})
        if resource == "/presets/speed":
            op = payload.get("op") if isinstance(payload, dict) else None
            if op not in ("inc", "dec"):
                return bad
            speed = int(self.state.get("preset_speed", 12)) + (1 if op == "inc" else -1)
            self.set_state("preset_speed", str(max(0, min(24, speed))))
            return 200, ""
        steppable = STEPPABLE.get(resource)
        if steppable is None:
            return 404, "Not Found"
        if not isinstance(payload, dict):
            return bad
        key = steppable.get(str(payload.get("target", "")))
        op = payload.get("op")
        if key is None or op not in ("inc", "dec", "reset"):
            return bad
        if op == "reset":
            self.set_state(key, self.SIMULATOR_INFO["initial_state"][key])
            return 200, ""
        step = _int(payload.get("value", "1"))
        current = _int(self.state.get(key))
        if step is None or step < 1 or current is None:
            return bad
        new = current + (step if op == "inc" else -step)
        self.set_state(key, new if isinstance(self.state.get(key), int) else str(new))
        return 200, ""

    # ── Movement ──

    def _speed(self, payload: dict, field: str, default: int) -> int:
        value = _int(payload.get(field, default))
        return default if value is None else value

    def _pan_tilt(self, resource: str, payload: Any):
        if not isinstance(payload, dict):
            return 400, {"error": "invalid request"}
        action = payload.get("action")
        if action == "stop":
            self._halt("pan")
            self._halt("tilt")
            return 200, ""
        if action not in JOG:
            return 400, {"error": "invalid request"}
        pan_speed = self._speed(payload, "pan_speed", 12)
        tilt_speed = self._speed(payload, "tilt_speed", 10)
        if not 1 <= pan_speed <= 24 or not 1 <= tilt_speed <= 20:
            return 400, {"error": "invalid request"}
        pan, tilt = JOG[action]
        self._jog("pan", pan, pan_speed)
        self._jog("tilt", tilt, tilt_speed)
        return 200, ""

    def _absolute(self, resource: str, payload: Any):
        if not isinstance(payload, dict):
            return 400, {"error": "invalid request"}
        targets = {axis: _int(payload.get(f"{axis}Pos")) for axis in ("pan", "tilt", "zoom")}
        if any(payload.get(f"{axis}Pos") is not None and value is None for axis, value in targets.items()):
            return 400, {"error": "invalid request"}
        zoom = targets["zoom"]
        if zoom is not None and not 0 <= zoom <= 4912:
            return 400, {"error": "invalid request"}
        for axis, value in targets.items():
            if value is not None:
                self._travel(axis, value, self._speed(payload, f"{axis}Speed", 7) or 1)
        return 200, ""

    def _relative(self, resource: str, payload: Any):
        if not isinstance(payload, dict):
            return 400, {"error": "invalid request"}
        pan, tilt = _int(payload.get("panPos")), _int(payload.get("tiltPos"))
        if pan is None or tilt is None:
            return 400, {"error": "invalid request"}
        self._travel("pan", self._axis("pan") + pan, self._speed(payload, "panSpeed", 12) or 1)
        self._travel("tilt", self._axis("tilt") + tilt, self._speed(payload, "tiltSpeed", 10) or 1)
        return 200, ""

    def _zoom_focus(self, resource: str, payload: Any):
        if not isinstance(payload, dict):
            return 400, {"error": "invalid request"}
        axis = "zoom" if resource.endswith("zoom") else "focus"
        words = {"zoom": {"tele": 1, "wide": -1}, "focus": {"far": 1, "near": -1}}[axis]
        action = payload.get("action")
        if action == "stop":
            self._halt(axis)
            return 200, ""
        if action not in words:
            return 400, {"error": "invalid request"}
        speed = self._speed(payload, "speed", 3)
        if not 1 <= speed <= 7 or payload.get("type", "standard") not in ("standard", "variable"):
            return 400, {"error": "invalid request"}
        if payload.get("type", "standard") == "standard":
            speed = 3
        self._jog(axis, words[action], speed)
        return 200, ""

    def _recalibrate(self, resource: str, payload: Any):
        self._travel("pan", 0, 24)
        self._travel("tilt", 0, 20)
        return 200, ""

    def _home(self, resource: str, payload: Any):
        if payload:
            return 400, {"error": "no body expected"}
        self._travel("pan", 0, 24)
        self._travel("tilt", 0, 20)
        return 200, ""

    def _preset(self, resource: str, payload: Any):
        number = _int(payload.get("presetNumber")) if isinstance(payload, dict) else None
        if number is None or not 0 <= number <= 254:
            return 400, {"error": "invalid request"}
        if resource.endswith("save"):
            self._presets[number] = tuple(self._axis(a) for a in ("pan", "tilt", "zoom", "focus"))
        elif resource.endswith("reset"):
            self._presets.pop(number, None)
        else:
            stored = self._presets.get(number)
            if stored is not None:
                speed = max(1, _int(self.state.get("preset_speed", 12)) or 1)
                for axis, position in zip(("pan", "tilt", "zoom", "focus"), stored):
                    self._travel(axis, position, speed)
            self.set_state("last_preset", str(number))
        return 200, ""

    def _preset_speed(self, resource: str, payload: Any):
        level = _int(payload.get("level")) if isinstance(payload, dict) else None
        if level is None or not 0 <= level <= 24:
            return 400, {"error": "invalid request"}
        self.set_state("preset_speed", str(level))
        return 200, ""

    def _presenter(self, resource: str, payload: Any):
        selection = payload.get("selection") if isinstance(payload, dict) else None
        if selection not in ("left", "right", "confirm"):
            return 400, {"error": "invalid request"}
        self.set_state("last_presenter_select", selection)
        return 200, ""

    # ── Resets ──

    def _restore_defaults(self, resource: str, payload: Any):
        initial = self.SIMULATOR_INFO["initial_state"]
        for image in ("/image/color/param", "/image/exposure/param", "/image/picture/param", "/image/orientation/param"):
            for _path, key, _kind, _accepts in RESOURCES[image]:
                self.set_state(key, initial[key])
        return 200, ""

    def _factory_reset(self, resource: str, payload: Any):
        keep = payload.get("keepNetwork") if isinstance(payload, dict) else None
        if keep not in ("true", "false"):
            return 400, {"error": "invalid request"}
        for key, value in self.SIMULATOR_INFO["initial_state"].items():
            self.set_state(key, value)
        self._moves.clear()
        self._presets.clear()
        self._power_change = None
        self._down_until = time.monotonic() + self._reboot_downtime
        return 200, ""
