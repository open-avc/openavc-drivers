"""
Algo IP endpoint: Simulator

Simulates the RESTful API of an Algo IP speaker, paging adapter, visual
alerter, display speaker, intercom, 8063 door controller or 8450 console:
HTTPS on 443 with a throwaway self-signed certificate (the real device's is
self-signed too) and JSON bodies. Built from Algo's RESTful API Guide,
https://docs.algosolutions.com/docs/restful-api-guide, the strobe pattern
reference and the provisioning parameter guides.

  - Authentication as the device does it: Standard checks the HMAC-SHA256
    signature over METHOD:URI[:CONTENT_MD5:application/json]:TIMESTAMP:NONCE,
    that Content-MD5 is the body's, and that the timestamp is within 30
    seconds of the simulated device's clock; Basic checks admin:<password>;
    None takes anything. Every answer carries the device's Date header.
  - The model decides what is there: the About and Status pages, which calls
    answer and which settings exist follow the guide's "Applicable Products"
    lines, and a call the model lacks answers 404. Firmware floors from the
    guide apply the same way (``duration`` before 5.7 is ignored; Stop Tone
    needs the tone's name on 5.4 and older).
  - Strobe patterns and brightness are checked against the model's own
    table: 0 / 56 / 255 on most, 1 / 2 / 3 on the 8190S.
  - Settings read and write through GET /api/settings/{parameter} and PUT
    /api/settings: volumes as "<n>dB" in 3 dB steps, on/off as "1" / "0".
  - Restore Factory Defaults turns the RESTful API off, as on the device: every
    call answers 404 until ``admin.web.api`` is turned back on (the
    "api_disabled" error mode does the same).

Simulator config (all optional): ``model`` (default 8410), ``firmware_version``
(default 5.7.1), ``auth_method`` (standard, basic or none; default standard),
``password`` (the RESTful API password to insist on; empty accepts any except
"invalid"), ``clock_offset_s`` (how far the device's clock is from the real
one), ``timestamp_tolerance_s`` (``api.auth.tsvar``: how far a Standard
timestamp may be from the device's clock; default 30).

What the documents do not say, and this simulator decides: the status code and
body of a refused signature (401, ``{"error": "Unauthorized"}``), of a call the
model lacks or a disabled API (404, ``{"error": "Not Found"}``) and of a bad
value (400 with an ``error``); the body of an accepted control call (empty); the
About page's Product Name for every model but the 8301 (the product page's
name); an unknown tone or console button (400 / 404).

Driver: algo_ip_endpoint
Transport: http (HTTPS)
"""

from __future__ import annotations

import base64
import email.utils
import hashlib
import hmac
import json
import re
import time
from typing import Any
from urllib.parse import urlsplit

from openavc.simulator.http_simulator import HTTPSimulator

_TOLERANCE_S = 30

_PRODUCTS = {
    "8186": "Algo 8186 IP Horn Speaker",
    "8188": "Algo 8188 IP Ceiling Speaker",
    "8190S": "Algo 8190S IP Speaker - Clock & Visual Alerter",
    "8301": "Algo 8301 IP Paging Adapter & Scheduler",
    "8128": "Algo 8128 IP Visual Alerter",
    "8138": "Algo 8138 IP Color Visual Alerter",
    "8410": "Algo 8410 IP Display Speaker",
    "8420": "Algo 8420 IP Dual-Sided Display Speaker",
    "8201": "Algo 8201 IP Intercom",
    "8063": "Algo 8063 IP Door Controller",
    "8450": "Algo 8450 IP Console",
}

_SPEAKERS = {"8186", "8188", "8190S", "8410", "8420"}
_ADAPTERS = {"8301"}
_STROBE_ONLY = {"8128", "8138"}
_STROBES = {"8128", "8138", "8190S", "8410", "8420"}
_DISPLAYS = {"8410", "8420"}
_INTERCOMS = {"8201"}
_DOORS = {"8201", "8063"}

_PATTERNS = {
    "8128": {0, 1, 3, 5, 7, 8, 11, 12, 13, 14, 15},
    "8138": {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15},
    "8190S": set(range(10)),
    "8410": {0, *range(2, 16)},
    "8420": {0, *range(2, 16)},
}

_TONES = [
    "bell-na.wav", "bell-uk.wav", "buzzer.wav", "chime.wav", "dogs.wav",
    "gong.wav", "page-notif.wav", "speech-test.wav", "tone-1kHz-max.wav",
    "warble1-low.wav", "warble2-med.wav", "warble3-high.wav", "warble4-trill.wav",
]

_CONSOLE_BUTTONS = {"lockdown", "weather-incident", "class-change"}

# Provisioning parameter -> (simulator state key, kind, models, lowest, highest).
_SETTINGS: dict[str, tuple[str, str, set[str], int, int]] = {
    "audio.page.vol": ("page_volume_db", "db", _SPEAKERS | _ADAPTERS, -45, 0),
    "audio.ring.vol": ("ring_volume_db", "db", _SPEAKERS | _ADAPTERS, -45, 0),
    "audio.vol.spk": ("speaker_volume_db", "db", _INTERCOMS | {"8063"}, -45, 0),
    "audio.noise.use": ("noise_compensation", "bool", _SPEAKERS | _ADAPTERS, 0, 0),
    "audio.noise.max": ("noise_max_db", "db", _SPEAKERS | _ADAPTERS, -45, 0),
    "audio.input.gain": ("input_gain_db", "db", _ADAPTERS, -27, 6),
    "audio.mic.mute": ("microphone_mute", "bool", set(_SPEAKERS), 0, 0),
    "admin.web.api": ("api_enabled", "bool", set(_PRODUCTS), 0, 0),
}


def _version(text: Any) -> tuple[int, ...]:
    match = re.match(r"\s*(\d+)\.(\d+)(?:\.(\d+))?", str(text or ""))
    if not match:
        return ()
    return tuple(int(part) for part in match.groups() if part is not None)


def _error(message: str) -> dict[str, str]:
    return {"error": message}


class AlgoIpEndpointSimulator(HTTPSimulator):
    """One Algo IP endpoint's RESTful API (HTTPS)."""

    SIMULATOR_INFO = {
        "driver_id": "algo_ip_endpoint",
        "name": "Algo IP Endpoint Simulator",
        "category": "audio",
        "transport": "http",
        "default_port": 443,
        # HTTPS is always on at the device.
        "tls": True,
        "initial_state": {
            "model": "8410",
            "firmware_version": "5.7.1",
            "mac_address": "00:22:ee:11:22:33",
            "device_name": "hallway-display-1",
            "sip_registration": "Page, Successful;",
            "call_status": "Idle",
            "multicast_mode": "Receiver (Idle)",
            "current_action": "None",
            "relay_input": "Idle",
            "relay_input_1": "Idle",
            "relay_input_2": "Idle",
            "temperature": "41C",
            "action_button": "Idle",
            "ambient_noise": 54,
            "page_volume_db": 0,
            "ring_volume_db": -3,
            "speaker_volume_db": -6,
            "noise_compensation": True,
            "noise_max_db": 0,
            "input_gain_db": 0,
            "microphone_mute": False,
            "api_enabled": True,
            "tone_playing": "",
            "test_active": False,
            "strobe": "",
            "screen": "",
            "screen_text": "",
            "audio_stream_port": 0,
            "emergency_alert": 0,
            "door": "locked",
            "relay_output": False,
            "aux_24v": False,
            "console_events": "",
            "skipped_dates": "",
            "firmware_available": "updated",
        },
        "error_modes": {
            "communication_timeout": {
                "description": "Device stops answering requests",
                "behavior": "no_response",
            },
            "password_changed": {
                "description": "The RESTful API password was changed on the device (every request refused, HTTP 401)",
                "behavior": "custom",
            },
            "clock_wrong": {
                "description": "The device's clock is two minutes fast (NTP off): a Standard signature timestamped in the server's clock is refused",
                "behavior": "custom",
            },
            "api_disabled": {
                "description": "RESTful API turned off under Advanced Settings > Admin (every call 404)",
                "behavior": "custom",
            },
        },
        "controls": [
            {
                "type": "select", "key": "model", "label": "Model",
                "options": sorted(_PRODUCTS),
            },
            {
                "type": "select", "key": "call_status", "label": "Call Status",
                "options": ["Idle", "Ringing", "Connected", "Recording Message", "Playing Delayed Page"],
            },
            {
                "type": "select", "key": "relay_input", "label": "Relay Input",
                "options": ["Idle", "active (Normally Open)", "Disabled"],
            },
            {
                "type": "select", "key": "multicast_mode", "label": "Multicast",
                "options": ["Disabled", "Receiver (Idle)", "Receiver (Active)", "Transmitter (Idle)", "Transmitter (Active)"],
            },
            {"type": "slider", "key": "ambient_noise", "label": "Ambient Noise", "min": 20, "max": 110, "step": 1, "unit": "dB"},
            {"type": "slider", "key": "page_volume_db", "label": "Page Volume", "min": -45, "max": 0, "step": 3, "unit": "dB"},
            {"type": "toggle", "key": "microphone_mute", "label": "Microphone Mute"},
            {
                "type": "group", "label": "What the device is doing",
                "controls": [
                    {"type": "indicator", "key": "tone_playing", "label": "Tone"},
                    {"type": "indicator", "key": "strobe", "label": "Strobe"},
                    {"type": "indicator", "key": "screen", "label": "Screen"},
                    {"type": "indicator", "key": "screen_text", "label": "Screen Text"},
                    {"type": "indicator", "key": "emergency_alert", "label": "Emergency Alert"},
                    {"type": "indicator", "key": "door", "label": "Door"},
                    {"type": "indicator", "key": "console_events", "label": "Console Events"},
                    {"type": "indicator", "key": "skipped_dates", "label": "Skipped Days"},
                ],
            },
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        cfg = config or {}
        self._password = str(cfg.get("password", "") or "")
        method = str(cfg.get("auth_method", "standard") or "standard").lower()
        self._auth_method = method if method in ("standard", "basic", "none") else "standard"
        try:
            self._clock_offset = float(cfg.get("clock_offset_s", 0) or 0)
        except (TypeError, ValueError):
            self._clock_offset = 0.0
        try:
            self._tolerance = float(cfg.get("timestamp_tolerance_s", _TOLERANCE_S) or _TOLERANCE_S)
        except (TypeError, ValueError):
            self._tolerance = float(_TOLERANCE_S)
        for key in ("model", "firmware_version"):
            if cfg.get(key):
                super().set_state(key, str(cfg[key]))
        self.requests: list[tuple[str, str]] = []

    # ── The device's clock and identity ──

    def _now(self) -> float:
        offset = self._clock_offset + (120.0 if "clock_wrong" in self.active_errors else 0.0)
        return time.time() + offset

    def _date_header(self) -> dict[str, str]:
        return {"Date": email.utils.formatdate(self._now(), usegmt=True)}

    def _model(self) -> str:
        return str(self.get_state("model", "8410"))

    def _firmware(self) -> tuple[int, ...]:
        return _version(self.get_state("firmware_version", "5.7.1"))

    # ── Authentication ──

    @staticmethod
    def _header(headers: dict[str, str], name: str) -> str:
        lowered = name.lower()
        for key, value in headers.items():
            if key.lower() == lowered:
                return str(value)
        return ""

    def _password_ok(self, candidate: str) -> bool:
        if "password_changed" in self.active_errors:
            return False
        if self._password:
            return hmac.compare_digest(candidate, self._password)
        return candidate != "invalid"

    def _authorized(self, method: str, uri: str, headers: dict[str, str], body: str) -> bool:
        if self._auth_method == "none":
            return True
        value = self._header(headers, "Authorization").strip()
        if self._auth_method == "basic":
            if not value.startswith("Basic "):
                return False
            try:
                user, _, password = base64.b64decode(value[6:].strip()).decode("utf-8").partition(":")
            except (ValueError, UnicodeDecodeError):
                return False
            return user in ("admin", "algo") and self._password_ok(password)
        match = re.fullmatch(r"hmac (admin|algo):([^:]+):([0-9a-f]{64})", value)
        if not match:
            return False
        nonce, digest = match.group(2), match.group(3)
        stamp = self._header(headers, "Date")
        try:
            timestamp = int(email.utils.parsedate_to_datetime(stamp).timestamp())
        except (TypeError, ValueError, IndexError, OverflowError):
            return False
        if abs(timestamp - self._now()) > self._tolerance:
            return False
        if body:
            content_md5 = self._header(headers, "Content-MD5").strip()
            if content_md5 != hashlib.md5(body.encode("utf-8")).hexdigest():
                return False
            text = f"{method}:{uri}:{content_md5}:application/json:{timestamp}:{nonce}"
        else:
            text = f"{method}:{uri}:{timestamp}:{nonce}"

        def sign(key: str) -> str:
            return hmac.new(key.encode("utf-8"), text.encode("utf-8"), hashlib.sha256).hexdigest()

        if "password_changed" in self.active_errors:
            return False
        if self._password:
            return hmac.compare_digest(digest, sign(self._password))
        return not hmac.compare_digest(digest, sign("invalid"))

    # ── Requests ──

    def handle_request(
        self, method: str, path: str, headers: dict[str, str], body: str,
    ) -> tuple[int, dict | str, dict[str, str]]:
        status, payload = self._route(method, path, headers, body)
        return status, payload, self._date_header()

    def _route(self, method: str, path: str, headers: dict[str, str], body: str) -> tuple[int, dict | str]:
        uri = urlsplit(path).path
        self.requests.append((method, uri))
        if not self._authorized(method, uri, headers, body):
            return 401, _error("Unauthorized")
        if "api_disabled" in self.active_errors or not self.get_state("api_enabled", True):
            return 404, _error("Not Found")
        payload: Any = None
        if body.strip():
            try:
                payload = json.loads(body)
            except ValueError:
                return 400, _error("Invalid JSON")
        model = self._model()
        firmware = self._firmware()

        if method == "GET":
            return self._get(uri, model, firmware)
        if method == "PUT" and uri == "/api/settings":
            return self._put_settings(payload, model)
        if method == "POST":
            return self._post(uri.rstrip("/"), payload if isinstance(payload, dict) else {}, model, firmware)
        return 404, _error("Not Found")

    def _get(self, uri: str, model: str, firmware: tuple[int, ...]) -> tuple[int, dict | str]:
        if uri == "/api/info/about" and firmware >= (5, 4):
            return 200, {
                "Product Name": _PRODUCTS.get(model, f"Algo {model}"),
                "Firmware Version": str(self.get_state("firmware_version")),
                "MAC Address": str(self.get_state("mac_address")),
                "Hardware Info": "Rev 2",
                "Manufacturer Certificate": "Installed",
            }
        if uri == "/api/info/status" and firmware >= (5, 4):
            return 200, self._status_body(model)
        if uri == "/api/info/tonelist" and firmware >= (5, 0):
            return 200, {"tonelist": list(_TONES)}
        if uri == "/api/info/input.relay.status" and model != "8450":
            return 200, {"input.relay.status": str(self.get_state("relay_input"))}
        if uri in ("/api/info/input.relay1.status", "/api/info/input.relay2.status") and model == "8063":
            key = "input.relay1.status" if "relay1" in uri else "input.relay2.status"
            state = "relay_input_1" if "relay1" in uri else "relay_input_2"
            return 200, {key: str(self.get_state(state))}
        if uri == "/api/info/audio.noise.level" and (model in _SPEAKERS or model == "8450"):
            return 200, {"audio.noise.level": str(int(self.get_state("ambient_noise", 0) or 0))}
        if uri == "/api/console/event/status" and model == "8450" and firmware >= (5, 6):
            events = [e for e in str(self.get_state("console_events") or "").split(",") if e]
            return 200, {"active": [{"type": e} for e in events]}
        match = re.fullmatch(r"/api/settings/([A-Za-z0-9_.]+)", uri)
        if match:
            param = match.group(1)
            spec = _SETTINGS.get(param)
            if spec is None or model not in spec[2]:
                return 404, _error("Not Found")
            key, kind, _models, _low, _high = spec
            value = self.get_state(key)
            if kind == "db":
                return 200, {param: f"{int(value or 0)}dB"}
            return 200, {param: "1" if value in (True, 1, "1", "true", "True") else "0"}
        return 404, _error("Not Found")

    def _status_body(self, model: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "Device Name": str(self.get_state("device_name")),
            "SIP Registration": str(self.get_state("sip_registration")),
            "Call Status": str(self.get_state("call_status")),
            "Provisioning Status": "None Found",
            "MAC": str(self.get_state("mac_address")),
            "IPv4": "10.0.0.161/24, Gateway: 10.0.0.1",
            "IPv6": "Not Available",
            "Switch Port ID": "name eth0",
            "Date / Time": time.strftime("%a %b %d %H:%M:%S GMT %Y", time.gmtime(self._now())),
        }
        if model != "8450":
            body["Relay Input Status"] = str(self.get_state("relay_input"))
            body["Multicast Mode"] = str(self.get_state("multicast_mode"))
        if model in _SPEAKERS or model in _ADAPTERS:
            level = int(self.get_state("page_volume_db") or 0) // 3 + 10
            body["Volume"] = f"Page Volume: {level} ({int(self.get_state('page_volume_db') or 0)}dB)"
        if model in _ADAPTERS:
            body["Current Action"] = str(self.get_state("current_action"))
            skipped = str(self.get_state("skipped_dates") or "")
            body["Next Scheduled Event"] = (
                "No Events Scheduled" if skipped else "School Day, 2026-10-08, 08:00, bell-na.wav"
            )
        if model in _STROBES or model in _DISPLAYS:
            body["Temperature"] = str(self.get_state("temperature"))
        if model == "8450":
            events = str(self.get_state("console_events") or "")
            body["Console"] = "Emergency Alert Active" if "emergency" in events else ("Paging with Tone Active" if events else "Idle")
            body["Action Button"] = str(self.get_state("action_button"))
            body["Stand (Docking Station)"] = "Handset connected - Idle"
        return body

    def _put_settings(self, payload: Any, model: str) -> tuple[int, dict | str]:
        if not isinstance(payload, dict) or not payload:
            return 400, _error("Expected a parameter and its value")
        for param, value in payload.items():
            spec = _SETTINGS.get(str(param))
            if spec is None or model not in spec[2]:
                return 400, _error(f"Unknown parameter {param}")
            key, kind, _models, low, high = spec
            text = str(value).strip()
            if kind == "db":
                match = re.fullmatch(r"([+-]?\d+)dB", text)
                if not match or not low <= int(match.group(1)) <= high or (int(match.group(1)) - high) % 3:
                    return 400, _error(f"Invalid value {text} for {param}")
                self.set_state(key, int(match.group(1)))
            else:
                if text not in ("0", "1"):
                    return 400, _error(f"Invalid value {text} for {param}")
                self.set_state(key, text == "1")
        return 200, {}

    def _post(self, uri: str, payload: dict, model: str, firmware: tuple[int, ...]) -> tuple[int, dict | str]:
        has_test = model in _SPEAKERS | _ADAPTERS | _INTERCOMS | _STROBE_ONLY
        has_tone = model in _SPEAKERS | _ADAPTERS | _INTERCOMS
        five_seven = firmware >= (5, 7)

        if uri in ("/api/controls/test/start", "/api/controls/test/loop") and has_test and firmware >= (5, 4):
            self.set_state("test_active", True)
            return 200, ""
        if uri == "/api/controls/test/stop" and has_test and firmware >= (5, 4):
            self.set_state("test_active", False)
            return 200, ""
        if uri == "/api/controls/tone/start" and has_tone and firmware >= (5, 4):
            tone = str(payload.get("path", ""))
            if tone not in _TONES:
                return 400, _error(f"Tone {tone or '(none)'} not found")
            if not isinstance(payload.get("loop"), bool):
                return 400, _error("loop is required")
            mcast = payload.get("mcast") is True
            if mcast and firmware < (5, 6) and model != "8301":
                return 400, _error("mcast is not available on this device")
            where = ""
            if mcast:
                state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
                if state.get("mode") != "sender" or not state.get("address") or not state.get("port"):
                    return 400, _error("Multicast state needs mode, address and port")
                where = f" to {state.get('address')}:{state.get('port')}"
            looping = " (loop)" if payload["loop"] else ""
            limit = f" for {payload['duration']}s" if five_seven and payload.get("duration") else ""
            self.set_state("tone_playing", f"{tone}{looping}{where}{limit}")
            return 200, ""
        if uri == "/api/controls/tone/stop" and has_tone:
            if firmware < (5, 5) and not payload.get("path"):
                return 400, _error("path is required")
            self.set_state("tone_playing", "")
            return 200, ""
        if uri == "/api/controls/rx/start" and model != "8450" and firmware >= (5, 3, 4):
            try:
                port = int(str(payload.get("port", "")))
            except ValueError:
                return 400, _error("port is required")
            self.set_state("audio_stream_port", port)
            return 200, ""
        if uri == "/api/controls/rx/stop" and model != "8450" and firmware >= (5, 3, 4):
            self.set_state("audio_stream_port", 0)
            return 200, ""
        if uri == "/api/controls/noise/update" and (model in _SPEAKERS or model in _ADAPTERS or model == "8450") and firmware >= (5, 4):
            try:
                self.set_state("ambient_noise", int(str(payload.get("level", ""))))
            except ValueError:
                return 400, _error("level is required")
            return 200, ""
        if uri == "/api/controls/call/start" and (model in _SPEAKERS or model in _ADAPTERS or model == "8063"):
            if not payload.get("extension") or not payload.get("tone"):
                return 400, _error("extension and tone are required")
            self.set_state("call_status", "Connected")
            return 200, ""
        if uri == "/api/controls/call/stop" and (model in _SPEAKERS or model in _ADAPTERS or model == "8063"):
            self.set_state("call_status", "Idle")
            return 200, ""
        if uri == "/api/controls/call/page" and model in _SPEAKERS and firmware >= (5, 3, 4):
            if not payload.get("extension"):
                return 400, _error("extension is required")
            self.set_state("call_status", "Connected")
            return 200, ""
        if uri == "/api/controls/emergency-alert/start" and (model in _SPEAKERS | _ADAPTERS | _STROBE_ONLY) and five_seven:
            try:
                number = int(str(payload.get("announcement", "")))
            except ValueError:
                return 400, _error("announcement is required")
            if not 1 <= number <= 10:
                return 400, _error("announcement must be 1 to 10")
            self.set_state("emergency_alert", number)
            return 200, ""
        if uri == "/api/controls/emergency-alert/stop" and (model in _SPEAKERS | _ADAPTERS | _STROBE_ONLY) and five_seven:
            self.set_state("emergency_alert", 0)
            return 200, ""
        if uri == "/api/controls/strobe/start" and model in _STROBES:
            return self._strobe(payload, model, five_seven)
        if uri == "/api/controls/strobe/stop" and model in _STROBES:
            self.set_state("strobe", "")
            return 200, ""
        if uri == "/api/controls/screen/start" and model in _DISPLAYS and firmware >= (5, 3, 4):
            return self._screen(payload, five_seven)
        if uri == "/api/controls/screen/stop" and model in _DISPLAYS and firmware >= (5, 3, 4):
            self.set_state("screen", "")
            return 200, ""
        if uri == "/api/controls/screen-text/start" and model in _DISPLAYS and five_seven:
            text = str(payload.get("textContent", ""))
            if not text:
                return 400, _error("textContent is required")
            self.set_state("screen_text", text)
            return 200, ""
        if uri == "/api/controls/screen-text/stop" and model in _DISPLAYS and five_seven:
            self.set_state("screen_text", "")
            return 200, ""
        if uri in ("/api/controls/door/lock", "/api/controls/door/unlock", "/api/controls/door/munlock") and model in _DOORS:
            door = payload.get("doorid")
            if door not in ("local", "netdc1"):
                return 400, _error("doorid must be local or netdc1")
            if uri.endswith("/munlock"):
                if firmware < (5, 5):
                    return 404, _error("Not Found")
                try:
                    seconds = int(str(payload.get("duration", "")))
                except ValueError:
                    return 400, _error("duration is required")
                self.set_state("door", f"unlocked for {seconds}s ({door})")
            else:
                self.set_state("door", ("locked" if uri.endswith("/lock") else "unlocked") + f" ({door})")
            return 200, ""
        if model == "8063" and uri in ("/api/controls/relay/enable", "/api/controls/relay/disable") and firmware >= (5, 0):
            self.set_state("relay_output", uri.endswith("/enable"))
            return 200, ""
        if model == "8063" and uri == "/api/controls/relay/menable" and firmware >= (5, 6):
            if not isinstance(payload.get("duration"), int):
                return 400, _error("duration is required")
            self.set_state("relay_output", True)
            return 200, ""
        if model == "8063" and uri in ("/api/controls/24v/enable", "/api/controls/24v/disable") and firmware >= (5, 0):
            self.set_state("aux_24v", uri.endswith("/enable"))
            return 200, ""
        if model == "8450" and uri == "/api/controls/console/button/activate" and firmware >= (5, 6):
            button = str(payload.get("id", ""))
            if button not in _CONSOLE_BUTTONS:
                return 404, _error(f"No button with identifier {button}")
            events = [e for e in str(self.get_state("console_events") or "").split(",") if e]
            kind = "emergency" if button == "lockdown" else "pageTone"
            if kind not in events:
                events.append(kind)
            self.set_state("console_events", ",".join(events))
            return 200, ""
        if model == "8450" and uri == "/api/controls/console/event/stop" and firmware >= (5, 6):
            kind = str(payload.get("type", ""))
            events = [e for e in str(self.get_state("console_events") or "").split(",") if e]
            kept = [] if kind == "all" else [e for e in events if e != kind]
            self.set_state("console_events", ",".join(kept))
            return 200, f"Stopped {len(events) - len(kept)} event(s)."
        if uri == "/api/schedules" and model in _ADAPTERS and five_seven:
            return self._schedules(payload)
        if uri == "/api/controls/upgrade/check" and firmware >= (4, 1):
            return 200, {"version": str(self.get_state("firmware_available"))}
        if uri == "/api/controls/upgrade/start" and firmware >= (4, 1):
            available = str(self.get_state("firmware_available"))
            return 200, {"status": "updated" if available == "updated" else f"upgrading {available}"}
        if uri in ("/api/controls/reboot", "/api/controls/reload"):
            return 200, ""
        if uri == "/api/settings/action/restore" and firmware >= (5, 4):
            # A factory reset turns the RESTful API off with everything else.
            self.set_state("api_enabled", False)
            return 200, {"Restore": "Restoring to default"}
        return 404, _error("Not Found")

    def _strobe(self, payload: dict, model: str, five_seven: bool) -> tuple[int, dict | str]:
        pattern = payload.get("pattern")
        if not isinstance(pattern, int) or pattern not in _PATTERNS[model]:
            return 400, _error(f"Pattern {pattern} is not available on the {model}")
        colors = {"red", "blue", "green", "amber"}
        if payload.get("color1") not in colors:
            return 400, _error("color1 must be red, blue, green or amber")
        if "color2" in payload and payload["color2"] not in colors:
            return 400, _error("color2 must be red, blue, green or amber")
        levels = {1, 2, 3} if model == "8190S" else {0, 56, 255}
        try:
            level = int(str(payload.get("ledlvl", "")))
        except ValueError:
            return 400, _error("ledlvl is required")
        if level not in levels:
            return 400, _error(f"ledlvl {level} is not a brightness the {model} has")
        limit = f" for {payload['duration']}s" if five_seven and payload.get("duration") else ""
        second = f"/{payload['color2']}" if payload.get("color2") else ""
        self.set_state("strobe", f"pattern {pattern} {payload['color1']}{second} at {level}{limit}")
        return 200, ""

    def _screen(self, payload: dict, five_seven: bool) -> tuple[int, dict | str]:
        kind = payload.get("type")
        if kind is None and payload.get("screenName"):
            shown = f"slide {payload['screenName']}"
        elif kind is None and payload.get("slideNames"):
            if not isinstance(payload.get("duration"), int):
                return 400, _error("duration is required")
            shown = f"slideshow {payload['slideNames']}"
        elif kind == "image":
            if not payload.get("image1"):
                return 400, _error("image1 is required")
            shown = f"image {payload['image1']}"
            if payload.get("text1"):
                shown += f" with text {payload['text1']}"
        elif kind in ("digitalClock", "analogClock"):
            shown = "digital clock" if kind == "digitalClock" else "analog clock"
        elif kind == "blitz":
            if not payload.get("image1") or not payload.get("image2"):
                return 400, _error("image1 and image2 are required")
            shown = f"flashing {payload['image1']} / {payload['image2']}"
        elif kind == "template":
            if not payload.get("template"):
                return 400, _error("template is required")
            shown = f"template {payload['template']}"
        else:
            return 400, _error(f"Unknown screen type {kind}")
        if five_seven and payload.get("stopAfter"):
            shown += f" for {payload['stopAfter']}s"
        self.set_state("screen", shown)
        return 200, ""

    def _schedules(self, payload: dict) -> tuple[int, dict | str]:
        skipped = {d for d in str(self.get_state("skipped_dates") or "").split(",") if d}
        answer: dict[str, Any] = {}
        for operation in ("skip", "remove_skip"):
            entries = payload.get(operation)
            if entries is None:
                continue
            if not isinstance(entries, list):
                return 400, _error(f"{operation} must be a list")
            for entry in entries:
                day = str(entry.get("date", "")) if isinstance(entry, dict) else ""
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                    return 400, _error("date must be YYYY-MM-DD")
                if operation == "skip":
                    skipped.add(day)
                else:
                    skipped.discard(day)
            answer[operation] = [
                {**entry, "affected_events": [{"evid": 2, "name": "Morning Bell", "skipped": 1 if operation == "skip" else 0}]}
                for entry in entries
            ]
        if not answer:
            return 400, _error("Nothing to do")
        self.set_state("skipped_dates", ",".join(sorted(skipped)))
        return 200, answer
