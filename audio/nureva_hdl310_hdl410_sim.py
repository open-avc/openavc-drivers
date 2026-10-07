"""
Nureva HDL310 / HDL410: Simulator

Simulates the connect module's local API: HTTPS on 443 (the simulator
terminates TLS with a throwaway self-signed certificate, as the real connect
module does with its own), JSON bodies, and three Server-Sent-Event streams.
Built from Nureva's local API documentation, https://developers-local.nureva.com.

  - Every request must carry Nureva-Client-Id and Nureva-Client-Version: 400
    naming each missing one, in the documented words, otherwise.
  - POST /api/v1/auth/login with the general (or admin) account returns
    ``authParameters``; every other endpoint except /api/v1 and the heartbeat
    wants ``Authorization: Nureva <authParameters>`` (401 without it).
  - The audio settings with the documented ranges, enumerations and firmware
    floors: an attribute this firmware does not have is left out of GET and
    refused on PATCH with "Unsupported Attributes: <name>. Firmware version
    is < <floor>." (microphoneGain is the other way round: firmware 1.3 to
    1.x only).
  - Volume up and down step speakerVolume by one, 0 to 20; a step past either
    end changes nothing.
  - Calibration takes 20 seconds and is announced started and completed on
    the event stream; it is refused while a speaker bar is disconnected.
  - Room layout with camera zones, the zone-to-camera map and the camera
    switching defaults; room profiles on an HDL410 (409 "Unsupported device
    type" on an HDL310); the camera switcher; the network configuration.
  - /api/v1/events announces calibration, the device information, the
    layout, the camera switcher, the network configuration, both status
    lights, USB and the speaker bars. Audio settings are not announced, as on
    the real system.
  - /api/v1/data honours ``?events=``: soundLocation every 200 ms (the
    talker's position from the Simulator UI, and the camera zone it falls in
    worked out from the zones' rectangles) and deviceMetrics every 5 s. A
    muted microphone, a disconnected bar or a system without sound location
    sends ``error (soundLocation)`` instead; an unknown event name sends the
    documented 404 error event.
  - /api/v1/heartbeat sends ``.`` every 5 s.

Simulator config (all optional): ``password`` (insist on one; empty accepts
anything but "invalid"), ``model`` (hdl410 or hdl310), ``firmware_version``,
``sound_location_supported`` (false makes soundLocation an "Unsupported
device" error, the open question for an HDL310), ``example_spelling`` (true
serves the zone and zone-map field names the documentation's HDL410 example
uses instead of the schema's), ``calibration_seconds``.

What the documents do not say, and this simulator decides: the body of a 404
for an unknown path, the bars' hardware ids, and how often a sound location
error repeats (once when it starts, then every 5 s while it lasts).

Driver: nureva_hdl310_hdl410
Transport: http (HTTPS)
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from openavc.simulator.http_simulator import HTTPSimulator

_TITLE = "Nureva Developer Toolkit"

_EVENTS = "/api/v1/events"
_DATA = "/api/v1/data"
_HEARTBEAT = "/api/v1/heartbeat"

_DATA_EVENTS = (
    "deviceMetrics", "soundLocation", "voiceAmplificationRmsLevel",
    "hdxVoiceAmplificationLevelChannel1", "hdxVoiceAmplificationLevelChannel2",
    "hdxVoiceAmplificationLevelChannel3", "hdxVoiceAmplificationLevelChannel4",
    "hdxSignalLevelChannel1", "hdxSignalLevelChannel2",
    "hdxSignalLevelChannel3", "hdxSignalLevelChannel4",
)

_TICK_S = 0.2           # soundLocation cadence (documented: 200 ms)
_SLOW_TICKS = 25        # deviceMetrics and heartbeat every 5 s

# Audio attribute -> (state key, rule, floor, ceiling). Floors and the one
# ceiling are the HDL310 / HDL410 firmware requirements in the reference's
# x-nureva capabilities. Rules: ("bool",), ("int", lo, hi), ("enum", [...]),
# ("ro",) for an attribute GET reports and PATCH does not take.
_AUDIO: dict[str, tuple[str, tuple, str, str | None]] = {
    "microphoneMute": ("microphone_mute", ("bool",), "0.9", None),
    "audienceMute": ("audience_mute", ("bool",), "1.1", None),
    "microphonePickupState": ("microphone_pickup", ("enum", ["Mono", "Stereo"]), "0.9", None),
    "microphoneGain": ("microphone_gain", ("int", -6, 12), "1.3", "1.999.999"),
    "speakerTrebleLevel": ("speaker_treble", ("int", 0, 100), "0.9", None),
    "speakerBassLevel": ("speaker_bass", ("int", 0, 100), "0.9", None),
    "speakerVolume": ("speaker_volume", ("ro",), "0.9", None),
    "echoReductionLevel": ("echo_reduction", ("enum", ["Low", "Medium", "High"]), "1.2", None),
    "noiseReductionLevel": ("noise_reduction", ("enum", ["Low", "Medium", "High"]), "1.2", None),
    "auxiliaryOutputState": (
        "aux_output_mode",
        ("enum", ["MicLevel", "LineLevel", "SpeakerOut", "MixedSignal", "SpeakerRef"]),
        "0.9", None,
    ),
    "voiceAmplificationEnabled": ("voice_amplification", ("bool",), "1.1", None),
    "voiceAmplificationLevel": ("voice_amplification_level", ("int", 0, 40), "1.1", None),
    "voiceAmplificationAuxInLevel": ("voice_amplification_aux_in", ("enum", ["Mic", "Line"]), "1.1", None),
    "dynamicBoostEnabled": ("dynamic_boost", ("bool",), "1.9", None),
    "microphoneDuckingEnabled": ("microphone_ducking", ("bool",), "1.9", None),
    "voiceAmplificationGateThreshold": ("voice_amp_gate_threshold", ("int", 0, 256), "2.0", None),
    "voiceAmplificationUsbOutputGainLevel": ("voice_amp_usb_gain", ("int", 0, 40), "2.0", None),
}

_PORTS = ["HDMI", "USB1", "USB2"]

# The documentation's room profile example.
_PROFILES = [
    ("68413b34-cc80-4140-9ecc-803a6cee035e", "Room Profile 1"),
    ("f5be8d1a-2a58-4cf4-8900-d400f5f57c75", "Room Profile 2"),
    ("4e8a2848-f575-42a6-b337-831011eea9bf", "Room Profile 3"),
]

# Two camera zones (the documentation's example ids), sized to the 8 ft
# minimum the zone tutorial gives an HDL310 / HDL410.
_DEFAULT_ZONES = [
    {
        "type": "Switching", "on": True,
        "geometry": {"point1": {"x": -2000, "y": 300}, "point2": {"x": 2000, "y": 3000}},
        "label": "Presenter", "id": "2345313e-deb8-4cd4-a58a-2df031296958",
    },
    {
        "type": "Switching", "on": True,
        "geometry": {"point1": {"x": -4500, "y": 3300}, "point2": {"x": 4500, "y": 9000}},
        "label": "Audience", "id": "9599a6a6-1603-4ceb-97f8-0008163ad88c",
    },
]
_DEFAULT_MAP = [
    {"zoneId": "2345313e-deb8-4cd4-a58a-2df031296958", "inputPort": "HDMI"},
    {"zoneId": "9599a6a6-1603-4ceb-97f8-0008163ad88c", "inputPort": "USB1"},
]

# State keys whose change the event stream announces, and as what.
_HARDWARE_KEYS = {"model", "firmware_version", "device_status", "enrollment_status"}
_SWITCHER_KEYS = {"camera_switcher_enabled", "active_camera_input", "camera_switcher_error"}


def _version(text: Any) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(text).split("-", 1)[0])
    return tuple(int(p) for p in parts[:3]) or (0,)


def _errors(*messages: str) -> dict[str, Any]:
    return {"errors": [{"message": m} for m in messages]}


def _problem(details: str, instance: str) -> dict[str, Any]:
    return {
        "type": "https://developers-local.nureva.com/probs/",
        "title": "One or more problems occurred",
        "instance": instance,
        "errorLevel": "ERROR",
        "problems": [{
            "type": "https://developers-local.nureva.com/probs/1000",
            "errorCode": 1000,
            "title": "Schema validation error",
            "details": details,
            "instance": instance,
            "errorLevel": "ERROR",
        }],
    }


class NurevaHdl310Hdl410Simulator(HTTPSimulator):
    """Nureva HDL310 / HDL410 connect module (HTTPS + SSE)."""

    SIMULATOR_INFO = {
        "driver_id": "nureva_hdl310_hdl410",
        "name": "Nureva HDL310 / HDL410 Simulator",
        "category": "audio",
        "transport": "http",
        "default_port": 443,
        # The real connect module speaks HTTPS only.
        "tls": True,
        "initial_state": {
            "model": "hdl410",
            "firmware_version": "1.9.278056-0",
            "device_version": "5.0.123456",
            "hardware_id": "XF714R57B319605",
            "ip_address": "10.0.0.1",
            "mac_address": "cd:ba:ea:fb:a9:b8",
            "enrollment_status": "enrolled",
            "device_status": "Ok",
            "network_led_colour": "green",
            "network_led_state": "solid",
            "console_led_colour": "green",
            "console_led_state": "solid",
            "usb_status": "connected",
            "components_status": "connected",
            "microphone_mute": False,
            "audience_mute": False,
            "microphone_pickup": "Mono",
            "microphone_gain": 3,
            "speaker_treble": 90,
            "speaker_bass": 16,
            "speaker_volume": 14,
            "echo_reduction": "Medium",
            "noise_reduction": "Medium",
            "aux_output_mode": "LineLevel",
            "voice_amplification": False,
            "voice_amplification_level": 25,
            "voice_amplification_aux_in": "Line",
            "dynamic_boost": False,
            "microphone_ducking": False,
            "voice_amp_gate_threshold": 65,
            "voice_amp_usb_gain": 10,
            "calibrating": False,
            "talker_x_mm": 0,
            "talker_y_mm": 1500,
            "talker_power_db": 55,
            "background_noise_db": 32,
            "camera_switcher_enabled": True,
            "camera_switcher_model": "CAM230",
            "camera_switcher_address": "10.43.0.245",
            "active_camera_input": "HDMI",
            "camera_switcher_error": "",
            "default_camera_input": "HDMI",
            "zone_trigger_wait_ms": 1000,
            "switch_to_default_wait_ms": 5000,
            "sound_location_algorithm": "BP",
            "room_profile_id": "68413b34-cc80-4140-9ecc-803a6cee035e",
            "network_static": True,
            "subnet_mask": "255.255.255.0",
            "gateway": "10.0.0.254",
            "dns_servers": "1.1.1.1, 8.8.8.8",
        },
        "error_modes": {
            "communication_timeout": {
                "description": "Connect module stops answering requests",
                "behavior": "no_response",
            },
            "password_changed": {
                "description": "The general password was changed in the Nureva App (every login and token refused, HTTP 401)",
                "behavior": "custom",
            },
            "rate_limited": {
                "description": "Too many requests in the last minute (HTTP 429)",
                "behavior": "custom",
            },
        },
        "controls": [
            {"type": "select", "key": "model", "label": "Model", "options": ["hdl410", "hdl310"]},
            {"type": "toggle", "key": "microphone_mute", "label": "Microphone Mute"},
            {"type": "slider", "key": "speaker_volume", "label": "Speaker Volume", "min": 0, "max": 20, "step": 1},
            {"type": "toggle", "key": "audience_mute", "label": "Audience Mute"},
            {"type": "toggle", "key": "voice_amplification", "label": "Voice Amplification"},
            {
                "type": "group", "label": "Talker",
                "controls": [
                    {"type": "slider", "key": "talker_x_mm", "label": "X", "min": -8382, "max": 8382, "step": 100, "unit": "mm"},
                    {"type": "slider", "key": "talker_y_mm", "label": "Y", "min": 1, "max": 16764, "step": 100, "unit": "mm"},
                    {"type": "slider", "key": "talker_power_db", "label": "Sound Level", "min": 0, "max": 90, "step": 1, "unit": "dB"},
                    {"type": "slider", "key": "background_noise_db", "label": "Background Noise", "min": 0, "max": 90, "step": 1, "unit": "dB"},
                ],
            },
            {
                "type": "group", "label": "Room Status",
                "controls": [
                    {"type": "select", "key": "network_led_colour", "label": "Network Light", "options": ["green", "yellow", "red", "none"]},
                    {"type": "select", "key": "console_led_colour", "label": "Console Light", "options": ["green", "yellow", "red", "none"]},
                    {"type": "select", "key": "usb_status", "label": "USB", "options": ["connected", "disconnected"]},
                    {"type": "select", "key": "components_status", "label": "Speaker Bars", "options": ["connected", "disconnected"]},
                    {"type": "select", "key": "device_status", "label": "System Status", "options": ["Ok", "CableUnplugged", "Disconnected"]},
                ],
            },
            {
                "type": "group", "label": "Camera Switcher",
                "controls": [
                    {"type": "toggle", "key": "camera_switcher_enabled", "label": "Enabled"},
                    {"type": "select", "key": "active_camera_input", "label": "Active Input", "options": ["HDMI", "USB1", "USB2"]},
                ],
            },
            {"type": "indicator", "key": "calibrating", "label": "Calibrating"},
            {"type": "indicator", "key": "firmware_version", "label": "Firmware"},
        ],
    }

    sse_paths = [_EVENTS, _DATA, _HEARTBEAT]

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        cfg = config or {}
        self._password = str(cfg.get("password", "") or "")
        self._tokens: set[str] = set()
        self._sound_location_supported = cfg.get("sound_location_supported", True) is not False
        self._example_spelling = bool(cfg.get("example_spelling", False))
        self._calibration_s = float(cfg.get("calibration_seconds", 20) or 20)
        self._zones: list[dict[str, Any]] = [json.loads(json.dumps(z)) for z in _DEFAULT_ZONES]
        self._zone_map: list[dict[str, str]] = [dict(m) for m in _DEFAULT_MAP]
        # Open streams: queue -> {"kind": events|data|heartbeat, "events": set}
        self._streams: dict[asyncio.Queue, dict[str, Any]] = {}
        self._ticker: asyncio.Task | None = None
        self._calibration: asyncio.Task | None = None
        self._location_error = ""
        self._location_error_ticks = 0
        for key in ("model", "firmware_version"):
            if cfg.get(key):
                super().set_state(key, str(cfg[key]))

    # ── Bodies ──

    def _firmware(self) -> tuple[int, ...]:
        return _version(self.get_state("firmware_version", "0"))

    def _has(self, floor: str, ceiling: str | None) -> bool:
        firmware = self._firmware()
        if firmware < _version(floor):
            return False
        if ceiling is not None and firmware > _version(ceiling):
            return False
        return True

    def _hdl310(self) -> bool:
        return str(self.get_state("model", "hdl410")) == "hdl310"

    def _audio_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {}
        for field, (key, rule, floor, ceiling) in _AUDIO.items():
            if not self._has(floor, ceiling):
                continue
            value = self.get_state(key)
            if rule[0] == "bool":
                value = bool(value)
            elif rule[0] in ("int", "ro"):
                value = int(value or 0)
            body[field] = value
        return body

    def _hardware_body(self) -> dict[str, Any]:
        components = [{"model": "ConnectModule2", "hardwareId": self.get_state("hardware_id")}]
        bars = 1 if self._hdl310() else 2
        for port in range(1, bars + 1):
            components.append({"model": "Bar", "hardwareId": f"AAC277A0D02{5 + port}", "portNumber": port})
        return {
            "hardwareId": self.get_state("hardware_id"),
            "model": self.get_state("model"),
            "firmwareVersion": self.get_state("firmware_version"),
            "deviceStatus": self.get_state("device_status"),
            "hardwareComponents": components,
            "ipAddress": self.get_state("ip_address"),
            "mac_address": self.get_state("mac_address"),
            "enrollmentStatus": self.get_state("enrollment_status"),
            # Only an admin login sees the code; the general account gets "".
            "enrollmentCode": "",
            "deviceVersion": self.get_state("device_version"),
        }

    def _status_body(self) -> dict[str, Any]:
        return {
            "leds": {
                "console": {"colour": self.get_state("console_led_colour"), "state": self.get_state("console_led_state")},
                "networkA": {"colour": self.get_state("network_led_colour"), "state": self.get_state("network_led_state")},
            },
            "usb": {"status": self.get_state("usb_status")},
            "deviceComponents": {"status": self.get_state("components_status")},
        }

    def _zones_body(self) -> list[dict[str, Any]]:
        if not self._example_spelling:
            return json.loads(json.dumps(self._zones))
        return [
            {"types": z["type"], "on": z["on"], "geometry": z["geometry"],
             "zoneId": z["id"], "ZoneLabel": z["label"]}
            for z in self._zones
        ]

    def _map_key(self) -> str:
        return "cameraSwitcherZoneInputMaps" if self._example_spelling else "cameraSwitcherZoneInputMap"

    def _defaults_body(self) -> dict[str, Any]:
        return {
            "defaultCameraInputPort": self.get_state("default_camera_input"),
            "zonesTriggerWaitTime": int(self.get_state("zone_trigger_wait_ms") or 0),
            "switchToDefaultWaitTime": int(self.get_state("switch_to_default_wait_ms") or 0),
        }

    def _layout_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "uiMeasurementSystem": "IMPERIAL",
            "roomDimensionsVerified": False,
            "audioComponentPositionsVerified": False,
            "zones": self._zones_body(),
            self._map_key(): [dict(m) for m in self._zone_map],
            "cameraSwitcherDefaults": self._defaults_body(),
        }
        if self._firmware() >= (2, 0):
            body["soundLocationAlgorithm"] = self.get_state("sound_location_algorithm")
        return body

    def _switcher_body(self) -> dict[str, Any]:
        error = str(self.get_state("camera_switcher_error") or "")
        return {
            "ipOrHostname": self.get_state("camera_switcher_address"),
            "port": 443,
            "enabled": bool(self.get_state("camera_switcher_enabled")),
            "model": self.get_state("camera_switcher_model"),
            "integrationErrors": [{"code": 1, "description": error}] if error else [],
            "firmwareVersion": "2.2",
            "macAddress": "10:B0:D0:63:C2:26",
            "cameraInputs": [
                {"name": "Nureva camera CV30", "port": "USB1"},
                {"name": "Logi Rally Camera", "port": "USB2"},
                {"name": "HDMI input", "port": "HDMI"},
            ],
            "activeCameraInput": self.get_state("active_camera_input"),
        }

    def _network_body(self) -> dict[str, Any]:
        dns = [d.strip() for d in str(self.get_state("dns_servers") or "").split(",") if d.strip()]
        return {"networkConfiguration": [{
            "port": "A",
            "isStatic": bool(self.get_state("network_static")),
            "ip": self.get_state("ip_address"),
            "subnetMask": self.get_state("subnet_mask"),
            "gateway": self.get_state("gateway"),
            "dns": dns,
            "mac": self.get_state("mac_address"),
        }]}

    def _profiles_body(self) -> dict[str, Any]:
        active = self.get_state("room_profile_id")
        return {"profiles": [
            {"profileId": pid, "name": name, "active": pid == active} for pid, name in _PROFILES
        ]}

    def _capabilities_body(self) -> dict[str, Any]:
        """The device's own OpenAPI description, as the reference's embedded
        definitions are laid out (openapi, info, servers, then paths)."""
        general = {"minimumRoleRequired": "general"}
        profiles_ext: dict[str, Any] = {
            "minimumRoleRequired": "general",
            "requirements": {"supportedDeviceTypes": ["hdl300", "dual-hdl", "hdl410", "HDX"]},
        }
        if self._hdl310():
            profiles_ext["requirementsFailed"] = {"supportedDeviceTypes": ["hdl310"]}
        paths = {
            "/api/v1": {"get": {"summary": "Get API capabilities", "x-nureva": {"minimumRoleRequired": None}}},
            "/api/v1/auth/login": {"post": {"summary": "Login to application", "x-nureva": {"minimumRoleRequired": None}}},
            "/api/v1/heartbeat": {"get": {"summary": "Check the connection", "x-nureva": {"minimumRoleRequired": None}}},
            "/api/v1/events": {"get": {"summary": "Get events stream", "x-nureva": general}},
            "/api/v1/data": {"get": {"summary": "Start device data stream", "x-nureva": general}},
            "/api/v1/audio": {"get": {"summary": "Get audio settings", "x-nureva": general},
                              "patch": {"summary": "Set audio settings", "x-nureva": general}},
            "/api/v1/audio/volume/change": {"put": {"summary": "Change the speaker volume", "x-nureva": general}},
            "/api/v1/audio/calibrate": {"post": {"summary": "Calibrate the device", "x-nureva": general}},
            "/api/v1/audio/identify": {"post": {"summary": "Identify audio components", "x-nureva": general}},
            "/api/v1/audio/hardware": {"get": {"summary": "Get device information", "x-nureva": general}},
            "/api/v1/status": {"get": {"summary": "Get room status", "x-nureva": general}},
            "/api/v1/room/layout": {"get": {"summary": "Get room layout", "x-nureva": general},
                                    "patch": {"summary": "Set room layout", "x-nureva": general}},
            "/api/v1/room/profiles": {"get": {"summary": "Get room profiles", "x-nureva": profiles_ext}},
            "/api/v1/integrations/camera-switcher": {"get": {"summary": "Get camera switcher integration details", "x-nureva": general},
                                                     "patch": {"summary": "Update camera switcher integration details", "x-nureva": general}},
            "/api/v1/network/configuration": {"get": {"summary": "Get network configuration", "x-nureva": general}},
        }
        return {
            "openapi": "3.1.0",
            "info": {
                "title": _TITLE,
                "version": "v1",
                "description": (
                    "Nureva Developer Toolkit provides modern local APIs to securely "
                    "manage and control your device and gain unique audio insights, "
                    "safely, from within your network."
                ),
                "contact": {
                    "name": _TITLE,
                    "url": "https://developers-local.nureva.com",
                    "email": "developers@nureva.com",
                },
            },
            "servers": [{"url": "https://{nurevaDeviceIP}", "description": "Local device connection via HTTPS"}],
            "paths": paths,
        }

    # ── Gates ──

    @staticmethod
    def _header(headers: dict[str, str], name: str) -> str:
        lowered = name.lower()
        for key, value in headers.items():
            if key.lower() == lowered:
                return str(value)
        return ""

    def _missing_headers(self, headers: dict[str, str]) -> dict[str, Any] | None:
        missing = [
            f"RESTApi: {name} is missing"
            for name in ("Nureva-Client-Id", "Nureva-Client-Version")
            if not self._header(headers, name).strip()
        ]
        return _errors(*missing) if missing else None

    def _authorized(self, headers: dict[str, str]) -> bool:
        if "password_changed" in self.active_errors:
            return False
        value = self._header(headers, "Authorization").strip()
        if not value.startswith("Nureva "):
            return False
        return value[len("Nureva "):].strip() in self._tokens

    # ── Requests ──

    def handle_request(
        self, method: str, path: str, headers: dict[str, str], body: str,
    ) -> tuple[int, dict | str]:
        parts = urlsplit(path)
        route = unquote(parts.path).rstrip("/") or "/"
        missing = self._missing_headers(headers)
        if missing is not None:
            return 400, missing
        if "rate_limited" in self.active_errors:
            return 429, _errors("Too many requests. Rate limit is exceeded")
        if route == "/api/v1" and method == "GET":
            return 200, self._capabilities_body()
        if route == "/api/v1/auth/login" and method == "POST":
            return self._login(body)
        if not self._authorized(headers):
            return 401, _errors("Unauthorized")

        if route == "/api/v1/audio":
            if method == "GET":
                return 200, self._audio_body()
            if method == "PATCH":
                return self._patch_audio(body)
        if route == "/api/v1/audio/volume/change" and method == "PUT":
            return self._change_volume(body)
        if route == "/api/v1/audio/calibrate" and method == "POST":
            return self._calibrate()
        if route == "/api/v1/audio/identify" and method == "POST":
            return self._identify(body)
        if route == "/api/v1/audio/hardware" and method == "GET":
            return 200, self._hardware_body()
        if route == "/api/v1/status" and method == "GET":
            return 200, self._status_body()
        if route == "/api/v1/room/layout":
            if method == "GET":
                return 200, self._layout_body()
            if method == "PATCH":
                return self._patch_layout(body)
        if route == "/api/v1/room/profiles" and method == "GET":
            if self._hdl310():
                return 409, _errors("Unsupported device type")
            return 200, self._profiles_body()
        match = re.fullmatch(r"/api/v1/room/profiles/([^/]+)/active", route)
        if match and method == "POST":
            return self._activate_profile(match.group(1), route)
        if route == "/api/v1/integrations/camera-switcher":
            if method == "GET":
                return 200, self._switcher_body()
            if method == "PATCH":
                return self._patch_switcher(body)
        if route == "/api/v1/network/configuration" and method == "GET":
            return 200, self._network_body()
        return 404, _errors("Not Found")

    @staticmethod
    def _json(body: str) -> tuple[Any, bool]:
        if not body.strip():
            return None, True
        try:
            return json.loads(body), True
        except ValueError:
            return None, False

    def _login(self, raw: str) -> tuple[int, dict]:
        payload, ok = self._json(raw)
        if not ok or not isinstance(payload, dict):
            return 400, _errors("Unsupported Attributes: account, password. Both attributes must be strings.")
        account, password = payload.get("account"), payload.get("password")
        for name, value in (("account", account), ("password", password)):
            if not isinstance(value, str):
                kind = "nothing" if value is None else type(value).__name__
                return 400, _errors(f"Unsupported Attributes: {name}. Expected string, received {kind}.")
        if account not in ("general", "admin"):
            return 400, _errors("Unsupported Attributes: account.")
        refused = (
            "password_changed" in self.active_errors
            or password == "invalid"
            or (self._password and password != self._password)
        )
        if refused:
            return 401, _errors("Invalid account or password")
        token = base64.b64encode(f"{account}:{password}".encode("utf-8")).decode("ascii")
        self._tokens.add(token)
        return 200, {"authParameters": token}

    def _patch_audio(self, raw: str) -> tuple[int, dict]:
        payload, ok = self._json(raw)
        if not ok or not isinstance(payload, dict) or not payload:
            return 400, _errors("Unsupported. At least one valid control setting must be included in the request body.")
        changes: dict[str, Any] = {}
        for field, value in payload.items():
            spec = _AUDIO.get(field)
            if spec is None or spec[1][0] == "ro":
                return 400, _errors(f"Unsupported Attributes: {field}.")
            key, rule, floor, ceiling = spec
            if not self._has(floor, ceiling):
                if self._firmware() < _version(floor):
                    return 400, _errors(f"Unsupported Attributes: {field}. Firmware version is < {floor}.0.")
                return 400, _errors(f"Unsupported Attributes: {field}. Firmware version is > {ceiling}.")
            if rule[0] == "bool":
                if not isinstance(value, bool):
                    return 400, _errors(f"Unsupported Attributes: {field}. Expected boolean.")
            elif rule[0] == "int":
                if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
                    return 400, _errors(f"Unsupported Attributes: {field}. Expected integer.")
                if not rule[1] <= value <= rule[2]:
                    return 400, _errors(f"Unsupported Attributes: {field}. Must be between {rule[1]} and {rule[2]}.")
                value = int(value)
            elif rule[0] == "enum":
                if value not in rule[1]:
                    return 400, _errors(f"Unsupported Attributes: {field}. Invalid enum value.")
            changes[key] = value
        for key, value in changes.items():
            self.set_state(key, value)
        return 200, {}

    def _change_volume(self, raw: str) -> tuple[int, dict]:
        payload, ok = self._json(raw)
        operation = payload.get("operation") if isinstance(payload, dict) else None
        if not ok or operation not in ("increment", "decrement"):
            return 400, _errors("Unsupported Attributes: operation.")
        volume = int(self.get_state("speaker_volume") or 0)
        volume = min(20, volume + 1) if operation == "increment" else max(0, volume - 1)
        self.set_state("speaker_volume", volume)
        return 200, {}

    def _calibrate(self) -> tuple[int, dict]:
        if self.get_state("components_status") != "connected":
            return 409, _errors("Speaker bar disconnected")
        if self._calibration is not None and not self._calibration.done():
            self._calibration.cancel()
        self.set_state("calibrating", True)
        try:
            self._calibration = asyncio.get_running_loop().create_task(self._finish_calibration())
        except RuntimeError:
            self._calibration = None
        return 200, {}

    async def _finish_calibration(self) -> None:
        try:
            await asyncio.sleep(self._calibration_s)
        except asyncio.CancelledError:
            return
        self.set_state("calibrating", False)

    def _identify(self, raw: str) -> tuple[int, dict]:
        if self._firmware() < (1, 8):
            return 400, _errors("Unsupported. Firmware version is < 1.8.0.")
        payload, ok = self._json(raw)
        if not ok or (isinstance(payload, dict) and "port" in payload):
            # HDL310 / HDL410: the request body should be empty.
            return 400, _problem("port must not be provided for this device.", "/api/v1/audio/identify")
        return 200, {}

    def _patch_layout(self, raw: str) -> tuple[int, dict]:
        payload, ok = self._json(raw)
        if not ok or not isinstance(payload, dict) or not payload:
            return 400, _problem("The request body must be an object.", "/api/v1/room/layout")
        changes: dict[str, Any] = {}
        zones = None
        zone_map = None
        for field, value in payload.items():
            if field == "cameraSwitcherDefaults":
                if not isinstance(value, dict):
                    return 400, _problem("cameraSwitcherDefaults must be an object.", "/api/v1/room/layout")
                for required in ("defaultCameraInputPort", "zonesTriggerWaitTime", "switchToDefaultWaitTime"):
                    if required not in value:
                        return 400, _problem(f"cameraSwitcherDefaults.{required} is required.", "/api/v1/room/layout")
                if value["defaultCameraInputPort"] not in _PORTS:
                    return 400, _problem("defaultCameraInputPort must be HDMI, USB1 or USB2.", "/api/v1/room/layout")
                for name in ("zonesTriggerWaitTime", "switchToDefaultWaitTime"):
                    number = value[name]
                    if isinstance(number, bool) or not isinstance(number, (int, float)) or number < 0:
                        return 400, _problem(f"{name} must be a number greater than or equal to zero.", "/api/v1/room/layout")
                changes["default_camera_input"] = value["defaultCameraInputPort"]
                changes["zone_trigger_wait_ms"] = int(value["zonesTriggerWaitTime"])
                changes["switch_to_default_wait_ms"] = int(value["switchToDefaultWaitTime"])
            elif field == "soundLocationAlgorithm":
                if self._firmware() < (2, 0):
                    return 400, _errors("Unsupported Attributes: soundLocationAlgorithm. Firmware version is < 2.0.0.")
                if value not in ("BP", "TDOA"):
                    return 400, _problem("soundLocationAlgorithm must be BP or TDOA.", "/api/v1/room/layout")
                changes["sound_location_algorithm"] = value
            elif field in ("cameraSwitcherZoneInputMap", "cameraSwitcherZoneInputMaps"):
                if not isinstance(value, list) or not all(
                    isinstance(m, dict) and m.get("inputPort") in _PORTS and m.get("zoneId") for m in value
                ):
                    return 400, _problem("Each zone input map needs a zoneId and an inputPort.", "/api/v1/room/layout")
                zone_map = [{"zoneId": str(m["zoneId"]), "inputPort": m["inputPort"]} for m in value]
            elif field == "zones":
                if not isinstance(value, list) or len(value) > 8:
                    return 400, _problem("A room can have at most eight camera zones.", "/api/v1/room/layout")
                zones = []
                for zone in value:
                    if not isinstance(zone, dict) or not zone.get("id") or "geometry" not in zone:
                        return 400, _problem("Each zone needs an id and a geometry.", "/api/v1/room/layout")
                    zones.append({
                        "type": "Switching", "on": True, "geometry": zone["geometry"],
                        "label": str(zone.get("label", "")), "id": str(zone["id"]),
                    })
            else:
                return 400, _errors(f"Unsupported Attributes: {field}.")
        layout_changed = False
        if zones is not None:
            self._zones = zones
            layout_changed = True
        if zone_map is not None:
            self._zone_map = zone_map
            layout_changed = True
        for key, value in changes.items():
            if self.get_state(key) != value:
                super().set_state(key, value)
                layout_changed = True
        if layout_changed:
            self._announce("/api/v1/room/layout", self._layout_body())
        return 200, {}

    def _activate_profile(self, profile_id: str, route: str) -> tuple[int, dict]:
        if self._hdl310():
            return 409, _errors("Unsupported device type")
        if profile_id not in {pid for pid, _name in _PROFILES}:
            return 404, _problem(f"Room profile {profile_id} was not found.", route)
        if self.get_state("room_profile_id") != profile_id:
            super().set_state("room_profile_id", profile_id)
            self._announce("/api/v1/room/layout", self._layout_body())
        return 200, {}

    def _patch_switcher(self, raw: str) -> tuple[int, dict]:
        payload, ok = self._json(raw)
        if not ok or not isinstance(payload, dict) or not payload:
            return 400, _errors("Unsupported. At least one valid setting must be included in the request body.")
        allowed = {"ipOrHostname", "port", "username", "password", "accessToken", "enabled"}
        for field in payload:
            if field not in allowed:
                return 400, _errors(f"Unsupported Attributes: {field}.")
        if "enabled" in payload:
            if not isinstance(payload["enabled"], bool):
                return 400, _errors("Unsupported Attributes: enabled. Expected boolean.")
            self.set_state("camera_switcher_enabled", payload["enabled"])
        if "ipOrHostname" in payload:
            self.set_state("camera_switcher_address", str(payload["ipOrHostname"]))
        return 202, {}

    # ── Streams ──

    @staticmethod
    def _frame(event: str, data: Any) -> str:
        text = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
        return f"event: {event}\ndata: {text}\n\n"

    def open_stream(
        self, path: str, headers: dict[str, str],
    ) -> tuple[int, dict | None, asyncio.Queue | None]:
        """Open an event stream. Returns (status, error body, queue); the queue
        yields framed SSE text and None at the end."""
        parts = urlsplit(path)
        route = unquote(parts.path).rstrip("/")
        missing = self._missing_headers(headers)
        if missing is not None:
            return 400, missing, None
        if "rate_limited" in self.active_errors:
            return 429, _errors("Too many requests. Rate limit is exceeded"), None
        if route != _HEARTBEAT and not self._authorized(headers):
            return 401, _errors("Unauthorized"), None
        queue: asyncio.Queue = asyncio.Queue()
        if route == _EVENTS:
            self._streams[queue] = {"kind": "events", "events": set()}
            return 200, None, queue
        if route == _HEARTBEAT:
            self._streams[queue] = {"kind": "heartbeat", "events": set()}
            self._ensure_ticker()
            return 200, None, queue
        query = parse_qs(parts.query)
        raw = ",".join(query.get("events", []))
        names = [n.strip() for n in raw.split(",") if n.strip()] or ["soundLocation", "deviceMetrics"]
        wanted: set[str] = set()
        for name in names:
            if name in _DATA_EVENTS:
                wanted.add(name)
            else:
                queue.put_nowait(self._frame(f"error ({name})", {
                    "statusCode": 404, "event": name, "message": f"Event {name} is not supported",
                }))
        self._streams[queue] = {"kind": "data", "events": wanted}
        self._ensure_ticker()
        return 200, None, queue

    def close_stream(self, queue: asyncio.Queue) -> None:
        self._streams.pop(queue, None)
        if not any(s["kind"] != "events" for s in self._streams.values()):
            ticker, self._ticker = self._ticker, None
            if ticker is not None:
                ticker.cancel()

    def _ensure_ticker(self) -> None:
        if self._ticker is not None and not self._ticker.done():
            return
        try:
            self._ticker = asyncio.get_running_loop().create_task(self._tick())
        except RuntimeError:
            self._ticker = None

    async def _tick(self) -> None:
        tick = 0
        try:
            while True:
                slow = tick % _SLOW_TICKS == 0
                self._emit_data(slow)
                tick += 1
                await asyncio.sleep(_TICK_S)
        except asyncio.CancelledError:
            return

    def _location_unavailable(self) -> str:
        """Why soundLocation cannot be sent right now, in the device's words."""
        if not self._sound_location_supported:
            return "Unsupported device"
        if self.get_state("components_status") != "connected":
            return "Speaker bar disconnected"
        if self.get_state("microphone_mute"):
            return "Microphone muted"
        return ""

    def _location_body(self) -> dict[str, Any]:
        power = float(self.get_state("talker_power_db") or 0)
        x = int(self.get_state("talker_x_mm") or 0)
        y = int(self.get_state("talker_y_mm") or 0)
        triggered = []
        if power > 0:
            for zone in self._zones:
                p1, p2 = zone["geometry"]["point1"], zone["geometry"]["point2"]
                if min(p1["x"], p2["x"]) <= x <= max(p1["x"], p2["x"]) and \
                        min(p1["y"], p2["y"]) <= y <= max(p1["y"], p2["y"]):
                    triggered.append({"type": ["Switching"], "label": zone["label"], "id": zone["id"]})
                    break
        return {
            "version": 3,
            "azimuth": 0,
            "powerLevel": power,
            "coordinates": {"x": x, "y": y},
            "time": "2026-06-04T14:22:31.834Z",
            "triggeredZones": triggered,
            "flags": {
                "powerZeroedReason": 0 if power > 0 else 3,
                "speakerActive": False,
                "voiceAmpModeActive": False,
                "voiceDetected": power > 0,
            },
        }

    def _emit_data(self, slow: bool) -> None:
        reason = self._location_unavailable()
        if reason:
            send_error = reason != self._location_error or self._location_error_ticks % _SLOW_TICKS == 0
            self._location_error_ticks += 1
        else:
            send_error = False
            self._location_error_ticks = 0
        self._location_error = reason
        status = 500 if reason == "Unsupported device" else 503
        for queue, stream in list(self._streams.items()):
            kind = stream["kind"]
            if kind == "heartbeat":
                if slow:
                    queue.put_nowait("event: heartbeat\ndata: .\nretry: 5000\n\n")
                continue
            if kind != "data":
                continue
            if "soundLocation" in stream["events"]:
                if reason:
                    if send_error:
                        queue.put_nowait(self._frame("error (soundLocation)", {
                            "statusCode": status, "error": [{"message": reason}],
                        }))
                else:
                    queue.put_nowait(self._frame("soundLocation", self._location_body()))
            if slow and "deviceMetrics" in stream["events"]:
                queue.put_nowait(self._frame("deviceMetrics", {
                    "backgroundNoise": int(self.get_state("background_noise_db") or 0),
                }))

    def _announce(self, event: str, data: Any) -> None:
        frame = self._frame(event, data)
        for queue, stream in list(self._streams.items()):
            if stream["kind"] == "events":
                queue.put_nowait(frame)
        if any(s["kind"] == "events" for s in self._streams.values()):
            self.log_protocol("out", frame.strip()[:200])

    # ── State ──

    def set_state(self, key: str, value: Any) -> None:
        previous = self.get_state(key)
        super().set_state(key, value)
        if previous == value:
            return
        if key == "calibrating":
            self._announce("/api/v1/audio/calibrate", {"status": "started" if value else "completed"})
        elif key in _HARDWARE_KEYS:
            self._announce("/api/v1/audio/hardware", self._hardware_body())
        elif key in ("network_led_colour", "network_led_state"):
            self._announce("ledStateUpdated", {
                "colour": self.get_state("network_led_colour"),
                "state": self.get_state("network_led_state"), "ledType": "networkA",
            })
        elif key in ("console_led_colour", "console_led_state"):
            self._announce("ledStateUpdated", {
                "colour": self.get_state("console_led_colour"),
                "state": self.get_state("console_led_state"), "ledType": "console",
            })
        elif key == "usb_status":
            self._announce("usbConnection", {"status": value})
        elif key == "components_status":
            self._announce("deviceComponentsConnection", {"overallStatus": value})
        elif key in _SWITCHER_KEYS or key == "camera_switcher_address":
            self._announce("/api/v1/integrations/camera-switcher", self._switcher_body())
        elif key in ("default_camera_input", "zone_trigger_wait_ms",
                     "switch_to_default_wait_ms", "sound_location_algorithm"):
            self._announce("/api/v1/room/layout", self._layout_body())
        elif key in ("network_static", "subnet_mask", "gateway", "dns_servers"):
            self._announce("/api/v1/network/configuration", {})

    # ── The streams themselves (aiohttp, on the real platform) ──

    async def _serve_sse(self, request: Any, path: str) -> Any:
        from aiohttp import web

        status, error, queue = self.open_stream(path, dict(request.headers))
        if status != 200 or queue is None:
            self.log_protocol("in", f"GET {path} (stream refused {status})")
            return web.Response(status=status, text=json.dumps(error or {}),
                                content_type="application/json")
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
        )
        await response.prepare(request)
        self.log_protocol("in", f"GET {path} (stream opened)")
        try:
            while self._running:
                chunk = await queue.get()
                if chunk is None:
                    break
                await response.write(chunk.encode("utf-8"))
        except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.close_stream(queue)
            self.log_protocol("in", f"GET {path} (stream closed)")
        return response

    async def stop(self) -> None:
        for queue in list(self._streams):
            queue.put_nowait(None)
        for task in (self._ticker, self._calibration):
            if task is not None:
                task.cancel()
        self._ticker = self._calibration = None
        await super().stop()
