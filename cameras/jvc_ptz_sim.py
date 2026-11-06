"""
JVC PTZ Camera (JVC Web API) — Simulator.

Simulates a JVC KY-PZ510, KY-PZ400 or KY-PZ200 (config ``model``, default
PZ510) as the JVC Camcorder Web API Reference 1.21 describes it:

* ``GET /api.php`` under HTTP Digest (user ``jvc``) answers ``302`` with a
  ``SessionID`` cookie.
* ``POST /cgi-bin/api.cgi`` takes ``{"Request":{"Command":..,"SessionID":..,
  "Params":{..}}}`` and answers ``{"Response":{"Requested":..,"Result":..,
  "Data":{..}}}``, always HTTP 200. A body with whitespace outside its strings
  is a ``FormatError`` (the document: the camera "cannot accept any space
  character or newline within command string"), an unknown or expired session
  a ``SessionError``, a command the model lacks a ``CommandError``, a value out
  of range a ``RequestedError``.

Modelled from the document: GetSystemInfo, GetCamStatus and
GetCamStatusMinimum (with the Enable flags), GetPTPosition, SetPTCtrl,
JoyStickOperation, ZoomSwitchOperation, SetZoomCtrl, SetPTZPreset (presets also
hold exposure, white balance and picture settings, as the KY-PZ510 firmware
3.0.12 notes say), SetWebButtonEvent, SetWebXYFieldEvent, SetStreamingCtrl,
SetTallyLampCtrl and SetStudioTally (KY-PZ400 / KY-PZ510), SystemRequest,
GetStreamingServerSettings for RTSP, and on the KY-PZ510 every auto tracking
command, with the settings one set shared by every tracking mode (JVC's
tracking manual) and the Area / Stage / Wide Area positions stored as presets
187-195.

Readings, not documented, chosen so the driver's handling can be seen:

* The Digest realm is the model name (the document's only example, a
  GY-HM650, uses its model). A session ends after ``session_timeout`` seconds
  with no request (default 30; section 2.1 says 30).
* Iris, shutter, gain and bright step through the values JVC's Standard
  Command List gives (F-numbers, 1/30 to 1/10000, 0 to 45 dB, 0 to 17), and
  their buttons are enabled in the exposure modes they belong to; a disabled
  button is answered Success and does nothing. White paint goes to 20 in auto
  white balance and 255 in manual (the document's XYField note).
* Starting the stream goes Starting then Start after 2 s; stopping goes
  Stopping then Stop after 1 s. While the on-screen menu is open, start and
  stop do nothing (the document).
* The tally lamp lights only while the camera's Tally Mode menu item is On.
* Recalling a preset never stored answers Success and does not move.
* Changing the tracking mode while tracking is on answers DisableError.
* A pan left or right while tracking is on switches the tracked person and
  does not move the camera (the document, JoyStickOperation).

Motion is computed from when a move started, so positions advance between
reads with no background task.

Error modes: ``communication_timeout`` (answers nothing) and
``password_changed`` (every request is refused 401).

Driver: jvc_ptz
Transport: http
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from typing import Any

from openavc.simulator.http_simulator import HTTPSimulator

MODELS = ("PZ510", "PZ400", "PZ200")
TALLY_MODELS = ("PZ400", "PZ510")
TRACKING_MODELS = ("PZ510",)

# JVC Standard Command List: iris 0 (close) and F11 to F1.8, shutter 1/30 to
# 1/10000, gain 0 to 45 dB in 3 dB steps, bright 0 to 17.
IRIS = ["Close", "F11", "F9.6", "F8.0", "F6.8", "F5.6", "F4.8", "F4.0", "F3.4", "F2.8", "F2.4", "F2.0", "F1.8"]
SHUTTER = [
    "1/30", "1/60", "1/90", "1/100", "1/125", "1/180", "1/250", "1/350", "1/500",
    "1/725", "1/1000", "1/1500", "1/2000", "1/3000", "1/4000", "1/6000", "1/10000",
]
GAIN = [f"{3 * i}dB" for i in range(16)]

LIMITS = {"pan": (0, 35080), "tilt": (0, 12080), "zoom": (0, 499), "focus": (0, 5180)}
HOME = {"pan": 17540, "tilt": 9040}
# Counts per second per speed step.
RATE = {"pan": 400.0, "tilt": 200.0, "zoom": 40.0}
FOCUS_RATE = {1: 100.0, 2: 400.0, 3: 1600.0}

PICTURE_RANGES = {"Luminance": (0, 14), "Saturation": (0, 14), "Contrast": (0, 14), "Sharpness": (0, 11), "Hue": (0, 14)}
TRACKING_PRESETS = {
    ("Area", "A1"): 187, ("Area", "A2"): 188, ("Area", "A3"): 189, ("Area", "A4"): 190,
    ("Stage", "Start"): 191, ("Stage", "Left"): 192, ("Stage", "Right"): 193,
    ("WideArea", "W1"): 194, ("WideArea", "W2"): 195,
}
# Settings a preset holds besides position (KY-PZ510 firmware 3.0.12 notes).
PRESET_SETTINGS = (
    "Exposure", "IrisIndex", "ShutterIndex", "GainIndex", "BrightIndex", "WbMode",
    "WhPR", "WhPB", "FocusMode", "Luminance", "Saturation", "Contrast", "Sharpness",
    "Hue", "Flip", "Mirror",
)
TRACKING_FIELDS = {
    "AutoZoom": "AutoZoom", "AutoTilt": "AutoTilt", "TargetRetentionTime": "Retention",
    "FigureSize": "FigureSize", "FigureSizeCustom": "FigureCustom",
    "VerticalPosition": "Vertical", "HorizontalPosition": "Horizontal",
    "StartPosition": "StartPosition", "StartingArea": "StartingArea", "DurationTime": "Duration",
}
TRACKING_GETS = {
    "Standard": ("AutoZoom", "AutoTilt", "TargetRetentionTime", "FigureSize", "FigureSizeCustom",
                 "VerticalPosition", "HorizontalPosition", "StartPosition"),
    "Area": ("TargetRetentionTime", "StartingArea"),
    "Stage": ("TargetRetentionTime", "HorizontalPosition"),
    "WideArea": ("AutoZoom", "AutoTilt", "TargetRetentionTime", "FigureSize", "FigureSizeCustom",
                 "VerticalPosition", "HorizontalPosition", "StartPosition"),
    "FineAdjustment": ("AutoZoom", "AutoTilt", "DurationTime", "FigureSize", "FigureSizeCustom",
                       "VerticalPosition", "HorizontalPosition"),
}
START_POSITIONS = {
    "Standard": ("CurrentPosition", "HomePosition", "Preset1"),
    "WideArea": ("CurrentPosition", "HomePosition", "Preset1", "W1", "W2"),
}


def _whitespace_outside_strings(text: str) -> bool:
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in " \t\r\n":
            return True
    return False


def _int(value: Any) -> int | None:
    """The document allows an Integer or a String for several PTZ values."""
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class JVCPTZSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "jvc_ptz",
        "name": "JVC PTZ Camera Simulator",
        "category": "camera",
        "transport": "http",
        "default_port": 80,
        "initial_state": {
            "Exposure": "Auto", "IrisIndex": 12, "ShutterIndex": 1, "GainIndex": 0, "BrightIndex": 8,
            "WbMode": "Auto", "WhPR": 10, "WhPB": 10,
            "FocusMode": "Auto", "Focus": 1200, "Zoom": 0, "Pan": 17540, "Tilt": 9040,
            "Luminance": 7, "Saturation": 4, "Contrast": 7, "Sharpness": 5, "Hue": 7,
            "Flip": "Off", "Mirror": "Off", "SpeedWithZoom": "Off", "PresetSpeed": 20,
            "Streaming": "Stop", "Lighting": "Off", "StudioTally": "Off", "TallyMode": "On",
            "MenuStatus": "Off", "VideoOutput": "On",
            "Tracking": "Off", "TrackingMode": "Standard", "BoundingBox": "Auto",
            "AutoZoom": "On", "AutoTilt": "On", "Retention": 10, "FigureSize": "Full",
            "FigureCustom": 141, "Vertical": 0, "Horizontal": "Center",
            "StartPosition": "CurrentPosition", "StartingArea": "A1", "Duration": 3,
            "RtspPort": 554, "RtspAuth": "0",
            "last_preset": "", "target_switches": 0,
        },
        "delays": {"command_response": 0.02},
        "controls": [
            {"type": "select", "key": "Exposure", "label": "Exposure mode",
             "options": ["Auto", "Manual", "IrisPriority", "ShutterPriority", "Bright"]},
            {"type": "select", "key": "WbMode", "label": "White balance",
             "options": ["Auto", "Indoor", "Outdoor", "OnePush", "Manual"]},
            {"type": "select", "key": "FocusMode", "label": "Focus mode", "options": ["Auto", "Manual"]},
            {"type": "select", "key": "StudioTally", "label": "Studio tally", "options": ["Off", "Program", "Preview"]},
            {"type": "select", "key": "TallyMode", "label": "Tally Mode (camera menu)", "options": ["On", "Off"]},
            {"type": "select", "key": "MenuStatus", "label": "On-screen menu", "options": ["Off", "On"]},
            {"type": "select", "key": "Tracking", "label": "Auto tracking (KY-PZ510)", "options": ["Off", "On"]},
            {"type": "select", "key": "TrackingMode", "label": "Tracking mode",
             "options": ["Standard", "Area", "Stage", "WideArea", "FineAdjustment"]},
            {"type": "select", "key": "RtspAuth", "label": "RTSP Auth", "options": ["0", "1"]},
            {"type": "indicator", "key": "Pan", "label": "Pan"},
            {"type": "indicator", "key": "Tilt", "label": "Tilt"},
            {"type": "indicator", "key": "Zoom", "label": "Zoom"},
            {"type": "indicator", "key": "Streaming", "label": "Streaming"},
            {"type": "indicator", "key": "last_preset", "label": "Last preset recalled"},
            {"type": "indicator", "key": "target_switches", "label": "Tracked person switched"},
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
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        cfg = self.config or {}
        model = str(cfg.get("model", "") or "").strip().upper()
        self._model = model if model in MODELS else "PZ510"
        self._password = str(cfg.get("password", "") or "")
        self._session_timeout = float(cfg.get("session_timeout", 30.0) or 30.0)
        self._reboot_downtime = float(cfg.get("reboot_downtime", 5.0) or 0)
        self._nonces: set[str] = set()
        self._sessions: dict[str, float] = {}
        self._presets: dict[int, dict[str, Any]] = {}
        # axis -> (start value, target, counts per second, start time)
        self._moves: dict[str, tuple[float, float, float, float]] = {}
        self._stream_change: tuple[str, float] | None = None
        self._down_until = 0.0
        self.requests: list[str] = []

    # ── Motion, advanced lazily ──

    def _axis(self, axis: str) -> int:
        key = axis.capitalize()
        move = self._moves.get(axis)
        if move is None:
            return int(self.state.get(key, 0))
        start, target, rate, t0 = move
        travelled = rate * (time.monotonic() - t0)
        if target >= start:
            return int(min(target, start + travelled))
        return int(max(target, start - travelled))

    def _stop(self, axis: str) -> None:
        if axis in self._moves:
            self.set_state(axis.capitalize(), self._axis(axis))
            del self._moves[axis]

    def _move(self, axis: str, target: float, rate: float) -> None:
        lo, hi = LIMITS[axis]
        current = self._axis(axis)
        self._moves.pop(axis, None)
        self.set_state(axis.capitalize(), current)
        self._moves[axis] = (current, max(lo, min(hi, target)), max(rate, 1.0), time.monotonic())

    def _jog(self, axis: str, direction: int, speed: int) -> None:
        if direction == 0 or speed <= 0:
            self._stop(axis)
            return
        lo, hi = LIMITS[axis]
        self._move(axis, hi if direction > 0 else lo, RATE[axis] * speed)

    def _settle(self) -> None:
        for axis in list(self._moves):
            start, target, rate, t0 = self._moves[axis]
            value = self._axis(axis)
            if self.state.get(axis.capitalize()) != value:
                self.set_state(axis.capitalize(), value)
            if value == int(target):
                del self._moves[axis]
        if self._stream_change and time.monotonic() >= self._stream_change[1]:
            self.set_state("Streaming", self._stream_change[0])
            self._stream_change = None

    # ── Authentication ──

    def _challenge(self) -> tuple[int, str, dict[str, str]]:
        nonce = secrets.token_hex(16)
        self._nonces.add(nonce)
        return 401, "401 Unauthorized", {
            "WWW-Authenticate": f'Digest realm="{self._model}", nonce="{nonce}", qop="auth"',
        }

    def _authorized(self, method: str, headers: dict[str, str]) -> bool:
        auth = next((v for k, v in headers.items() if k.lower() == "authorization"), "")
        if not auth.startswith("Digest "):
            return False
        fields = {k.lower(): v.strip('"') for k, v in re.findall(r'(\w+)=("[^"]*"|[^,\s]+)', auth[7:])}
        if fields.get("username") != "jvc" or fields.get("nonce") not in self._nonces:
            return False

        def expected(password: str) -> str:
            ha1 = hashlib.md5(f"jvc:{self._model}:{password}".encode()).hexdigest()
            ha2 = hashlib.md5(f"{method}:{fields.get('uri', '')}".encode()).hexdigest()
            tail = f"{fields.get('nc', '')}:{fields.get('cnonce', '')}:{fields.get('qop', '')}:" if fields.get("qop") else ""
            return hashlib.md5(f"{ha1}:{fields['nonce']}:{tail}{ha2}".encode()).hexdigest()

        answer = fields.get("response", "")
        if self._password:
            return answer == expected(self._password)
        return answer != expected("invalid")

    def _session_valid(self, session: Any) -> bool:
        last = self._sessions.get(str(session or ""))
        if last is None:
            return False
        if time.monotonic() - last > self._session_timeout:
            del self._sessions[str(session)]
            return False
        self._sessions[str(session)] = time.monotonic()
        return True

    # ── Dispatch ──

    def handle_request(self, method: str, path: str, headers: dict[str, str], body: str):
        if time.monotonic() < self._down_until:
            return 503, "Service Unavailable"
        if "password_changed" in self.active_errors or not self._authorized(method, headers):
            return self._challenge()
        self.requests.append(f"{method} {path} {body}".rstrip())
        route = path.split("?")[0]
        if route == "/api.php" and method == "GET":
            session = secrets.token_hex(16)
            self._sessions[session] = time.monotonic()
            return 302, "", {"Set-Cookie": f"SessionID={session}"}
        if route != "/cgi-bin/api.cgi" or method != "POST":
            return 404, "404 Not Found"
        self._settle()
        if _whitespace_outside_strings(body):
            return 200, self._answer("", "FormatError")
        try:
            request = json.loads(body).get("Request") or {}
        except (ValueError, AttributeError):
            return 200, self._answer("", "FormatError")
        command = str(request.get("Command", "") or "")
        if not self._session_valid(request.get("SessionID")):
            return 200, self._answer(command, "SessionError")
        params = request.get("Params") or {}
        if not isinstance(params, dict):
            return 200, self._answer(command, "FormatError")
        try:
            result, data = self._dispatch(command, params)
        except _Refuse as exc:
            result, data = exc.result, None
        return 200, self._answer(command, result, data)

    @staticmethod
    def _answer(command: str, result: str, data: dict | None = None) -> dict:
        response: dict[str, Any] = {"Requested": command, "Result": result}
        if data is not None:
            response["Data"] = data
        return {"Response": response}

    def _dispatch(self, command: str, p: dict[str, Any]) -> tuple[str, dict | None]:
        tally = command in ("SetTallyLampCtrl", "SetStudioTally")
        tracking = command.startswith(("SetTracking", "GetTracking", "ExecuteTracking"))
        if (tally and self._model not in TALLY_MODELS) or (tracking and self._model not in TRACKING_MODELS):
            raise _Refuse("CommandError")
        handler = getattr(self, f"_cmd_{command}", None)
        if handler is not None:
            return "Success", handler(p)
        if tracking:
            return "Success", self._tracking(command, p)
        raise _Refuse("CommandError")

    # ── System ──

    def _cmd_GetSystemInfo(self, p: dict) -> dict:
        data = {
            "Model": self._model, "Destination": "US",
            "ApiVersion": {"PZ510": "1.21.000", "PZ400": "1.20.000", "PZ200": "1.20.000"}[self._model],
            "Serial": "23500123", "Srt": "On",
        }
        if self._model == "PZ510":
            data["NDI"] = "On"
        return data

    def _cmd_SystemRequest(self, p: dict) -> None:
        request = p.get("Request")
        if request == "Reboot":
            self._down_until = time.monotonic() + self._reboot_downtime
            self._sessions.clear()
            return None
        if request == "Restore":
            for key, value in self.SIMULATOR_INFO["initial_state"].items():
                self.set_state(key, value)
            self._presets.clear()
            self._moves.clear()
            return None
        raise _Refuse("RequestedError")

    # ── Status ──

    def _exposure_flags(self) -> dict[str, int]:
        mode = self.state.get("Exposure")
        return {
            "iris": int(mode in ("Manual", "IrisPriority")),
            "shutter": int(mode in ("Manual", "ShutterPriority")),
            "gain": int(mode == "Manual"),
            "bright": int(mode == "Bright"),
        }

    def _wb_scale(self) -> int:
        return 255 if self.state.get("WbMode") == "Manual" else 20

    def _common_status(self) -> dict[str, Any]:
        s = self.state
        mode = s.get("Exposure")
        flags = self._exposure_flags()
        data: dict[str, Any] = {
            "Camera": {"Status": "NoCard", "VideoOutputStatus": s.get("VideoOutput"), "MenuStatus": s.get("MenuStatus")},
            "Exposure": {"Status": mode},
            "Iris": {"Status": "Manual" if flags["iris"] else "Auto", "Value": IRIS[int(s.get("IrisIndex", 12))]},
            "Shutter": {"Status": "Manual" if flags["shutter"] else "Auto", "Value": SHUTTER[int(s.get("ShutterIndex", 1))]},
            "Gain": {"Status": "Manual" if flags["gain"] else "Auto", "Value": GAIN[int(s.get("GainIndex", 0))]},
            "Whb": {
                "Status": s.get("WbMode"), "Value": "5600K" if s.get("WbMode") == "Outdoor" else "3200K",
                "WhPRScale": self._wb_scale(), "WhPBScale": self._wb_scale(),
                "WhPRPosition": int(s.get("WhPR", 0)), "WhPBPosition": int(s.get("WhPB", 0)),
                "WhPRValue": str(s.get("WhPR", 0)), "WhPBValue": str(s.get("WhPB", 0)),
            },
            "Focus": {"Status": s.get("FocusMode"), "Value": str(self._axis("focus"))},
            "Zoom": {"Position": self._axis("zoom")},
            "Bright": {"Value": str(s.get("BrightIndex", 0))},
            "Streaming": {"Status": s.get("Streaming")},
            "TallyLamp": {"Lighting": s.get("Lighting"), "StudioTally": s.get("StudioTally")},
        }
        enable: dict[str, Any] = {
            "Exposure": {"Auto": 1, "Manual": 1, "ShutterPriority": 1, "IrisPriority": 1, "Bright": 1},
            "Iris": {"Manual": 0, "Auto": 0, "Open1": flags["iris"], "Close1": flags["iris"]},
            "Gain": {"Auto": 0, "Manual": 0, "Up1": flags["gain"], "Down1": flags["gain"]},
            "Shutter": {"Slower": flags["shutter"], "Faster": flags["shutter"]},
            "Whb": {
                "Auto": 1, "Indoor": 1, "Outdoor": 1, "OnePush": 1, "Manual": 1,
                "AwbTrigger": int(s.get("WbMode") == "OnePush"),
            },
            "Focus": {"Manual": 1, "Auto": 1},
            "Bright": {"Up": flags["bright"], "Down": flags["bright"]},
        }
        if self._model in TRACKING_MODELS:
            data["Tracking"] = {"Status": s.get("Tracking")}
            data["TrackingMode"] = {"Status": s.get("TrackingMode")}
            tracking_off = int(s.get("Tracking") != "On")
            enable["Tracking"] = {"On": 1, "Off": 1}
            enable["TrackingMode"] = {m: tracking_off for m in ("Standard", "Area", "Stage", "WideArea", "FineAdjustment")}
        data["Enable"] = enable
        return data

    def _cmd_GetCamStatusMinimum(self, p: dict) -> dict:
        data = self._common_status()
        data["Enable"]["Preset"] = {str(n): 1 for n in range(1, 101)}
        return data

    def _cmd_GetCamStatus(self, p: dict) -> dict:
        s = self.state
        data = self._common_status()
        manual_focus = int(s.get("FocusMode") == "Manual")
        zoom = data["Zoom"]["Position"]
        data["Camera"].update({"WebAccess": "On"})
        data["Zoom"]["DisplayValue"] = f"Z{round(zoom * 149 / 499)}"
        data["PanTilt"] = {"SpeedWithZoom": s.get("SpeedWithZoom"), "PresetSpeed": int(s.get("PresetSpeed", 20))}
        for name in PICTURE_RANGES:
            data[name] = {"Value": int(s.get(name, 0))}
        data["Flip"] = {"Status": s.get("Flip")}
        data["Mirror"] = {"Status": s.get("Mirror")}
        data["SlotB"] = {"Status": "NoCard"}
        enable = data["Enable"]
        enable["Focus"].update({
            **{f"{d}{n}": manual_focus for d in ("Far", "Near") for n in (1, 2, 3)},
            "PushAuto": manual_focus,
        })
        enable["Zoom"] = {f"{d}{n}": 1 for d in ("Tele", "Wide") for n in (1, 2, 3)}
        enable["Streaming"] = {"On": 1, "Off": 1}
        enable["Menu"] = {"Display": 0, "Status": 0, "Menu": 1, "Set": 1, "Cancel": 1, "Up": 1, "Down": 1, "Left": 1, "Right": 1}
        for name in (*PICTURE_RANGES, "Flip", "Mirror"):
            enable[name] = {"Up": 1, "Down": 1}
        if self._model in TRACKING_MODELS:
            data["TrackingBoundingBox"] = {"Status": s.get("BoundingBox")}
            enable["TrackingBoundingBox"] = {"Auto": 1, "Off": 1, "On": 1}
        return data

    def _cmd_GetPTPosition(self, p: dict) -> dict:
        return {"Pan": self._axis("pan"), "Tilt": self._axis("tilt")}

    def _cmd_GetStreamingServerSettings(self, p: dict) -> dict:
        if p.get("Type") != "RTSP":
            raise _Refuse("RequestedError")
        return {"Type": "RTSP", "Rtsp": {"DstPort": int(self.state.get("RtspPort", 554)), "StreamId": "1", "Auth": int(self.state.get("RtspAuth", "0"))}}

    # ── Pan / tilt / zoom ──

    def _cmd_JoyStickOperation(self, p: dict) -> None:
        pan, tilt = p.get("PanDirection"), p.get("TiltDirection")
        pan_speed, tilt_speed = _int(p.get("PanSpeed")), _int(p.get("TiltSpeed"))
        if pan not in ("Stop", "Left", "Right") or tilt not in ("Stop", "Up", "Down"):
            raise _Refuse("RequestedError")
        if pan_speed is None or tilt_speed is None or not 0 <= pan_speed <= 30 or not 0 <= tilt_speed <= 30:
            raise _Refuse("RequestedError")
        if self.state.get("Tracking") == "On" and self._model in TRACKING_MODELS:
            # The document: while tracking, a pan left or right switches the
            # tracked person.
            if pan in ("Left", "Right") and pan_speed:
                self.set_state("target_switches", int(self.state.get("target_switches", 0)) + 1)
            return None
        # Left lowers the pan count (0 is the back-left limit); up lowers tilt
        # (0 is straight up).
        self._jog("pan", {"Stop": 0, "Left": -1, "Right": 1}[pan], pan_speed)
        self._jog("tilt", {"Stop": 0, "Up": -1, "Down": 1}[tilt], tilt_speed)
        return None

    def _cmd_SetPTCtrl(self, p: dict) -> None:
        for axis, word in (("pan", "Pan"), ("tilt", "Tilt")):
            direction = p.get(f"{word}Direction")
            speed = _int(p.get(f"{word}Speed"))
            if speed is None or not 0 <= speed <= 30:
                raise _Refuse("RequestedError")
            if direction == "Home":
                self._move(axis, HOME[axis], RATE[axis] * max(speed, 1))
            elif direction == "Position":
                position = _int(p.get(f"{word}Position"))
                lo, hi = LIMITS[axis]
                if position is None or not lo <= position <= hi:
                    raise _Refuse("RequestedError")
                if speed == 0:
                    self._stop(axis)
                else:
                    self._move(axis, position, RATE[axis] * speed)
            elif direction == "Stop" or speed == 0:
                self._stop(axis)
            elif direction in ("Left", "Right", "Up", "Down"):
                self._jog(axis, -1 if direction in ("Left", "Up") else 1, speed)
            else:
                raise _Refuse("RequestedError")
        return None

    def _cmd_ZoomSwitchOperation(self, p: dict) -> None:
        direction, speed = p.get("Direction"), _int(p.get("Speed"))
        if direction not in ("Stop", "Wide", "Tele") or speed is None or not 0 <= speed <= 8:
            raise _Refuse("RequestedError")
        self._jog("zoom", {"Stop": 0, "Wide": -1, "Tele": 1}[direction], speed)
        return None

    def _cmd_SetZoomCtrl(self, p: dict) -> None:
        position = _int(p.get("Position"))
        if position is None or not 0 <= position <= 499:
            raise _Refuse("RequestedError")
        self._stop("zoom")
        self.set_state("Zoom", position)
        return None

    def _cmd_SetPTZPreset(self, p: dict) -> None:
        number, operation = _int(p.get("No")), p.get("Operation")
        if number is None or not 1 <= number <= 255 or operation not in ("Move", "Set", "Delete"):
            raise _Refuse("RequestedError")
        if operation == "Set":
            self._store(number)
        elif operation == "Delete":
            self._presets.pop(number, None)
        else:
            self._recall(number)
        return None

    def _store(self, number: int) -> None:
        stored = {axis: self._axis(axis) for axis in ("pan", "tilt", "zoom")}
        stored.update({key: self.state.get(key) for key in PRESET_SETTINGS})
        self._presets[number] = stored

    def _recall(self, number: int) -> None:
        stored = self._presets.get(number)
        self.set_state("last_preset", str(number))
        if stored is None:
            return
        speed = int(self.state.get("PresetSpeed", 20))
        for axis in ("pan", "tilt"):
            self._move(axis, stored[axis], RATE[axis] * speed * 2)
        self._move("zoom", stored["zoom"], RATE["zoom"] * 8)
        for key in PRESET_SETTINGS:
            self.set_state(key, stored[key])

    # ── Buttons ──

    def _cmd_SetWebButtonEvent(self, p: dict) -> None:
        kind, button = p.get("Kind"), p.get("Button")
        s = self.state
        flags = self._exposure_flags()
        if kind == "Exposure" and button in ("Auto", "Manual", "IrisPriority", "ShutterPriority", "Bright"):
            self.set_state("Exposure", button)
        elif kind == "Iris" and button in ("Open1", "Close1"):
            if flags["iris"]:
                self._step("IrisIndex", 1 if button == "Open1" else -1, 0, len(IRIS) - 1)
        elif kind == "Gain" and button in ("Up1", "Down1"):
            if flags["gain"]:
                self._step("GainIndex", 1 if button == "Up1" else -1, 0, len(GAIN) - 1)
        elif kind == "Shutter" and button in ("Slower", "Faster"):
            if flags["shutter"]:
                self._step("ShutterIndex", -1 if button == "Slower" else 1, 0, len(SHUTTER) - 1)
        elif kind == "Bright" and button in ("Up", "Down"):
            if flags["bright"]:
                self._step("BrightIndex", 1 if button == "Up" else -1, 0, 17)
        elif kind == "Whb" and button in ("Auto", "Indoor", "Outdoor", "OnePush", "Manual"):
            self.set_state("WbMode", button)
            top = self._wb_scale()
            self.set_state("WhPR", min(int(s.get("WhPR", 0)), top))
            self.set_state("WhPB", min(int(s.get("WhPB", 0)), top))
        elif kind == "Whb" and button == "Adjust":
            pass  # One push white balance: nothing to model beyond the flag
        elif kind == "Whb" and button in ("WhPaintRP", "WhPaintRM", "WhPaintBP", "WhPaintBM"):
            key = "WhPR" if button.startswith("WhPaintR") else "WhPB"
            self._step(key, 1 if button.endswith("P") else -1, 0, self._wb_scale())
        elif kind == "Focus" and button in ("Auto", "Manual"):
            self._stop("focus")
            self.set_state("FocusMode", button)
        elif kind == "Focus" and re.fullmatch(r"(Far|Near)[123]", str(button)):
            if s.get("FocusMode") == "Manual":
                rate = FOCUS_RATE[int(button[-1])]
                lo, hi = LIMITS["focus"]
                self._move("focus", hi if button.startswith("Far") else lo, rate)
        elif kind == "Focus" and button in ("Stop", "PushAuto"):
            self._stop("focus")
        elif kind == "Zoom" and re.fullmatch(r"(Tele|Wide)[123]", str(button)):
            self._jog("zoom", 1 if button.startswith("Tele") else -1, int(button[-1]) * 2)
        elif kind == "Zoom" and button == "Stop":
            self._stop("zoom")
        elif kind == "Menu" and button in ("Menu", "Set", "Cancel", "Up", "Down", "Left", "Right"):
            if button == "Menu":
                self.set_state("MenuStatus", "Off" if s.get("MenuStatus") == "On" else "On")
            elif button == "Cancel":
                self.set_state("MenuStatus", "Off")
        elif kind in PICTURE_RANGES and button in ("Up", "Down"):
            lo, hi = PICTURE_RANGES[kind]
            self._step(kind, 1 if button == "Up" else -1, lo, hi)
        elif kind in ("Flip", "Mirror", "SpeedWithZoom") and button in ("On", "Off"):
            self.set_state(kind, button)
        else:
            raise _Refuse("RequestedError")
        return None

    def _step(self, key: str, delta: int, lo: int, hi: int) -> None:
        self.set_state(key, max(lo, min(hi, int(self.state.get(key, lo)) + delta)))

    def _cmd_SetWebXYFieldEvent(self, p: dict) -> None:
        x, y = _int(p.get("XPosition")), _int(p.get("YPosition"))
        top = self._wb_scale()
        if p.get("Kind") != "WhPaintRB" or x is None or y is None or not 0 <= x <= top or not 0 <= y <= top:
            raise _Refuse("RequestedError")
        self.set_state("WhPB", x)
        self.set_state("WhPR", y)
        return None

    # ── Streaming and tally ──

    def _cmd_SetStreamingCtrl(self, p: dict) -> None:
        streaming = p.get("Streaming")
        if streaming not in ("On", "Off"):
            raise _Refuse("RequestedError")
        if self.state.get("MenuStatus") == "On":
            return None  # the document: start / stop do not work while the menu is shown
        if streaming == "On" and self.state.get("Streaming") in ("Stop", "Error"):
            self.set_state("Streaming", "Starting")
            self._stream_change = ("Start", time.monotonic() + 2.0)
        elif streaming == "Off" and self.state.get("Streaming") in ("Start", "Starting", "Waiting"):
            self.set_state("Streaming", "Stopping")
            self._stream_change = ("Stop", time.monotonic() + 1.0)
        return None

    def _lamp(self, lit: bool) -> None:
        self.set_state("Lighting", "On" if lit and self.state.get("TallyMode") == "On" else "Off")

    def _cmd_SetTallyLampCtrl(self, p: dict) -> None:
        sw = p.get("Sw")
        if sw not in ("On", "Off"):
            raise _Refuse("RequestedError")
        self._lamp(sw == "On")
        return None

    def _cmd_SetStudioTally(self, p: dict) -> None:
        indication = p.get("Indication")
        if indication not in ("Off", "Program", "Preview"):
            raise _Refuse("RequestedError")
        self.set_state("StudioTally", indication)
        self._lamp(indication != "Off")
        return None

    # ── Auto tracking (KY-PZ510) ──

    def _cmd_SetTracking(self, p: dict) -> None:
        if p.get("Tracking") not in ("On", "Off"):
            raise _Refuse("RequestedError")
        self.set_state("Tracking", p["Tracking"])
        return None

    def _cmd_SetTrackingMode(self, p: dict) -> None:
        mode = p.get("TrackingMode")
        if mode not in ("Standard", "Area", "Stage", "WideArea", "FineAdjustment"):
            raise _Refuse("RequestedError")
        if self.state.get("Tracking") == "On":
            raise _Refuse("DisableError")
        self.set_state("TrackingMode", mode)
        return None

    def _cmd_SetTrackingBoundingBox(self, p: dict) -> None:
        if p.get("TrackingBoundingBox") not in ("Auto", "Off", "On"):
            raise _Refuse("RequestedError")
        self.set_state("BoundingBox", p["TrackingBoundingBox"])
        return None

    def _cmd_GetTrackingDetectedObjects(self, p: dict) -> dict:
        return {"ScreenWidth": 1920, "ScreenHeight": 1080, "Objects": [], "TargetObjectID": ""}

    def _cmd_SetTrackingTarget(self, p: dict) -> dict:
        return {"Result": "InvalidID"}

    def _tracking(self, command: str, p: dict) -> dict | None:
        m = re.fullmatch(r"GetTracking(Standard|Area|Stage|WideArea|FineAdjustment)", command)
        if m:
            data = {}
            for field in TRACKING_GETS[m.group(1)]:
                value = self.state.get(TRACKING_FIELDS[field])
                if field == "StartPosition" and m.group(1) == "Standard" and value in ("W1", "W2"):
                    value = "CurrentPosition"
                data[field] = value
            return data
        m = re.fullmatch(r"ExecuteTracking(StartingArea|StagePosition|WideAreaPosition)", command)
        if m:
            mode = {"StartingArea": "Area", "StagePosition": "Stage", "WideAreaPosition": "WideArea"}[m.group(1)]
            which = p.get("Area" if mode == "Area" else "Position")
            preset = TRACKING_PRESETS.get((mode, which))
            if preset is None or p.get("Command") not in ("Set", "Delete"):
                raise _Refuse("RequestedError")
            if p["Command"] == "Set":
                self._store(preset)
            else:
                self._presets.pop(preset, None)
            return None
        m = re.fullmatch(
            r"SetTracking(Standard|Area|Stage|WideArea|FineAdjustment)"
            r"(AutoZoom|AutoTilt|TargetRetentionTime|FigureSize|VerticalPosition|HorizontalPosition|StartPosition|StartingArea|DurationTime)",
            command,
        )
        if not m:
            raise _Refuse("CommandError")
        mode, field = m.groups()
        allowed = {
            "Standard": ("AutoZoom", "AutoTilt", "TargetRetentionTime", "FigureSize", "VerticalPosition", "HorizontalPosition", "StartPosition"),
            "Area": ("TargetRetentionTime", "StartingArea"),
            "Stage": ("TargetRetentionTime", "HorizontalPosition"),
            "WideArea": ("AutoZoom", "AutoTilt", "TargetRetentionTime", "FigureSize", "VerticalPosition", "HorizontalPosition", "StartPosition"),
            "FineAdjustment": ("AutoZoom", "AutoTilt", "DurationTime", "FigureSize", "VerticalPosition", "HorizontalPosition"),
        }[mode]
        if field not in allowed:
            raise _Refuse("CommandError")
        value = p.get(field)
        if field in ("AutoZoom", "AutoTilt"):
            ok = value in ("On", "Off")
        elif field == "TargetRetentionTime":
            ok = _int(value) in (5, 8, 10, 12, 15)
            value = _int(value)
        elif field == "VerticalPosition":
            ok = _int(value) is not None and -5 <= _int(value) <= 5
            value = _int(value)
        elif field == "DurationTime":
            ok = _int(value) is not None and 1 <= _int(value) <= 6
            value = _int(value)
        elif field == "HorizontalPosition":
            ok = value in ("Left", "Center", "Right")
        elif field == "StartPosition":
            ok = value in START_POSITIONS[mode]
        elif field == "StartingArea":
            ok = value in ("A1", "A2", "A3", "A4")
        else:  # FigureSize
            ok = value in ("Full", "Upper", "Close", "Custom")
            custom = p.get("FigureSizeCustom")
            if ok and value == "Custom" and custom is not None:
                size = _int(custom)
                if size is None or not 80 <= size <= 290:
                    raise _Refuse("RequestedError")
                self.set_state("FigureCustom", size)
        if not ok:
            raise _Refuse("RequestedError")
        self.set_state(TRACKING_FIELDS[field], value)
        return None


class _Refuse(Exception):
    def __init__(self, result: str) -> None:
        super().__init__(result)
        self.result = result
