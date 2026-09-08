"""
Axis Camera (VAPIX) — Simulator

Simulates an Axis camera's VAPIX surface: basic device information, API
discovery, param.cgi (the Properties, Brand, ImageSource, Image, Audio,
StreamProfile, IOPort and GuardTour groups), the optics control API, the
DayNight API, I/O port management plus the older port.cgi and virtual
inputs, light control, the dynamic overlay API, view areas, stream profiles,
the time API, firmware management (reboot), and the event WebSocket at
/vapix/ws-data-stream with its JSON-RPC configure/notify exchange.

By default it is a fixed dome with remote zoom and focus, an IR cut filter,
one input and one output port, one IR illuminator and audio, which is the
shape of the bench camera. ``ptz: True`` turns it into a PTZ model: ptz.cgi
and ptzconfig.cgi come alive with presets and a guard tour, movement
integrates continuous velocities over time, and optics control goes away as
it does on real PTZ cameras.

Authentication is off by default (``require_auth``), which is what the
connect-lifecycle smoke and a first look in the Simulator UI need. With it
on, every CGI except the unrestricted device information demands RFC 2617
HTTP Digest against the configured password, the WebSocket handshake
included, and ``auth_mode: "basic"`` models a camera whose authentication
policy is Basic only.

Driver: axis_vapix
Transport: http
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from openavc.simulator.http_simulator import HTTPSimulator

SERIAL = "B8A44F000001"
REALM = f"AXIS_{SERIAL}"
MANUAL_TRIGGER_NBR = 6

# Optics steps (magnification and focus) for the relative methods.
ZOOM_BIG_STEP = 0.2
ZOOM_SMALL_STEP = 0.05
FOCUS_BIG_STEP = 0.1
FOCUS_SMALL_STEP = 0.02

# PTZ movement: a continuous move at speed 100 covers this many degrees or
# zoom steps per second.
PAN_DEG_PER_S = 60.0
ZOOM_STEPS_PER_S = 2000.0
PAN_LIMITS = (-180.0, 180.0)
TILT_LIMITS = (-90.0, 90.0)
ZOOM_LIMITS = (1, 9999)

# param.cgi parameters mirrored into simulator state (parameter -> state key,
# coercion), so the Simulator UI's sliders and the driver read the same value.
PARAM_STATE: dict[str, tuple[str, str]] = {
    "ImageSource.I0.Sensor.Brightness": ("brightness", "int"),
    "ImageSource.I0.Sensor.Contrast": ("contrast", "int"),
    "ImageSource.I0.Sensor.ColorLevel": ("color_level", "int"),
    "ImageSource.I0.Sensor.Sharpness": ("sharpness", "int"),
    "ImageSource.I0.Sensor.WDR": ("wdr", "onoff"),
    "ImageSource.I0.Sensor.WDRLevel": ("wdr_level", "int"),
    "ImageSource.I0.Sensor.LocalContrast": ("local_contrast", "int"),
    "ImageSource.I0.Sensor.Exposure": ("exposure_mode", "str"),
    "ImageSource.I0.Sensor.ExposureValue": ("exposure_value", "int"),
    "ImageSource.I0.Sensor.ExposureWindow": ("exposure_window", "str"),
    "ImageSource.I0.Sensor.ExposurePriority": ("exposure_priority", "int"),
    "ImageSource.I0.Sensor.MaxGain": ("max_gain", "int"),
    "ImageSource.I0.Sensor.WhiteBalance": ("white_balance", "str"),
    "ImageSource.I0.Sensor.BacklightCompensation": ("backlight_compensation", "yesno"),
    "ImageSource.I0.Sensor.Defog": ("defog", "str"),
    "ImageSource.I0.Sensor.DefogEffect": ("defog_effect", "int"),
    "ImageSource.I0.Rotation": ("rotation", "int"),
    "ImageSource.I0.DayNight.IrCutFilter": ("ir_cut_filter_param", "str"),
    "Image.I0.Appearance.MirrorEnabled": ("mirror", "yesno"),
    "Image.I0.Appearance.Overlays": ("overlays_shown", "str"),
    "Audio.A0.Enabled": ("audio_enabled", "yesno"),
    "AudioSource.A0.InputGain": ("audio_input_gain", "str"),
    "AudioSource.A0.OutputGain": ("audio_output_gain", "str"),
    "GuardTour.G0.Running": ("guard_tour_running", "yesno"),
}

SENSOR_INT_PARAMS = {
    "Brightness", "Contrast", "ColorLevel", "Sharpness", "WDRLevel", "LocalContrast",
    "ExposureValue", "ExposurePriority", "MaxGain", "DefogEffect",
}
SENSOR_ENUMS = {
    "WDR": ("on", "off"),
    "Exposure": ("auto", "flickerfree50", "flickerfree60", "hold"),
    "ExposureWindow": ("auto", "right", "left", "upper", "lower", "spot", "custom"),
    "WhiteBalance": ("auto", "auto_indoor", "auto_outdoor", "hold", "manual", "fixed_outdoor1",
                     "fixed_outdoor2", "fixed_indoor", "fixed_fluor1", "fixed_fluor2"),
    "BacklightCompensation": ("yes", "no"),
    "Defog": ("off", "on", "auto"),
}

OVERLAY_POSITIONS = ("top", "topRight", "bottomRight", "bottom", "bottomLeft", "topLeft")
OVERLAY_COLORS = ("black", "white", "red", "transparent", "semiTransparent")
OVERLAY_SLOTS = 8


def _json_ok(method: str, data: Any, api_version: str = "1.0", context: str = "") -> tuple[int, str]:
    body: dict[str, Any] = {"apiVersion": api_version, "method": method, "data": data}
    if context:
        body["context"] = context
    return 200, json.dumps(body)


def _json_error(method: str, code: int, message: str, api_version: str = "1.0",
                context: str = "", status: int = 200, details: Any = None) -> tuple[int, str]:
    err: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        err["details"] = details
    body: dict[str, Any] = {"apiVersion": api_version, "method": method, "error": err}
    if context:
        body["context"] = context
    return status, json.dumps(body)


def _yesno(value: Any) -> str:
    return "yes" if value else "no"


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _fmt_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _WsClient:
    """One event-stream session: the socket (anything with an async ``send``)
    and the topic filters it configured."""

    def __init__(self, socket: Any):
        self.socket = socket
        self.filters: list[str] = []
        self.configured = False

    def wants(self, topic: str) -> bool:
        if not self.configured:
            return False
        for expr in self.filters:
            if not expr:
                return True
            for alt in expr.split("|"):
                prefix = alt.strip()
                if prefix.endswith("//."):
                    prefix = prefix[:-3]
                if topic == prefix or topic.startswith(prefix + "/"):
                    return True
        return False


class AxisVapixSimulator(HTTPSimulator):
    """An Axis camera over VAPIX, fixed dome by default, PTZ on request."""

    SIMULATOR_INFO = {
        "driver_id": "axis_vapix",
        "name": "Axis Camera (VAPIX) Simulator",
        "category": "camera",
        "transport": "http",
        "default_port": 80,
        "initial_state": {
            "model": "P3265-V",
            "product_name": "AXIS P3265-V Dome Camera",
            "firmware_version": "10.12.165",
            "serial_number": SERIAL,
            "magnification": 1.0,
            "focus_position": 0.5,
            "zoom_moving": False,
            "focus_moving": False,
            "ir_cut_filter": "auto",
            "ir_cut_filter_param": "auto",
            "day_mode": True,
            "brightness": 50,
            "contrast": 50,
            "color_level": 50,
            "sharpness": 50,
            "wdr": True,
            "wdr_level": 50,
            "local_contrast": 50,
            "exposure_mode": "auto",
            "exposure_value": 50,
            "exposure_window": "auto",
            "exposure_priority": 50,
            "max_gain": 100,
            "white_balance": "auto",
            "backlight_compensation": False,
            "defog": "off",
            "defog_effect": 0,
            "rotation": 0,
            "mirror": False,
            "overlays_shown": "all",
            "audio_enabled": False,
            "audio_input_gain": "0",
            "audio_output_gain": "0",
            "input_0": False,
            "output_1": False,
            "manual_trigger": False,
            "motion": False,
            "tamper": False,
            "stream_accessed": False,
            "system_ready": True,
            "light_on": False,
            "light_intensity": 50,
            "light_auto": False,
            "pan": 0.0,
            "tilt": 0.0,
            "zoom": 1,
            "autofocus": True,
            "guard_tour_running": False,
            "overlay_count": 0,
            "rebooted": False,
            "ws_clients": 0,
        },
        "controls": [
            {"type": "toggle", "key": "input_0", "label": "Digital Input (port 0)"},
            {"type": "indicator", "key": "output_1", "label": "Output (port 1)"},
            {"type": "toggle", "key": "day_mode", "label": "Day Mode"},
            {"type": "toggle", "key": "motion", "label": "Motion (Object Analytics)"},
            {"type": "toggle", "key": "tamper", "label": "Tampering (pulse)"},
            {"type": "toggle", "key": "stream_accessed", "label": "Live Stream Accessed"},
            {"type": "toggle", "key": "manual_trigger", "label": "Manual Trigger"},
            {"type": "slider", "key": "magnification", "min": 1, "max": 2.4, "step": 0.1, "label": "Magnification"},
            {"type": "slider", "key": "focus_position", "min": 0, "max": 1, "step": 0.01, "label": "Focus"},
            {"type": "slider", "key": "brightness", "min": 0, "max": 100, "label": "Brightness"},
            {"type": "slider", "key": "pan", "min": -180, "max": 180, "label": "Pan (PTZ model)"},
            {"type": "slider", "key": "tilt", "min": -90, "max": 90, "label": "Tilt (PTZ model)"},
            {"type": "slider", "key": "zoom", "min": 1, "max": 9999, "label": "Zoom (PTZ model)"},
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        cfg = self.config
        self._require_auth = bool(cfg.get("require_auth", False))
        self._auth_mode = str(cfg.get("auth_mode", "digest"))
        self._password = str(cfg.get("password", "secret"))
        self._ptz = bool(cfg.get("ptz", False))          # a PTZ model: mechanical, always on
        self._optics = bool(cfg.get("optics", True)) and not self._ptz
        self._lights = bool(cfg.get("lights", True))
        self._audio = bool(cfg.get("audio", True))
        self._events = bool(cfg.get("events", True))
        self._legacy = bool(cfg.get("legacy", False))
        self._daynight_api = bool(cfg.get("daynight_api", True))
        self._sd_card = bool(cfg.get("sd_card", False))
        self._clock_skew = timedelta(seconds=float(cfg.get("clock_skew_s", 0) or 0))
        self._max_magnification = float(cfg.get("max_magnification", 2.4) or 2.4)
        self._nonces: set[str] = set()
        self._tokens: dict[str, float] = {}
        self._ws_clients: list[_WsClient] = []
        self._params = self._initial_params()
        self._daynight = {
            "DayNightDwellTime": 3, "DayNightShiftLevel": 50, "NightDayDwellTime": 3,
            "NightDayShiftLevel": 50, "Autotune": True, "NightFilter": "clear",
        }
        self._ports: dict[str, dict[str, Any]] = {
            "0": {"port": "0", "state": "open", "configurable": True, "usage": "", "direction": "input",
                  "name": "Input 1", "normalState": "open"},
            "1": {"port": "1", "state": "open", "configurable": True, "usage": "", "direction": "output",
                  "name": "Output 1", "normalState": "open"},
        }
        self._sequence_tasks: dict[str, asyncio.Task] = {}
        self._virtual_inputs: dict[int, bool] = {}
        self._overlays: dict[int, dict[str, Any]] = {}
        self._image_files = ["/etc/overlays/axis(128x44).ovl", "/etc/overlays/logo.ovl"]
        self._presets: dict[str, dict[str, Any]] = {
            "1": {"name": "Home", "pan": 0.0, "tilt": 0.0, "zoom": 1},
            "2": {"name": "Lectern", "pan": 30.0, "tilt": -10.0, "zoom": 2500},
        }
        self._home = "1"
        self._velocity = [0.0, 0.0, 0.0]   # pan, tilt, zoom
        self._focus_velocity = 0.0
        self._motion_task: asyncio.Task | None = None
        self._last_tick: float | None = None
        self._light_enabled = True
        self.calls: list[str] = []
        self.configure_count = 0

    # ── Parameter store ──

    def _initial_params(self) -> dict[str, str]:
        p: dict[str, str] = {
            "Properties.API.HTTP.Version": "3",
            "Properties.ApiDiscovery.ApiDiscovery": "yes",
            "Properties.Firmware.Version": "10.12.165",
            "Properties.System.SerialNumber": SERIAL,
            "Properties.Image.Rotation": "0,90,180,270",
            "Properties.Image.Format": "jpeg,mjpeg,h264,h265",
            "Properties.Image.NbrOfViews": "2",
            # A fixed dome has digital PTZ that ships turned off; a PTZ model
            # is mechanical and always on.
            "Properties.PTZ.PTZ": "yes",
            "Properties.PTZ.DigitalPTZ": _yesno(not self._ptz),
            "PTZ.ImageSource.I0.PTZEnabled": "true" if self._ptz else "false",
            "Properties.Audio.Audio": _yesno(self._audio),
            "Properties.DynamicOverlay.DynamicOverlay": "yes",
            "Properties.DynamicOverlay.Version": "1.00",
            "Properties.LightControl.LightControl2": _yesno(self._lights),
            "Properties.GuardTour.GuardTour": _yesno(self._ptz),
            "Brand.Brand": "AXIS",
            "Brand.ProdFullName": "AXIS P3265-V Dome Camera",
            "Brand.ProdNbr": "P3265-V",
            "Brand.ProdShortName": "AXIS P3265-V",
            "Brand.ProdType": "Dome Camera",
            "Image.NbrOfConfigs": "2",
            "Image.I0.Enabled": "yes",
            "Image.I1.Enabled": "no",
            "Image.MaxViewers": "20",
            "Image.I0.Appearance.Resolution": "1920x1080",
            "Image.I0.Appearance.Compression": "30",
            "Image.I0.Appearance.MirrorEnabled": "no",
            "Image.I0.Appearance.Overlays": "all",
            "Image.I0.Appearance.Rotation": "0",
            "Image.I0.Stream.FPS": "30",
            "Image.I1.Appearance.Resolution": "640x360",
            "ImageSource.I0.Name": "View Area 1",
            "ImageSource.I0.SourceRotation": "yes",
            "ImageSource.I0.Rotation": "0",
            "ImageSource.I0.DayNight.IrCutFilter": "auto",
            "ImageSource.I0.DayNight.ShiftLevel": "50",
            "ImageSource.I0.Sensor.Brightness": "50",
            "ImageSource.I0.Sensor.Contrast": "50",
            "ImageSource.I0.Sensor.ColorLevel": "50",
            "ImageSource.I0.Sensor.Sharpness": "50",
            "ImageSource.I0.Sensor.WDR": "on",
            "ImageSource.I0.Sensor.WDRLevel": "50",
            "ImageSource.I0.Sensor.LocalContrast": "50",
            "ImageSource.I0.Sensor.Exposure": "auto",
            "ImageSource.I0.Sensor.ExposureValue": "50",
            "ImageSource.I0.Sensor.ExposureWindow": "auto",
            "ImageSource.I0.Sensor.ExposurePriority": "50",
            "ImageSource.I0.Sensor.MaxGain": "100",
            "ImageSource.I0.Sensor.WhiteBalance": "auto",
            "ImageSource.I0.Sensor.BacklightCompensation": "no",
            "ImageSource.I0.Sensor.Defog": "off",
            "ImageSource.I0.Sensor.DefogEffect": "0",
            "StreamProfile.S0.Name": "Quality",
            "StreamProfile.S0.Description": "Best quality",
            "StreamProfile.S0.Parameters": "videocodec%3dh264%26resolution%3d1920x1080",
            "StreamProfile.S1.Name": "Bandwidth",
            "StreamProfile.S1.Description": "Low bandwidth",
            "StreamProfile.S1.Parameters": "videocodec%3dh264%26resolution%3d640x360",
            "IOPort.I0.Configurable": "yes",
            "IOPort.I0.Direction": "input",
            "IOPort.I0.Input.Name": "Input 1",
            "IOPort.I0.Input.Trig": "closed",
            "IOPort.I1.Configurable": "yes",
            "IOPort.I1.Direction": "output",
            "IOPort.I1.Output.Name": "Output 1",
            "IOPort.I1.Output.Active": "closed",
            "IOPort.ManualTriggerNbr": str(MANUAL_TRIGGER_NBR),
            "Input.NbrOfInputs": "1",
            "Output.NbrOfOutputs": "1",
        }
        if self._audio:
            p.update({
                "Audio.A0.Enabled": "no",
                "Audio.A0.Name": "Audio",
                "Audio.A0.Source": "0",
                "Audio.NbrOfConfigs": "1",
                "AudioSource.A0.Name": "Audio",
                "AudioSource.A0.AudioSupport": "yes",
                "AudioSource.A0.AudioEncoding": "aac",
                "AudioSource.A0.InputType": "mic",
                "AudioSource.A0.InputGain": "0",
                "AudioSource.A0.OutputGain": "0",
            })
        if True:
            p.update({
                "PTZ.Support.S1.AbsolutePan": "true",
                "PTZ.Support.S1.AbsoluteTilt": "true",
                "PTZ.Support.S1.AbsoluteZoom": "true",
                "PTZ.Support.S1.ContinuousPan": "true",
                "PTZ.Support.S1.ContinuousTilt": "true",
                "PTZ.Support.S1.ContinuousZoom": "true",
                "PTZ.Support.S1.ContinuousFocus": _yesno(self._ptz).replace("yes", "true").replace("no", "false"),
                "PTZ.Support.S1.AutoFocus": _yesno(self._ptz).replace("yes", "true").replace("no", "false"),
                "PTZ.Support.S1.IrCutFilter": _yesno(self._ptz).replace("yes", "true").replace("no", "false"),
                "PTZ.Support.S1.AutoIrCutFilter": _yesno(self._ptz).replace("yes", "true").replace("no", "false"),
                "PTZ.Support.S1.ServerPreset": "true",
                "PTZ.Support.S1.AreaZoom": "true",
                "PTZ.Various.V1.IrCutFilter": "auto",
                "PTZ.Various.V1.Locked": "true" if not self._ptz else "false",
                "PTZ.Various.V1.PanEnabled": "true",
                "PTZ.Various.V1.TiltEnabled": "true",
                "PTZ.Various.V1.ZoomEnabled": "true",
            })
        if self._ptz:
            p.update({
                "GuardTour.G0.Name": "Lobby sweep",
                "GuardTour.G0.CamNbr": "1",
                "GuardTour.G0.Running": "no",
                "GuardTour.G0.RandomEnabled": "no",
                "GuardTour.G0.TimeBetweenSequences": "0",
                "GuardTour.G0.Tour.T0.PresetNbr": "1",
                "GuardTour.G0.Tour.T0.Position": "1",
                "GuardTour.G0.Tour.T0.WaitTime": "10",
                "GuardTour.G0.Tour.T1.PresetNbr": "2",
                "GuardTour.G0.Tour.T1.Position": "2",
                "GuardTour.G0.Tour.T1.WaitTime": "10",
            })
        return p

    def _param(self, name: str) -> str:
        return self._params.get(name, "")

    def _ptz_on(self) -> bool:
        return self._param("PTZ.ImageSource.I0.PTZEnabled") == "true"

    def _set_param(self, name: str, value: str) -> bool:
        """Validate and store a parameter, mirroring it into state. False when
        the camera would refuse the value."""
        m = re.match(r"ImageSource\.I\d+\.Sensor\.(\w+)$", name)
        if m:
            field = m.group(1)
            if field in SENSOR_INT_PARAMS:
                if not value.lstrip("-").isdigit() or not (0 <= int(value) <= 100):
                    return False
            elif field in SENSOR_ENUMS and value not in SENSOR_ENUMS[field]:
                return False
        if name.endswith(".Rotation") and value not in ("0", "90", "180", "270"):
            return False
        if name.endswith(".DayNight.IrCutFilter"):
            if value not in ("yes", "no", "auto"):
                return False
            self._apply_ir_cut({"yes": "on", "no": "off"}.get(value, value))
        if name.endswith(".MirrorEnabled") and value not in ("yes", "no"):
            return False
        if name.endswith(".Appearance.Overlays") and value not in (
            "off", "all", "all-sync", "image", "text", "application", "application-sync",
        ):
            return False
        if name.endswith(".Enabled") and name.startswith("Audio.") and value not in ("yes", "no"):
            return False
        if name == "PTZ.ImageSource.I0.PTZEnabled" and value not in ("true", "false"):
            return False
        if name == "PTZ.Various.V1.Locked" and value not in ("true", "false"):
            return False
        if name.startswith("GuardTour.") and name.endswith(".Running"):
            if value not in ("yes", "no"):
                return False
            if value == "yes":
                first = self._presets.get(self._param(name.replace("Running", "Tour.T0.PresetNbr")))
                if first:
                    self._goto(first)
        if name.startswith("ImageSource.I0.Rotation") or name == "Image.I0.Appearance.Rotation":
            # Source rotation keeps both names in step, as the camera does.
            self._params["ImageSource.I0.Rotation"] = value
            self._params["Image.I0.Appearance.Rotation"] = value
        self._params[name] = value
        mirror = PARAM_STATE.get(name)
        if mirror:
            key, kind = mirror
            if kind == "int":
                self.set_state(key, int(value))
            elif kind in ("onoff", "yesno"):
                self.set_state(key, value in ("on", "yes"))
            else:
                self.set_state(key, value)
        return True

    # ── Time, movement ──

    def _now(self) -> datetime:
        return datetime.now(timezone.utc) + self._clock_skew

    def tick(self, dt: float) -> None:
        """Advance PTZ movement by ``dt`` seconds (the motion task calls
        this; a test calls it directly)."""
        if not self._ptz_on():
            return
        vp, vt, vz = self._velocity
        if vp or vt or vz:
            pan = _clamp(float(self.get_state("pan", 0.0)) + vp / 100.0 * PAN_DEG_PER_S * dt, *PAN_LIMITS)
            tilt = _clamp(float(self.get_state("tilt", 0.0)) + vt / 100.0 * PAN_DEG_PER_S * dt, *TILT_LIMITS)
            zoom = int(_clamp(int(self.get_state("zoom", 1)) + vz / 100.0 * ZOOM_STEPS_PER_S * dt, *ZOOM_LIMITS))
            self.set_state("pan", round(pan, 2))
            self.set_state("tilt", round(tilt, 2))
            self.set_state("zoom", zoom)

    async def _motion_loop(self) -> None:
        while True:
            await asyncio.sleep(0.1)
            now = time.monotonic()
            if self._last_tick is not None:
                self.tick(now - self._last_tick)
            self._last_tick = now

    async def start(self, port: int) -> None:
        await self.start_http_server(port)
        self._motion_task = asyncio.create_task(self._motion_loop())

    async def stop(self) -> None:
        task, self._motion_task = self._motion_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for seq in list(self._sequence_tasks.values()):
            seq.cancel()
        self._sequence_tasks.clear()
        for client in list(self._ws_clients):
            try:
                await client.socket.close()
            except Exception:
                pass
        self._ws_clients.clear()
        await self.stop_http_server()

    def _goto(self, preset: dict[str, Any]) -> None:
        self._velocity = [0.0, 0.0, 0.0]
        self.set_state("pan", float(preset["pan"]))
        self.set_state("tilt", float(preset["tilt"]))
        self.set_state("zoom", int(preset["zoom"]))

    # ── State changes that are events ──

    def set_state(self, key: str, value: Any) -> None:
        old = self.get_state(key)
        super().set_state(key, value)
        if old == value and key != "tamper":
            return
        if key == "input_0":
            self._emit("tns1:Device/tnsaxis:IO/tnsaxis:Port", {"port": "0"}, {"state": "1" if value else "0"})
        elif key == "output_1":
            self._emit("tns1:Device/tnsaxis:IO/tnsaxis:OutputPort", {"port": "1"},
                       {"state": "high" if value else "low"})
        elif key == "day_mode":
            self._emit("tns1:VideoSource/tnsaxis:DayNightVision", {"VideoSourceConfigurationToken": "0"},
                       {"day": "1" if value else "0"})
        elif key == "motion":
            self._emit("tns1:CameraApplicationPlatform/tnsaxis:VMD/tnsaxis:Camera1ProfileANY", {},
                       {"active": "1" if value else "0"})
        elif key == "tamper" and value:
            self._emit("tns1:VideoSource/tnsaxis:Tampering", {"channel": "1"}, {"tampering": "1"})
            super().set_state("tamper", False)
        elif key == "stream_accessed":
            self._emit("tns1:VideoSource/tnsaxis:LiveStreamAccessed", {}, {"accessed": "1" if value else "0"})
        elif key == "manual_trigger":
            self._emit("tns1:Device/tnsaxis:IO/tnsaxis:VirtualPort", {"port": "1"},
                       {"state": "1" if value else "0"})
        elif key == "system_ready":
            self._emit("tns1:Device/tnsaxis:Status/tnsaxis:SystemReady", {}, {"ready": "1" if value else "0"})

    def _stateful_events(self) -> list[tuple[str, dict[str, str], dict[str, str]]]:
        """Every stateful event with its current state, replayed on subscribe."""
        out: list[tuple[str, dict[str, str], dict[str, str]]] = []
        for port_id, item in self._ports.items():
            active = item["state"] != item["normalState"]
            if item["direction"] == "input":
                out.append(("tns1:Device/tnsaxis:IO/tnsaxis:Port", {"port": port_id}, {"state": "1" if active else "0"}))
            else:
                out.append(("tns1:Device/tnsaxis:IO/tnsaxis:OutputPort", {"port": port_id},
                            {"state": "high" if active else "low"}))
        out.append(("tns1:Device/tnsaxis:IO/tnsaxis:VirtualPort", {"port": "1"},
                    {"state": "1" if self.get_state("manual_trigger") else "0"}))
        out.append(("tns1:VideoSource/tnsaxis:DayNightVision", {"VideoSourceConfigurationToken": "0"},
                    {"day": "1" if self.get_state("day_mode") else "0"}))
        out.append(("tns1:VideoSource/tnsaxis:LiveStreamAccessed", {},
                    {"accessed": "1" if self.get_state("stream_accessed") else "0"}))
        out.append(("tns1:Device/tnsaxis:Status/tnsaxis:SystemReady", {},
                    {"ready": "1" if self.get_state("system_ready") else "0"}))
        out.append(("tns1:CameraApplicationPlatform/tnsaxis:VMD/tnsaxis:Camera1ProfileANY", {},
                    {"active": "1" if self.get_state("motion") else "0"}))
        out.append(("tns1:PTZController/tnsaxis:PTZReady", {"channel": "1"},
                    {"ready": "1" if self._ptz_on() else "0"}))
        out.append(("tns1:PTZController/tnsaxis:PTZReady", {"channel": "2"}, {"ready": "0"}))
        # The camera reports storage per disk; an empty card slot is "disrupted".
        out.append(("tns1:Device/tnsaxis:HardwareFailure/tnsaxis:StorageFailure", {"disk_id": "SD_DISK"},
                    {"disruption": "0" if self._sd_card else "1"}))
        out.append(("tns1:Device/tnsaxis:HardwareFailure/tnsaxis:StorageFailure", {"disk_id": "NetworkShare"},
                    {"disruption": "1"}))
        out.append(("tns1:VideoSource/GlobalSceneChange/ImagingService", {"Source": "0"}, {"State": "0"}))
        return out

    def _frame(self, topic: str, source: dict[str, str], data: dict[str, str]) -> str:
        return json.dumps({
            "apiVersion": "1.0",
            "method": "events:notify",
            "params": {"notification": {
                "timestamp": int(self._now().timestamp() * 1000),
                "topic": topic,
                "message": {"source": source, "key": {}, "data": data},
            }},
        })

    def _emit(self, topic: str, source: dict[str, str], data: dict[str, str]) -> None:
        if not self._ws_clients:
            return
        frame = self._frame(topic, source, data)
        for client in list(self._ws_clients):
            if client.wants(topic):
                self._send_later(client, frame)

    def _send_later(self, client: _WsClient, text: str) -> None:
        async def _send() -> None:
            try:
                await client.socket.send(text)
            except Exception:
                self._drop_client(client)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        task = asyncio.ensure_future(_send())
        self._pending_sends = [t for t in getattr(self, "_pending_sends", []) if not t.done()] + [task]

    def _drop_client(self, client: _WsClient) -> None:
        if client in self._ws_clients:
            self._ws_clients.remove(client)
            super().set_state("ws_clients", len(self._ws_clients))

    # ── Event WebSocket (JSON-RPC over /vapix/ws-data-stream) ──

    def ws_authorized(self, path: str, headers: dict[str, str]) -> bool:
        """Digest on the handshake, or a fresh wssession token in the query."""
        if not self._require_auth:
            return True
        query = dict(parse_qsl(urlsplit(path).query))
        token = query.get("wssession", "")
        if token and self._tokens.get(token, 0) > time.monotonic():
            return True
        return self._digest_ok("GET", path, headers)

    def ws_open(self, socket: Any) -> _WsClient:
        client = _WsClient(socket)
        self._ws_clients.append(client)
        super().set_state("ws_clients", len(self._ws_clients))
        return client

    def ws_close(self, client: _WsClient) -> None:
        self._drop_client(client)

    async def ws_message(self, client: _WsClient, text: str) -> str:
        """Answer one client frame (events:configure). Notifications go out
        through ``_emit``; the stateful replay follows a configure at once."""
        try:
            req = json.loads(text)
        except (TypeError, ValueError):
            return json.dumps({"apiVersion": "1.0", "method": "", "error": {"code": 2101, "message": "Invalid JSON."}})
        method = str(req.get("method", ""))
        context = str(req.get("context", ""))
        if method != "events:configure":
            return _json_error(method, 2102, "Method not supported.", context=context)[1]
        filters = (req.get("params") or {}).get("eventFilterList")
        if not isinstance(filters, list):
            return _json_error(method, 2103, "Required parameter missing.", context=context)[1]
        client.filters = [str(f.get("topicFilter", "")) for f in filters if isinstance(f, dict)]
        client.configured = True
        self.configure_count += 1
        reply = _json_ok(method, {}, context=context)[1]
        for topic, source, data in self._stateful_events():
            if client.wants(topic):
                self._send_later(client, self._frame(topic, source, data))
        return reply

    async def _handle_http_request(self, request):
        """Catch the WebSocket upgrade before the HTTP preamble reads a body."""
        from aiohttp import WSMsgType, web

        path = "/" + request.match_info.get("path", "")
        if request.query_string:
            path += "?" + request.query_string
        if (
            request.headers.get("Upgrade", "").lower() == "websocket"
            and path.split("?")[0] == "/vapix/ws-data-stream"
        ):
            headers = dict(request.headers)
            if not self.ws_authorized(path, headers):
                status, body, extra = self._challenge()
                return web.Response(status=status, text=body, headers=extra)
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            client = self.ws_open(ws)
            self.log_protocol("in", f"WS open {path}")
            try:
                async for msg in ws:
                    if msg.type == WSMsgType.TEXT:
                        self.log_protocol("in", msg.data)
                        reply = await self.ws_message(client, msg.data)
                        self.log_protocol("out", reply)
                        await ws.send_str(reply)
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                        break
            finally:
                self.ws_close(client)
            return ws
        return await super()._handle_http_request(request)

    # ── Authentication ──

    def _challenge(self) -> tuple[int, str, dict[str, str]]:
        if self._auth_mode == "basic":
            return 401, "Unauthorized", {"WWW-Authenticate": f'Basic realm="{REALM}"'}
        nonce = secrets.token_hex(16)
        self._nonces.add(nonce)
        return 401, "Unauthorized", {
            "WWW-Authenticate": f'Digest realm="{REALM}", nonce="{nonce}", algorithm=MD5, qop="auth"',
        }

    def _digest_ok(self, method: str, path: str, headers: dict[str, str]) -> bool:
        auth = ""
        for k, v in headers.items():
            if k.lower() == "authorization":
                auth = v
        if self._auth_mode == "basic":
            if not auth.startswith("Basic "):
                return False
            import base64
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return False
            return decoded.split(":", 1)[-1] == self._password
        if not auth.startswith("Digest "):
            return False
        fields = {k.lower(): v.strip('"') for k, v in re.findall(r'(\w+)=("[^"]*"|[^,\s]+)', auth[7:])}
        nonce = fields.get("nonce", "")
        if nonce not in self._nonces:
            return False
        ha1 = hashlib.md5(f"{fields.get('username', '')}:{fields.get('realm', '')}:{self._password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{fields.get('uri', '')}".encode()).hexdigest()
        if fields.get("qop"):
            expected = hashlib.md5(
                f"{ha1}:{nonce}:{fields.get('nc', '')}:{fields.get('cnonce', '')}:{fields.get('qop', '')}:{ha2}".encode()
            ).hexdigest()
        else:
            expected = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        return fields.get("response", "") == expected

    # ── HTTP dispatch ──

    def handle_request(self, method: str, path: str, headers: dict[str, str], body: str):
        parts = urlsplit(path)
        route = parts.path
        query = {k: v for k, v in parse_qsl(parts.query, keep_blank_values=True)}
        # Every CGI is authenticated except the unrestricted device information.
        anonymous = route == "/axis-cgi/basicdeviceinfo.cgi" and '"getAllUnrestrictedProperties"' in body.replace(" ", "")
        if self._require_auth and not anonymous and not self._digest_ok(method, path, headers):
            return self._challenge()
        self.calls.append(route)
        if route == "/axis-cgi/basicdeviceinfo.cgi":
            return self._device_info(body)
        if route == "/axis-cgi/apidiscovery.cgi":
            return self._api_discovery(body)
        if route == "/axis-cgi/param.cgi":
            return self._param_cgi(query)
        if route == "/axis-cgi/opticscontrol.cgi":
            return self._optics_cgi(body)
        if route == "/axis-cgi/daynight.cgi":
            return self._daynight_cgi(body)
        if route == "/axis-cgi/io/portmanagement.cgi":
            return self._port_mgmt_cgi(body)
        if route == "/axis-cgi/io/port.cgi":
            return self._port_cgi(query)
        if route == "/axis-cgi/io/virtualinput.cgi":
            return self._virtual_input_cgi(query)
        if route == "/axis-cgi/lightcontrol.cgi":
            return self._light_cgi(body)
        if route == "/axis-cgi/dynamicoverlay/dynamicoverlay.cgi":
            return self._overlay_cgi(body)
        if route == "/axis-cgi/viewarea/info.cgi":
            return self._view_area_cgi(body)
        if route == "/axis-cgi/streamprofile.cgi":
            return self._stream_profile_cgi(body)
        if route == "/axis-cgi/time.cgi":
            return self._time_cgi(body)
        if route == "/axis-cgi/firmwaremanagement.cgi":
            return self._firmware_cgi(body)
        if route == "/axis-cgi/restart.cgi":
            self.set_state("rebooted", True)
            return 200, "<html><body>Restarting</body></html>"
        if route == "/axis-cgi/wssession.cgi":
            token = str(secrets.randbelow(10**19))
            self._tokens[token] = time.monotonic() + 15.0
            return 200, token
        if route == "/axis-cgi/com/ptz.cgi":
            return self._ptz_cgi(query)
        if route == "/axis-cgi/com/ptzconfig.cgi":
            return self._ptz_config_cgi(query)
        if route == "/axis-cgi/jpg/image.cgi":
            return 200, "JPEG"
        return 404, "Not Found"

    @staticmethod
    def _parse_json(body: str) -> tuple[dict[str, Any], str, str, dict[str, Any]]:
        try:
            req = json.loads(body) if body else {}
        except ValueError:
            req = {}
        if not isinstance(req, dict):
            req = {}
        return req, str(req.get("method", "")), str(req.get("context", "")), dict(req.get("params") or {})

    # ── basicdeviceinfo.cgi ──

    def _properties(self) -> dict[str, str]:
        return {
            "Architecture": "aarch64",
            "Brand": "AXIS",
            "BuildDate": "Mar 29 2023 19:56",
            "HardwareID": "931.11",
            "ProdFullName": str(self.get_state("product_name")),
            "ProdNbr": str(self.get_state("model")),
            "ProdShortName": f"AXIS {self.get_state('model')}",
            "ProdType": "Dome Camera",
            "ProdVariant": "",
            "SerialNumber": str(self.get_state("serial_number")),
            "Soc": "Axis Artpec-8",
            "SocSerialNumber": "00000000-00000000-00000000-00000001",
            "Version": str(self.get_state("firmware_version")),
            "WebURL": "http://www.axis.com",
        }

    def _device_info(self, body: str):
        if self._legacy:
            return 404, "Not Found"
        _, method, context, params = self._parse_json(body)
        props = self._properties()
        if method == "getAllProperties":
            return _json_ok(method, {"propertyList": props}, "1.3", context)
        if method == "getAllUnrestrictedProperties":
            keys = ("ProdNbr", "HardwareID", "ProdFullName", "Version", "ProdType", "Brand", "WebURL",
                    "ProdVariant", "SerialNumber", "ProdShortName", "BuildDate")
            return _json_ok(method, {"propertyList": {k: props[k] for k in keys}}, "1.3", context)
        if method == "getProperties":
            wanted = params.get("propertyList") or []
            missing = [k for k in wanted if k not in props]
            if missing:
                return _json_error(method, 2104, "Invalid parameter value specified", "1.3", context,
                                   details={"propertyList": missing})
            return _json_ok(method, {"propertyList": {k: props[k] for k in wanted}}, "1.3", context)
        if method == "getSupportedVersions":
            return _json_ok(method, {"apiVersions": ["1.3"]}, "1.3", context)
        return _json_error(method, 2102, "Method not supported", "1.3", context)

    # ── apidiscovery.cgi ──

    def _api_ids(self) -> list[str]:
        ids = ["api-discovery", "basic-device-info", "param-cgi", "view-area", "stream-profiles",
               "time-service", "fwmgr", "io-port-management", "light-control", "ptz-control"]
        if self._daynight_api:
            ids.append("daynight")
        if self._events:
            ids.append("event-streaming-over-websocket")
        if self._optics:
            ids.append("optics-control")
        if self._ptz:
            ids.append("guard-tour")
        return ids

    def _api_discovery(self, body: str):
        if self._legacy:
            return 404, "Not Found"
        _, method, context, params = self._parse_json(body)
        if method == "getSupportedVersions":
            return 200, json.dumps({"method": method, "data": {"apiVersions": ["1.0"]}})
        if method != "getApiList":
            return _json_error(method, 2102, "Method not supported", "1.0", context)
        wanted = str(params.get("id", "*"))
        entries = [
            {"id": api_id, "version": "1.0", "status": "released",
             "docLink": f"https://developer.axis.com/vapix/{api_id}", "name": api_id.replace("-", " ").title()}
            for api_id in self._api_ids()
            if wanted in ("*", api_id)
        ]
        return _json_ok(method, {"apiList": entries}, "1.0", context)

    # ── param.cgi ──

    def _param_cgi(self, query: dict[str, str]):
        action = query.get("action", "")
        if action == "list":
            group = query.get("group", "")
            if not group:
                lines = [f"root.{k}={v}" for k, v in sorted(self._params.items())]
                return 200, "\n".join(lines) + "\n"
            matches: dict[str, str] = {}
            for wanted in group.split(","):
                wanted = wanted.strip()
                if "*" in wanted:
                    # The camera takes a wildcard per path segment: Image.*.Enabled lists every view area's flag.
                    pattern = re.compile("^" + ".".join(
                        "[^.]+" if part == "*" else re.escape(part) for part in wanted.split(".")) + r"(\..*)?$")
                    matches.update({k: v for k, v in self._params.items() if pattern.match(k)})
                    continue
                prefix = wanted if wanted.endswith(".") else wanted + "."
                matches.update({k: v for k, v in self._params.items() if k == wanted or k.startswith(prefix)})
            if not matches:
                return 200, f"# Error: Error -1 getting param in group '{group}'\n"
            return 200, "\n".join(f"root.{k}={v}" for k, v in sorted(matches.items())) + "\n"
        if action == "update":
            for name, value in query.items():
                if name == "action":
                    continue
                if name not in self._params or not self._set_param(name, unquote(value)):
                    return 200, f"# Error: Error setting '{name}' to '{value}'!\n"
            return 200, "OK\n"
        return 200, "# Error: Unknown action\n"

    # ── opticscontrol.cgi ──

    def _apply_ir_cut(self, mode: str) -> None:
        self.set_state("ir_cut_filter", mode)
        self._params["ImageSource.I0.DayNight.IrCutFilter"] = {"on": "yes", "off": "no"}.get(mode, "auto")
        super().set_state("ir_cut_filter_param", self._params["ImageSource.I0.DayNight.IrCutFilter"])
        if mode == "on":
            self.set_state("day_mode", True)
        elif mode == "off":
            self.set_state("day_mode", False)

    def _optics_cgi(self, body: str):
        if not self._optics:
            return 404, "Not Found"
        _, method, context, params = self._parse_json(body)
        v = "1.2"
        if method == "getSupportedVersions":
            return 200, json.dumps({"context": context, "method": method, "data": {"apiVersions": [v]}})
        if method == "getCapabilities":
            return _json_ok(method, {"numberOfOptics": 1, "optics": [{
                "opticsId": "0",
                "capabilities": ["focus", "zoom", "irCutFilter", "calibrateFocus", "calibrateZoom",
                                 "compensateTemperature", "compensateIr"],
                "maxMagnification": self._max_magnification,
            }]}, v, context)
        if method == "getOptics":
            return _json_ok(method, {"numberOfOptics": 1, "optics": [{
                "opticsId": "0",
                "focusPosition": float(self.get_state("focus_position")),
                "focusMoving": bool(self.get_state("focus_moving")),
                "focusWindowUpperLeftX": 0.0, "focusWindowUpperLeftY": 0.0,
                "focusWindowWidth": 1.0, "focusWindowHeight": 1.0,
                "magnification": float(self.get_state("magnification")),
                "zoomMoving": bool(self.get_state("zoom_moving")),
                "temperatureCompensation": True,
                "irCutFilterState": str(self.get_state("ir_cut_filter")),
                "irCompensation": True,
            }]}, v, context)
        optics = params.get("optics")
        if not isinstance(optics, list) or not optics:
            return _json_error(method, 2103, "Required parameter missing", v, context)
        entry = optics[0]
        if str(entry.get("opticsId", "")) != "0":
            return _json_error(method, 2104, "Invalid parameter value specified", v, context,
                               details={"opticsId": entry.get("opticsId")})
        if method == "setMagnification":
            mag = entry.get("magnification")
            if not isinstance(mag, (int, float)) or not (1.0 <= mag <= self._max_magnification):
                return _json_error(method, 2104, "Invalid parameter value specified", v, context,
                                   details={"opticsId": "0", "propertyName": "magnification"})
            self.set_state("magnification", round(float(mag), 3))
            return _json_ok(method, {}, v, context)
        if method == "setRelativeMagnification":
            delta = self._relative_delta(entry, ZOOM_BIG_STEP, ZOOM_SMALL_STEP)
            if delta is None:
                return _json_error(method, 2104, "Invalid parameter value specified", v, context)
            self.set_state("magnification", round(_clamp(float(self.get_state("magnification")) + delta,
                                                         1.0, self._max_magnification), 3))
            return _json_ok(method, {}, v, context)
        if method == "setFocus":
            pos = entry.get("position")
            if not isinstance(pos, (int, float)) or not (0.0 <= pos <= 1.0):
                return _json_error(method, 2104, "Invalid parameter value specified", v, context,
                                   details={"opticsId": "0", "propertyName": "position"})
            self.set_state("focus_position", round(float(pos), 3))
            return _json_ok(method, {}, v, context)
        if method == "setRelativeFocus":
            delta = self._relative_delta(entry, FOCUS_BIG_STEP, FOCUS_SMALL_STEP)
            if delta is None:
                return _json_error(method, 2104, "Invalid parameter value specified", v, context)
            self.set_state("focus_position", round(_clamp(float(self.get_state("focus_position")) + delta, 0.0, 1.0), 3))
            return _json_ok(method, {}, v, context)
        if method == "performAutofocus":
            self.set_state("focus_position", 0.5)
            return _json_ok(method, {}, v, context)
        if method == "setFocusWindow":
            for name in ("upperLeftX", "upperLeftY", "width", "height"):
                value = entry.get(name)
                if not isinstance(value, (int, float)) or not (0.0 <= value <= 1.0):
                    return _json_error(method, 2104, "Invalid parameter value specified", v, context,
                                       details={"opticsId": "0", "propertyName": name})
            return _json_ok(method, {}, v, context)
        if method == "reset":
            if entry.get("zoom", False):
                self.set_state("magnification", 1.0)
            if entry.get("focus", False):
                self.set_state("focus_position", 0.5)
            return _json_ok(method, {}, v, context)
        if method == "calibrate":
            return _json_ok(method, {}, v, context)
        if method == "setIrCutFilterState":
            mode = str(entry.get("irCutFilterState", ""))
            if mode not in ("on", "off", "auto"):
                return _json_error(method, 2104, "Invalid parameter value specified", v, context,
                                   details={"opticsId": "0", "propertyName": "irCutFilterState"})
            self._apply_ir_cut(mode)
            return _json_ok(method, {}, v, context)
        if method in ("setTemperatureCompensation", "setIrCompensation"):
            if not isinstance(entry.get("enable"), bool):
                return _json_error(method, 2104, "Invalid parameter value specified", v, context)
            return _json_ok(method, {}, v, context)
        return _json_error(method, 2102, "Method not supported", v, context)

    @staticmethod
    def _relative_delta(entry: dict[str, Any], big: float, small: float) -> float | None:
        kind = str(entry.get("type", ""))
        if kind == "numerical":
            value = entry.get("value")
            return float(value) if isinstance(value, (int, float)) else None
        sign = 1.0 if kind.startswith("+") else -1.0 if kind.startswith("-") else None
        if sign is None:
            return None
        if kind[1:] == "bigStep":
            return sign * big
        if kind[1:] == "smallStep":
            return sign * small
        return None

    # ── daynight.cgi ──

    def _daynight_cgi(self, body: str):
        if not self._daynight_api:
            return 404, "Not Found"
        _, method, context, params = self._parse_json(body)
        v = "1.2"
        channel = params.get("channel")
        if method == "getSupportedVersions":
            return _json_ok(method, {"apiVersions": ["1.0", "1.2"]}, v, context)
        if channel != 0:
            return _json_error(method, 2104, "Invalid parameter value specified.", v, context, status=500)
        if method == "getCapabilities":
            return _json_ok(method, [{"channel": 0, "AutotuneSupport": True, "IrPassSupport": False,
                                      "NightDayShiftLevelSupport": True}], v, context)
        if method == "getConfiguration":
            return _json_ok(method, [{"channel": 0, **self._daynight}], v, context)
        if method == "setConfiguration":
            new = dict(self._daynight)
            for key, value in params.items():
                if key == "channel":
                    continue
                if key not in new:
                    return _json_error(method, 2104, "Invalid parameter value specified.", v, context, status=500)
                if key == "Autotune":
                    if not isinstance(value, bool):
                        return _json_error(method, 2104, "Invalid parameter value specified.", v, context, status=500)
                elif key == "NightFilter":
                    if value != "clear":
                        return _json_error(method, 1100, "Internal error", v, context, status=500,
                                           details=[{"NightFilter": "Can't set NightFilter, IR pass is not supported.", "channel": 0}])
                elif key == "NightDayShiftLevel" and new.get("Autotune"):
                    return _json_error(method, 1100, "Internal error, autotune set to true.", v, context, status=500)
                elif not isinstance(value, (int, float)) or isinstance(value, bool):
                    return _json_error(method, 2104, "Invalid parameter value specified.", v, context, status=500)
                elif key.endswith("ShiftLevel") and not (0 <= value <= 100):
                    return _json_error(method, 2104, "Invalid parameter value specified.", v, context, status=500)
                elif key.endswith("DwellTime") and not (1 <= value <= 600):
                    return _json_error(method, 2104, "Invalid parameter value specified.", v, context, status=500)
                new[key] = value
            self._daynight = new
            self._params["ImageSource.I0.DayNight.ShiftLevel"] = str(new["DayNightShiftLevel"])
            return _json_ok(method, [{"channel": 0, **self._daynight}], v, context)
        return _json_error(method, 2102, "Method not supported.", v, context)

    # ── I/O ──

    def _port_state_changed(self, port_id: str) -> None:
        item = self._ports[port_id]
        active = item["state"] != item["normalState"]
        if item["direction"] == "output":
            self.set_state("output_1" if port_id == "1" else f"output_{port_id}", active)
        else:
            self.set_state("input_0" if port_id == "0" else f"input_{port_id}", active)

    def _port_mgmt_cgi(self, body: str):
        if self._legacy:
            return 404, "Not Found"
        _, method, context, params = self._parse_json(body)
        v = "1.0"
        if method == "getSupportedVersions":
            return 200, json.dumps({"apiVersion": v, "context": context, "method": method,
                                    "data": {"apiVersions": ["1.0"]}})
        if method == "getPorts":
            items = [dict(p) for p in self._ports.values()]
            return _json_ok(method, {"numberOfPorts": len(items), "items": items}, v, context)
        if method == "setPorts":
            ports = params.get("ports")
            if not isinstance(ports, list):
                return _json_error(method, 2103, "Required parameter missing", v, context)
            affected = []
            for entry in ports:
                port_id = str(entry.get("port", ""))
                item = self._ports.get(port_id)
                if item is None:
                    return _json_error(method, 2104, "Invalid parameter value specified", v, context,
                                       details={"port": port_id, "propertyName": "port"})
                if "direction" in entry:
                    if entry["direction"] not in ("input", "output"):
                        return _json_error(method, 2104, "Invalid parameter value specified", v, context,
                                           details={"port": port_id, "propertyName": "direction"})
                    item["direction"] = entry["direction"]
                if "normalState" in entry:
                    if entry["normalState"] not in ("open", "closed"):
                        return _json_error(method, 2104, "Invalid parameter value specified", v, context,
                                           details={"port": port_id, "propertyName": "normalState"})
                    item["normalState"] = entry["normalState"]
                for name in ("name", "usage"):
                    if name in entry:
                        item[name] = str(entry[name])
                if "state" in entry:
                    if item["direction"] != "output" or entry["state"] not in ("open", "closed"):
                        return _json_error(method, 2104, "Invalid parameter value specified", v, context,
                                           details={"port": port_id, "propertyName": "state"})
                    item["state"] = entry["state"]
                    self._port_state_changed(port_id)
                affected.append(port_id)
            return _json_ok(method, {"ports": affected}, v, context)
        if method == "setStateSequence":
            port_id = str(params.get("port", ""))
            sequence = params.get("sequence")
            item = self._ports.get(port_id)
            if item is None or not isinstance(sequence, list) or not sequence:
                return _json_error(method, 2104, "Invalid parameter value specified", v, context)
            if item["direction"] != "output":
                return _json_error(method, 2104, "Invalid parameter value specified", v, context)
            if port_id in self._sequence_tasks and not self._sequence_tasks[port_id].done():
                return _json_error(method, 1200, "State sequence already ongoing.", v, context)
            for step in sequence:
                if step.get("state") not in ("open", "closed") or not isinstance(step.get("time"), int) \
                        or not (0 <= step["time"] <= 65535):
                    return _json_error(method, 2104, "Invalid parameter value specified", v, context)
            self._sequence_tasks[port_id] = asyncio.ensure_future(self._run_sequence(port_id, sequence))
            return _json_ok(method, {"port": port_id}, v, context)
        return _json_error(method, 2102, "Method not supported", v, context)

    async def _run_sequence(self, port_id: str, sequence: list[dict[str, Any]]) -> None:
        for step in sequence:
            self._ports[port_id]["state"] = step["state"]
            self._port_state_changed(port_id)
            await asyncio.sleep(step["time"] / 1000.0)

    def _port_cgi(self, query: dict[str, str]):
        if "check" in query:
            lines = []
            for number in query["check"].split(","):
                item = self._ports.get(str(int(number) - 1)) if number.strip().isdigit() else None
                if item is None:
                    return 200, "# Error: invalid port\n"
                lines.append(f"port{number}={'1' if item['state'] == 'closed' else '0'}")
            return 200, "\n".join(lines) + "\n"
        if "checkactive" in query:
            lines = []
            for number in query["checkactive"].split(","):
                item = self._ports.get(str(int(number) - 1)) if number.strip().isdigit() else None
                if item is None:
                    return 200, "# Error: invalid port\n"
                lines.append(f"port{number}={'active' if item['state'] != item['normalState'] else 'inactive'}")
            return 200, "\n".join(lines) + "\n"
        if "checkdirection" in query:
            lines = []
            for number in query["checkdirection"].split(","):
                item = self._ports.get(str(int(number) - 1)) if number.strip().isdigit() else None
                if item is None:
                    return 200, "# Error: invalid port\n"
                lines.append(f"port{number}={item['direction']}")
            return 200, "\n".join(lines) + "\n"
        if "action" in query:
            spec = unquote(query["action"])
            m = re.match(r"^(?:(\d+):)?([/\\](?:\d+[/\\])*)$", spec)
            if not m:
                return 200, "Error: invalid action\n"
            number = int(m.group(1) or "1")
            item = self._ports.get(str(number - 1))
            if item is None or item["direction"] != "output":
                return 200, "Error: invalid port\n"
            steps = re.findall(r"([/\\])(\d*)", m.group(2))
            sequence = []
            for char, wait in steps:
                active = char == "/"
                state = ("closed" if item["normalState"] == "open" else "open") if active else item["normalState"]
                sequence.append({"state": state, "time": int(wait) if wait else 0})
            port_id = str(number - 1)
            if len(sequence) == 1:
                item["state"] = sequence[0]["state"]
                self._port_state_changed(port_id)
            else:
                self._sequence_tasks[port_id] = asyncio.ensure_future(self._run_sequence(port_id, sequence))
            return 200, ""
        return 200, "# Error: unknown request\n"

    def _virtual_input_cgi(self, query: dict[str, str]):
        spec = unquote(query.get("action", ""))
        m = re.match(r"^(\d+):([/\\])(\d*)\\?$", spec)
        if not m:
            return 200, "Error: invalid action\n"
        number = int(m.group(1))
        active = m.group(2) == "/"
        self._virtual_inputs[number] = active
        self._emit("tns1:Device/tnsaxis:IO/tnsaxis:VirtualInput", {"port": str(number)},
                   {"active": "1" if active else "0"})
        if number == MANUAL_TRIGGER_NBR:
            self.set_state("manual_trigger", active)
        return 200, ""

    # ── lightcontrol.cgi ──

    def _light_cgi(self, body: str):
        _, method, context, params = self._parse_json(body)
        v = "1.0"
        if not self._lights:
            # The API answers on a camera with no illuminator; the hardware is
            # what is missing.
            return _json_error(method, 1005, "No light hardware found, could not complete request.", v, context)
        light_id = str(params.get("lightID", ""))
        if method == "getSupportedVersions":
            return _json_ok(method, {"apiVersions": ["1.0"]}, v, context)
        if method == "getServiceCapabilities":
            return _json_ok(method, {
                "automaticIntensitySupport": True, "manualIntensitySupport": True,
                "individualIntensitySupport": False, "getCurrentIntensitySupport": True,
                "manualAngleOfIlluminationSupport": False, "automaticAngleOfIlluminationSupport": False,
                "dayNightSynchronizeSupport": True, "multiIRWaveLengthSupport": False,
                "capabilities": [{"lightID": "led0"}],
            }, v, context)
        if method == "getLightInformation":
            return _json_ok(method, {"items": [{
                "lightID": "led0", "lightType": "IR", "enabled": self._light_enabled,
                "synchronizeDayNightMode": True, "lightState": bool(self.get_state("light_on")),
                "automaticIntensityMode": bool(self.get_state("light_auto")),
                "automaticAngleOfIlluminationMode": False, "nrOfLEDs": 1, "error": False, "errorInfo": "",
            }]}, v, context)
        if light_id != "led0":
            return _json_error(method, 1002, "Provided lightID parameter is not valid for the device.", v, context)
        if method == "activateLight":
            if not self._light_enabled:
                return _json_error(method, 1009, "The light group is turned off and can't be activated.", v, context)
            self.set_state("light_on", True)
            return _json_ok(method, {}, v, context)
        if method == "deactivateLight":
            self.set_state("light_on", False)
            return _json_ok(method, {}, v, context)
        if method == "enableLight":
            self._light_enabled = True
            return _json_ok(method, {}, v, context)
        if method == "disableLight":
            self._light_enabled = False
            self.set_state("light_on", False)
            return _json_ok(method, {}, v, context)
        if method == "getLightStatus":
            return _json_ok(method, {"status": bool(self.get_state("light_on"))}, v, context)
        if method == "getValidIntensity":
            return _json_ok(method, {"ranges": [{"low": 0, "high": 100}]}, v, context)
        if method == "setManualIntensity":
            intensity = params.get("intensity")
            if not isinstance(intensity, int) or not (0 <= intensity <= 100):
                return _json_error(method, 2104, "Invalid parameter value specified.", v, context)
            self.set_state("light_intensity", intensity)
            self.set_state("light_auto", False)
            return _json_ok(method, {}, v, context)
        if method == "getManualIntensity":
            return _json_ok(method, {"intensity": int(self.get_state("light_intensity"))}, v, context)
        if method == "getCurrentIntensity":
            current = int(self.get_state("light_intensity")) if self.get_state("light_on") else 0
            return _json_ok(method, {"intensity": current}, v, context)
        if method == "setAutomaticIntensityMode":
            enabled = params.get("enabled")
            if not isinstance(enabled, bool):
                return _json_error(method, 2104, "Invalid parameter value specified.", v, context)
            self.set_state("light_auto", enabled)
            return _json_ok(method, {}, v, context)
        return _json_error(method, 2102, "Method not supported.", v, context)

    # ── dynamicoverlay.cgi ──

    def _overlay_cgi(self, body: str):
        _, method, context, params = self._parse_json(body)
        v = "1.8"
        if method == "getSupportedVersions":
            return 200, json.dumps({"method": method, "context": context, "data": {"apiVersions": ["1.8"]}})
        if method == "getOverlayCapabilities":
            return _json_ok(method, {
                "maxFontSize": 200, "minFontSize": 8, "maxImageHeight": 1080, "maxImageSize": 2073600,
                "maxImageWidth": 1920, "maxTextLength": 512, "numAvailableSlots": OVERLAY_SLOTS,
                "rotationSupported": True, "slotsPerImageOverlay": 2, "slotsPerOverlay": 1,
                "slotsPerTextOverlay": 1, "supportedReferences": ["channel", "scene"],
            }, v, context)
        if method == "list":
            wanted = params.get("identity")
            texts, images = [], []
            for identity, ov in sorted(self._overlays.items()):
                if wanted is not None and identity != wanted:
                    continue
                if ov["kind"] == "text":
                    texts.append({
                        "camera": ov["camera"], "identity": identity, "text": ov["text"],
                        "position": ov["position"], "textColor": ov["textColor"],
                        "textBGColor": ov["textBGColor"], "textOLColor": "transparent",
                        "fontSize": ov["fontSize"], "rotation": 0, "scrollSpeed": 0,
                        "reference": "channel", "size": [32, 8 * len(ov["text"])], "zIndex": identity,
                        "visible": True, "scalable": True, "textLength": len(ov["text"]),
                    })
                else:
                    images.append({
                        "camera": ov["camera"], "identity": identity, "overlayPath": ov["overlayPath"],
                        "position": ov["position"], "zIndex": identity, "visible": True, "scalable": False,
                    })
            return _json_ok(method, {"imageFiles": list(self._image_files), "imageOverlays": images,
                                     "textOverlays": texts}, v, context)
        if method in ("addText", "addImage"):
            camera = params.get("camera")
            if not isinstance(camera, int) or not (1 <= camera <= 2):
                return _json_error(method, 102 if camera is None else 103,
                                   "A mandatory input parameter was not found in the input." if camera is None
                                   else "Invalid parameter.", v, context)
            slots = sum(2 if ov["kind"] == "image" else 1 for ov in self._overlays.values())
            if slots + (2 if method == "addImage" else 1) > OVERLAY_SLOTS:
                return _json_error(method, 300, "Unable to create overlays (limit reached)", v, context)
            position = params.get("position", [0.0, 0.0])
            if isinstance(position, str) and position not in OVERLAY_POSITIONS:
                return _json_error(method, 103, "Invalid value for parameter position", v, context)
            identity = 1
            while identity in self._overlays:
                identity += 1
            if method == "addText":
                text = params.get("text")
                if not isinstance(text, str) or not text:
                    return _json_error(method, 304, "Invalid value for parameter text", v, context)
                for name in ("textColor", "textBGColor"):
                    if name in params and params[name] not in OVERLAY_COLORS:
                        return _json_error(method, 103, f"Invalid value for parameter {name}", v, context)
                size = params.get("fontSize", 48)
                if not isinstance(size, int) or not (0 <= size <= 200):
                    return _json_error(method, 103, "Invalid value for parameter fontSize", v, context)
                self._overlays[identity] = {
                    "kind": "text", "camera": camera, "text": text[:512], "position": position,
                    "textColor": params.get("textColor", "black"),
                    "textBGColor": params.get("textBGColor", "transparent"), "fontSize": size,
                }
            else:
                path = str(params.get("overlayPath", ""))
                if not any(f.endswith("/" + path) or f == path for f in self._image_files):
                    return _json_error(method, 103, "Invalid value for parameter overlayPath", v, context)
                self._overlays[identity] = {"kind": "image", "camera": camera, "overlayPath": path,
                                            "position": position}
            self.set_state("overlay_count", len(self._overlays))
            return _json_ok(method, {"camera": camera, "identity": identity}, v, context)
        if method in ("setText", "setImage", "remove"):
            identity = params.get("identity")
            ov = self._overlays.get(identity) if isinstance(identity, int) else None
            if ov is None:
                return _json_error(method, 103, "Invalid value for parameter identity", v, context)
            if method == "remove":
                del self._overlays[identity]
                self.set_state("overlay_count", len(self._overlays))
                return _json_ok(method, {}, v, context)
            if (method == "setText") != (ov["kind"] == "text"):
                return _json_error(method, 103, "Invalid value for parameter identity", v, context)
            position = params.get("position")
            if position is not None:
                if isinstance(position, str) and position not in OVERLAY_POSITIONS:
                    return _json_error(method, 103, "Invalid value for parameter position", v, context)
                ov["position"] = position
            if method == "setText":
                if "text" in params:
                    ov["text"] = str(params["text"])[:512]
                for name in ("textColor", "textBGColor"):
                    if name in params:
                        if params[name] not in OVERLAY_COLORS:
                            return _json_error(method, 103, f"Invalid value for parameter {name}", v, context)
                        ov[name] = params[name]
                if "fontSize" in params:
                    ov["fontSize"] = int(params["fontSize"])
            elif "overlayPath" in params:
                ov["overlayPath"] = str(params["overlayPath"])
            return _json_ok(method, {}, v, context)
        return _json_error(method, 203, "Invalid method.", v, context)

    # ── viewarea, streamprofile, time, firmware ──

    def _view_area_cgi(self, body: str):
        _, method, context, _ = self._parse_json(body)
        if method == "getSupportedVersions":
            return _json_ok(method, {"apiVersions": ["1.0"]}, "1.0", context)
        if method != "list":
            return _json_error(method, 2102, "Method not supported", "1.0", context)
        return _json_ok(method, {"viewAreas": [
            {"id": 1000001, "source": 0, "camera": 1, "configurable": False},
            {"id": 1001002, "source": 0, "camera": 2, "configurable": True,
             "rectangularGeometry": {"horizontalOffset": 128, "horizontalSize": 640,
                                     "verticalOffset": 64, "verticalSize": 360},
             "canvasSize": {"horizontal": 1920, "vertical": 1080},
             "minSize": {"horizontal": 256, "vertical": 144},
             "maxSize": {"horizontal": 1920, "vertical": 1080},
             "grid": {"horizontalOffset": 8, "horizontalSize": 8, "verticalOffset": 8, "verticalSize": 8}},
        ]}, "1.0", context)

    def _stream_profile_cgi(self, body: str):
        _, method, context, _ = self._parse_json(body)
        if method != "list":
            return _json_error(method, 2102, "Method not supported", "1.0", context)
        profiles = []
        for key in sorted(k for k in self._params if re.match(r"StreamProfile\.S\d+\.Name$", k)):
            group = key.rsplit(".", 1)[0]
            profiles.append({"name": self._params[key], "description": self._param(group + ".Description"),
                             "parameters": unquote(self._param(group + ".Parameters"))})
        return _json_ok(method, {"streamProfile": profiles, "maxProfiles": 26}, "1.0", context)

    def _time_cgi(self, body: str):
        _, method, context, _ = self._parse_json(body)
        if method == "getSupportedVersions":
            return _json_ok(method, {"apiVersions": ["1.1"]}, "1.1", context)
        if method != "getDateTimeInfo":
            return _json_error(method, 2102, "Method not supported", "1.1", context)
        now = self._now()
        return _json_ok(method, {
            "dateTime": _fmt_iso(now), "maxYearSupported": 2037,
            "localDateTime": (now - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%S"),
            "timeZone": "America/New_York", "posixTimeZone": "EST5EDT,M3.2.0,M11.1.0", "dstEnabled": True,
        }, "1.1", context)

    def _firmware_cgi(self, body: str):
        _, method, context, _ = self._parse_json(body)
        if method == "reboot":
            self.set_state("rebooted", True)
            return _json_ok(method, {}, "1.3", context)
        if method == "getSupportedVersions":
            return _json_ok(method, {"apiVersions": ["1.3"]}, "1.3", context)
        return _json_error(method, 2102, "Method not supported", "1.3", context)

    # ── ptz.cgi / ptzconfig.cgi (PTZ models) ──

    def _ptz_cgi(self, query: dict[str, str]):
        camera = query.get("camera", "1")
        if camera not in ("1", "all"):
            return 200, "Error:\ncamera: invalid value\n"
        if not self._ptz_on():
            # What a fixed dome with digital PTZ turned off answers: whoami
            # says so in plain text, every other argument is an error.
            if "whoami" in query:
                return 200, "PTZ disabled\n"
            return 200, "Error:\nPTZ disabled\n"
        if "info" in query:
            return 200, ("Available commands\n:\n{camera=[n]}\nwhoami=yes\ncenter=[x],[y]\n  imagewidth=[n]\n"
                         "  imageheight=[n]\nareazoom=[x],[y],[z]\nmove={ home | up | down | left | right | "
                         "upleft | upright | downleft | downright | stop }\npan=[abspos]\ntilt=[abspos]\n"
                         "zoom=[n]\nrpan=[offset]\nrtilt=[offset]\nrzoom=[offset]\nautofocus={ on | off }\n"
                         "ircutfilter={ on | off | auto }\ncontinuouspantiltmove=[x-speed],[y-speed]\n"
                         "continuouszoommove=[speed]\ncontinuousfocusmove=[speed]\nauxiliary=[function]\n"
                         "gotoserverpresetname=[name]\ngotoserverpresetno=[n]\nspeed=[n]\n"
                         "query={ speed | position | limits | presetposcam | presetposall }\n")
        if "whoami" in query:
            return 200, ("Axis PTZ driver\n" if self._ptz else "Digital PTZ\n")
        if "query" in query:
            what = query["query"]
            if what == "position":
                return 200, (f"pan={float(self.get_state('pan')):.4f}\ntilt={float(self.get_state('tilt')):.4f}\n"
                             f"zoom={int(self.get_state('zoom'))}\n"
                             f"autofocus={'on' if self.get_state('autofocus') else 'off'}\nautoiris=on\n")
            if what == "limits":
                return 200, (f"MinPan={PAN_LIMITS[0]:.4f}\nMaxPan={PAN_LIMITS[1]:.4f}\nMinTilt={TILT_LIMITS[0]:.4f}\n"
                             f"MaxTilt={TILT_LIMITS[1]:.4f}\nMinZoom={ZOOM_LIMITS[0]}\nMaxZoom={ZOOM_LIMITS[1]}\n"
                             "MinIris=1\nMaxIris=9999\nMinFocus=1\nMaxFocus=9999\nMinFieldAngle=1\nMaxFieldAngle=623\n"
                             "MinBrightness=1\nMaxBrightness=9999\n")
            if what in ("presetposcam", "presetposall"):
                lines = ["Preset Positions for camera 1"] + [
                    f"presetposno{n}={p['name']}" for n, p in sorted(self._presets.items(), key=lambda kv: int(kv[0]))
                ]
                return 200, "\n".join(lines) + "\n"
            if what == "speed":
                return 200, "speed=50\n"
            return 200, f"Error:\nquery: unknown value: {what}\n"
        for name, value in query.items():
            try:
                self._ptz_apply(name, value)
            except ValueError as exc:
                return 200, f"Error:\n{exc}\n"
        return 204, ""

    def _ptz_apply(self, name: str, value: str) -> None:
        if name in ("camera", "imagewidth", "imageheight", "speed", "proportionalspeed"):
            return
        if name == "move":
            if value == "home":
                self._goto(self._presets[self._home])
            elif value == "stop":
                self._velocity = [0.0, 0.0, 0.0]
            elif value in ("up", "down", "left", "right", "upleft", "upright", "downleft", "downright"):
                dpan = 15.0 * (("right" in value) - ("left" in value))
                dtilt = 15.0 * (("up" in value) - ("down" in value))
                self.set_state("pan", _clamp(float(self.get_state("pan")) + dpan, *PAN_LIMITS))
                self.set_state("tilt", _clamp(float(self.get_state("tilt")) + dtilt, *TILT_LIMITS))
            else:
                raise ValueError(f"move: unknown value: {value}")
            return
        if name in ("pan", "tilt"):
            limits = PAN_LIMITS if name == "pan" else TILT_LIMITS
            self.set_state(name, _clamp(float(value), *limits))
            return
        if name == "zoom":
            self.set_state("zoom", int(_clamp(int(float(value)), *ZOOM_LIMITS)))
            return
        if name == "rpan":
            self.set_state("pan", _clamp(float(self.get_state("pan")) + float(value), *PAN_LIMITS))
            return
        if name == "rtilt":
            self.set_state("tilt", _clamp(float(self.get_state("tilt")) + float(value), *TILT_LIMITS))
            return
        if name == "rzoom":
            self.set_state("zoom", int(_clamp(int(self.get_state("zoom")) + int(float(value)), *ZOOM_LIMITS)))
            return
        if name == "continuouspantiltmove":
            pan, tilt = (float(v) for v in value.split(","))
            if not (-100 <= pan <= 100 and -100 <= tilt <= 100):
                raise ValueError("continuouspantiltmove: value out of range")
            self._velocity[0], self._velocity[1] = pan, tilt
            return
        if name == "continuouszoommove":
            speed = float(value)
            if not -100 <= speed <= 100:
                raise ValueError("continuouszoommove: value out of range")
            self._velocity[2] = speed
            return
        if name == "continuousfocusmove":
            self._focus_velocity = float(value)
            if self._focus_velocity:
                self.set_state("autofocus", False)
            return
        if name == "autofocus":
            if value not in ("on", "off"):
                raise ValueError("autofocus: unknown value")
            self.set_state("autofocus", value == "on")
            return
        if name == "ircutfilter":
            if value not in ("on", "off", "auto"):
                raise ValueError("ircutfilter: unknown value")
            self._params["PTZ.Various.V1.IrCutFilter"] = value
            self._apply_ir_cut(value)
            return
        if name == "backlight":
            if value not in ("on", "off"):
                raise ValueError("backlight: unknown value")
            return
        if name == "center":
            x, y = (int(v) for v in value.split(","))
            self.set_state("pan", _clamp(float(self.get_state("pan")) + (x - 960) / 32.0, *PAN_LIMITS))
            self.set_state("tilt", _clamp(float(self.get_state("tilt")) - (y - 540) / 32.0, *TILT_LIMITS))
            return
        if name == "areazoom":
            x, y, z = (int(v) for v in value.split(","))
            self._ptz_apply("center", f"{x},{y}")
            self.set_state("zoom", int(_clamp(int(self.get_state("zoom")) * z / 100.0, *ZOOM_LIMITS)))
            return
        if name == "gotoserverpresetno":
            preset = self._presets.get(value)
            if preset is None:
                raise ValueError(f"gotoserverpresetno: no preset {value}")
            self._goto(preset)
            self._emit("tns1:PTZController/tnsaxis:PTZPresets/tnsaxis:Channel_1", {"PresetToken": value},
                       {"PresetToken": value, "on_preset": "1"})
            return
        if name == "gotoserverpresetname":
            for number, preset in self._presets.items():
                if preset["name"] == value:
                    self._ptz_apply("gotoserverpresetno", number)
                    return
            raise ValueError(f"gotoserverpresetname: no preset {value}")
        if name == "auxiliary":
            self.set_state("last_aux", value)
            return
        raise ValueError(f"{name}: unknown argument")

    def _ptz_config_cgi(self, query: dict[str, str]):
        if not self._ptz_on():
            return 200, "Error:\nPTZ disabled\n"
        if "setserverpresetname" in query:
            name = query["setserverpresetname"]
            number = next((n for n, p in self._presets.items() if p["name"] == name), None)
            if number is None:
                number = str(max((int(n) for n in self._presets), default=0) + 1)
            self._presets[number] = {"name": name, "pan": float(self.get_state("pan")),
                                     "tilt": float(self.get_state("tilt")), "zoom": int(self.get_state("zoom"))}
            if query.get("home") == "yes":
                self._home = number
            return 204, ""
        if "removeserverpresetname" in query:
            number = next((n for n, p in self._presets.items() if p["name"] == query["removeserverpresetname"]), None)
            if number is None:
                return 200, "Error:\nno such preset\n"
            del self._presets[number]
            return 204, ""
        if "removeserverpresetno" in query:
            if query["removeserverpresetno"] not in self._presets:
                return 200, "Error:\nno such preset\n"
            del self._presets[query["removeserverpresetno"]]
            return 204, ""
        return 200, "Error:\nunknown argument\n"
