"""
Sennheiser TeamConnect Ceiling Medium (TCC M) — Simulator

Simulates the microphone's SSCv2 REST API: HTTPS on 443 (the simulator
terminates TLS with a throwaway self-signed certificate, as the real device
does with its own), HTTP Basic authentication as the ``api`` user, JSON
bodies, and the subscription stream. Every resource of the TCC M OpenAPI 1.9
schema is served with the schema's ranges, enumerations and refusals:

  - 401 without credentials, 403 while third-party access is off (an error
    mode), 404 for an unknown resource, 405 for a method a resource lacks;
  - 400 for a body that is not JSON or names a field the resource lacks,
    422 for a value out of range, off its step, or an unknown enumeration
    (the spec's "Unprocessable Value");
  - 409 for a manual reference gain while automatic adjustment is on, 422
    for both in one request, 422 for an exclusion zone narrower than 10
    degrees or a priority zone narrower than 15;
  - identify on puts the device state to Identifying and back.

Subscriptions follow the SSCv2 specification: GET /api/ssc/state/subscriptions
opens an event stream whose Content-Location names the session, the stream's
first event is ``open`` carrying the session UUID, PUT of a resource list to
the session (or to /add and /remove) arms it, and the current value of every
newly subscribed resource is sent at once. A path the device does not have
refuses the whole list with 400 naming it; an unknown session is 422; DELETE
sends ``close`` and ends the stream. A change to any subscribed resource,
from the API or from the Simulator UI, is pushed as ``{"<path>": {...}}``.

Driver: sennheiser_tccm
Transport: http (HTTPS)
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid as uuidlib
from typing import Any

from openavc.simulator.http_simulator import HTTPSimulator

_SUBSCRIPTIONS = "/api/ssc/state/subscriptions"
_API_USER = "api"
_VENDOR = "Sennheiser electronic GmbH & Co. KG"

_LED_COLORS = [
    "LightGreen", "Green", "Blue", "Red", "Yellow", "Orange", "Cyan", "Pink",
]

# Field rules, keyed by dotted field path within a resource body:
#   ("bool",)                 boolean
#   ("int", lo, hi[, step])   integer within lo..hi, on the step when given
#   ("num", lo, hi, step)     number within lo..hi, on the step
#   ("enum", [...])           one of the strings
#   ("str",)                  free string
# Each maps to the simulator state key that holds it.
_Field = tuple[str, str, tuple]


def _zone(prefix: str) -> list[_Field]:
    return [
        ("enabled", f"{prefix}_enabled", ("bool",)),
        ("azimuth.min", f"{prefix}_azimuth_min", ("int", 0, 360, 5)),
        ("azimuth.max", f"{prefix}_azimuth_max", ("int", 0, 360, 5)),
        ("elevation.min", f"{prefix}_elevation_min", ("int", 0, 90, 5)),
        ("elevation.max", f"{prefix}_elevation_max", ("int", 0, 90, 5)),
    ]


# path -> (writable, fields)
_RESOURCES: dict[str, tuple[bool, list[_Field]]] = {
    "/api/device/identity": (False, [
        ("product", "product", ("str",)),
        ("hardwareRevision", "hardware_revision", ("str",)),
        ("serial", "serial", ("str",)),
    ]),
    "/api/device/identification": (True, [
        ("visual", "identify_active", ("bool",)),
    ]),
    "/api/device/site": (False, [
        ("deviceName", "device_name", ("str",)),
        ("location", "location", ("str",)),
        ("position", "position", ("str",)),
    ]),
    "/api/device/state": (False, [
        ("state", "device_state", ("enum", ["Normal", "Identifying", "FirmwareUpdate"])),
    ]),
    "/api/firmware/update/state": (False, [
        ("deviceVersion", "firmware_version", ("str",)),
        ("danteVersion", "dante_version", ("str",)),
        ("state", "update_state", ("enum", ["Updating", "Idle"])),
        ("progress", "update_progress", ("int", 0, 100)),
        ("lastStatus", "update_last_status", ("enum", ["None", "ChecksumError"])),
    ]),
    "/api/audio/outputs/global/mute": (True, [
        ("enabled", "mute", ("bool",)),
    ]),
    "/api/audio/inputs/microphone/beam": (True, [
        ("installationType", "installation_type",
         ("enum", ["FlushMounted", "SurfaceMounted", "Suspended"])),
        ("sourceDetectionThreshold", "source_detection_threshold",
         ("enum", ["QuietRoom", "NormalRoom", "LoudRoom"])),
        ("offset", "beam_offset", ("int", 0, 330, 30)),
    ]),
    "/api/audio/inputs/microphone/beam/direction": (False, [
        ("azimuth", "beam_azimuth", ("int", 0, 360)),
        ("elevation", "beam_elevation", ("int", 0, 90)),
        ("beamFreezeActive", "beam_freeze_active", ("bool",)),
    ]),
    "/api/audio/inputs/microphone/level": (False, [
        ("peak", "mic_peak_db", ("int", -90, 0)),
    ]),
    "/api/audio/inputs/reference/level": (False, [
        ("rms", "reference_rms_dbfs", ("int", -120, 0)),
    ]),
    "/api/audio/roomInUse": (False, [
        ("active", "room_in_use", ("bool",)),
    ]),
    "/api/audio/roomInUse/activityLevel": (False, [
        ("peak", "room_activity_db", ("int", 0, 90)),
    ]),
    "/api/audio/roomInUse/config": (False, [
        ("triggerTime", "room_in_use_trigger_s", ("int", 1, 20)),
        ("releaseTime", "room_in_use_release_s", ("int", 5, 600)),
        ("threshold", "room_in_use_threshold_db", ("int", 0, 36)),
    ]),
    "/api/audio/outputs/analog": (True, [
        ("gain", "analog_gain_db", ("int", -18, 0)),
        ("switch", "analog_output_source", ("enum", ["FarendOutput", "LocalOutput"])),
    ]),
    "/api/audio/outputs/dante/farEnd": (True, [
        ("gain", "farend_gain_db", ("int", 0, 24)),
        ("noiseGateEnabled", "farend_noise_gate", ("bool",)),
        ("equalizerEnabled", "farend_equalizer", ("bool",)),
        ("delay", "farend_delay_ms", ("int", 0, 100)),
    ]),
    "/api/audio/voiceLift": (True, [
        ("emergencyMuteThreshold", "voice_lift_mute_threshold_db", ("int", -50, -3)),
        ("emergencyMuteTime", "voice_lift_mute_time_s", ("int", 1, 30)),
    ]),
    "/api/audio/outputs/dante/local": (True, [
        ("gain", "local_gain_db", ("int", 0, 24)),
        ("noiseGateEnabled", "local_noise_gate", ("bool",)),
        ("equalizerEnabled", "local_equalizer", ("bool",)),
        ("voiceLiftEnabled", "local_voice_lift", ("bool",)),
        ("delay", "local_delay_ms", ("int", 0, 100)),
    ]),
    "/api/audio/inputs/dante/reference": (True, [
        ("gain", "reference_gain_db", ("int", -60, 10)),
        ("farEndAutoAdjustEnabled", "reference_auto_adjust", ("bool",)),
    ]),
    "/api/audio/equalizer": (True, [
        (f"gains.{i}", key, ("int", -8, 8))
        for i, key in enumerate(
            ["eq_125_db", "eq_250_db", "eq_500_db", "eq_1k_db",
             "eq_2k_db", "eq_4k_db", "eq_8k_db"]
        )
    ]),
    "/api/audio/noiseGate": (True, [
        ("threshold", "noise_gate_threshold_db", ("int", -90, -40)),
        ("holdTime", "noise_gate_hold_ms", ("int", 50, 1000)),
    ]),
    "/api/device/leds/ring": (True, [
        ("brightness", "led_brightness", ("int", 0, 5)),
        ("showFarendActivity", "led_show_farend_activity", ("bool",)),
        ("micOn.color", "led_mic_on_color", ("enum", _LED_COLORS)),
        ("micMute.color", "led_mic_mute_color", ("enum", _LED_COLORS)),
        ("micCustom.enabled", "led_custom_enabled", ("bool",)),
        ("micCustom.color", "led_custom_color", ("enum", _LED_COLORS)),
    ]),
    "/api/device/power/poe/daisychain": (False, [
        ("sufficientPower", "poe_sufficient_power", ("bool",)),
        ("inUse", "poe_output_in_use", ("bool",)),
    ]),
    "/api/audio/inputs/microphone/denoiser": (True, [
        ("setting", "denoiser", ("enum", ["Off", "Low", "Medium", "High"])),
    ]),
    "/api/audio/inputs/microphone/inputLowcuts": (True, [
        ("enabled", "input_lowcuts", ("bool",)),
    ]),
    "/api/audio/inputs/microphone/singleCapsuleMode": (True, [
        ("enabled", "single_capsule_mode", ("bool",)),
    ]),
    "/api/audio/inputs/microphone/beam/beamfreeze/autoHold": (True, [
        ("enabled", "beam_freeze_auto_hold", ("bool",)),
        ("holdTime", "beam_freeze_hold_ms", ("int", 50, 500)),
    ]),
    "/api/audio/inputs/microphone/priorityZones/0": (True, [
        ("weight", "priority_zone_weight", ("num", 1.0, 4.0, 0.1)),
        *_zone("priority_zone"),
    ]),
}
for _n in range(5):
    _RESOURCES[f"/api/audio/inputs/microphone/exclusionZones/{_n}"] = (
        True, _zone(f"exclusion_zone_{_n + 1}"),
    )

_EXCLUSION_COLLECTION = "/api/audio/inputs/microphone/exclusionZones"
_PRIORITY_COLLECTION = "/api/audio/inputs/microphone/priorityZones"

# Resources that answer without credentials (their OpenAPI entries declare
# no security scheme).
_OPEN_PATHS = {
    "/api/ssc/version",
    "/api/device/identity",
    "/api/device/identification",
    "/api/device/licenseAgreements/hash",
}

_KEY_TO_PATH: dict[str, str] = {}
for _path, (_w, _fields) in _RESOURCES.items():
    for _field, _key, _spec in _fields:
        _KEY_TO_PATH[_key] = _path


def _nest_set(body: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur: Any = body
    for seg in parts[:-1]:
        if seg.isdigit():
            idx = int(seg)
            while len(cur) <= idx:
                cur.append(None)
            if cur[idx] is None:
                cur[idx] = {}
            cur = cur[idx]
        else:
            cur = cur.setdefault(seg, {})
    last = parts[-1]
    if last.isdigit():
        idx = int(last)
        while len(cur) <= idx:
            cur.append(None)
        cur[idx] = value
    else:
        cur[last] = value


def _dig(obj: Any, path: str) -> tuple[bool, Any]:
    """(present, value) for a dotted path in a request body."""
    cur = obj
    for seg in path.split("."):
        if isinstance(cur, dict):
            if seg not in cur:
                return False, None
            cur = cur[seg]
        elif isinstance(cur, list) and seg.isdigit():
            if int(seg) >= len(cur):
                return False, None
            cur = cur[int(seg)]
        else:
            return False, None
    return True, cur


def _leaf_paths(obj: Any, prefix: str = "") -> list[str]:
    """Every dotted leaf path in a request body, for spotting unknown fields."""
    out: list[str] = []
    if isinstance(obj, dict):
        for key, val in obj.items():
            out.extend(_leaf_paths(val, f"{prefix}{key}."))
    elif isinstance(obj, list):
        for i, val in enumerate(obj):
            out.extend(_leaf_paths(val, f"{prefix}{i}."))
    else:
        out.append(prefix.rstrip("."))
    return out


class SennheiserTccmSimulator(HTTPSimulator):
    """Sennheiser TeamConnect Ceiling Medium over SSCv2 (HTTPS + SSE)."""

    SIMULATOR_INFO = {
        "driver_id": "sennheiser_tccm",
        "name": "Sennheiser TeamConnect Ceiling Medium Simulator",
        "category": "audio",
        "transport": "http",
        "default_port": 443,
        # The real microphone speaks HTTPS only.
        "tls": True,
        "initial_state": {
            "product": "TCCM",
            "hardware_revision": "1",
            "serial": "1023456789",
            "firmware_version": "1.9.2",
            "dante_version": "4.2.6.4",
            "device_name": "TCCM",
            "location": "Room",
            "position": "over central table",
            "device_state": "Normal",
            "update_state": "Idle",
            "update_progress": 0,
            "update_last_status": "None",
            "identify_active": False,
            "mute": False,
            "installation_type": "FlushMounted",
            "source_detection_threshold": "NormalRoom",
            "beam_offset": 0,
            "beam_azimuth": 180,
            "beam_elevation": 45,
            "beam_freeze_active": False,
            "mic_peak_db": -42,
            "reference_rms_dbfs": -60,
            "room_in_use": False,
            "room_activity_db": 0,
            "room_in_use_trigger_s": 15,
            "room_in_use_release_s": 300,
            "room_in_use_threshold_db": 10,
            "analog_gain_db": 0,
            "analog_output_source": "LocalOutput",
            "farend_gain_db": 12,
            "farend_noise_gate": False,
            "farend_equalizer": False,
            "farend_delay_ms": 0,
            "voice_lift_mute_threshold_db": -20,
            "voice_lift_mute_time_s": 3,
            "local_gain_db": 12,
            "local_noise_gate": False,
            "local_equalizer": False,
            "local_voice_lift": False,
            "local_delay_ms": 0,
            "reference_gain_db": 0,
            "reference_auto_adjust": True,
            "eq_125_db": 0,
            "eq_250_db": 0,
            "eq_500_db": 0,
            "eq_1k_db": 0,
            "eq_2k_db": 0,
            "eq_4k_db": 0,
            "eq_8k_db": 0,
            "noise_gate_threshold_db": -80,
            "noise_gate_hold_ms": 350,
            "led_brightness": 5,
            "led_show_farend_activity": False,
            "led_mic_on_color": "Green",
            "led_mic_mute_color": "Red",
            "led_custom_enabled": False,
            "led_custom_color": "Green",
            "poe_sufficient_power": True,
            "poe_output_in_use": False,
            "denoiser": "Off",
            "input_lowcuts": True,
            "single_capsule_mode": False,
            "beam_freeze_auto_hold": True,
            "beam_freeze_hold_ms": 100,
            # The schema's default zone table.
            "exclusion_zone_1_enabled": True,
            "exclusion_zone_1_azimuth_min": 0,
            "exclusion_zone_1_azimuth_max": 360,
            "exclusion_zone_1_elevation_min": 0,
            "exclusion_zone_1_elevation_max": 10,
            "exclusion_zone_2_enabled": False,
            "exclusion_zone_2_azimuth_min": 20,
            "exclusion_zone_2_azimuth_max": 70,
            "exclusion_zone_2_elevation_min": 10,
            "exclusion_zone_2_elevation_max": 50,
            "exclusion_zone_3_enabled": False,
            "exclusion_zone_3_azimuth_min": 110,
            "exclusion_zone_3_azimuth_max": 160,
            "exclusion_zone_3_elevation_min": 10,
            "exclusion_zone_3_elevation_max": 50,
            "exclusion_zone_4_enabled": False,
            "exclusion_zone_4_azimuth_min": 200,
            "exclusion_zone_4_azimuth_max": 250,
            "exclusion_zone_4_elevation_min": 10,
            "exclusion_zone_4_elevation_max": 50,
            "exclusion_zone_5_enabled": False,
            "exclusion_zone_5_azimuth_min": 290,
            "exclusion_zone_5_azimuth_max": 340,
            "exclusion_zone_5_elevation_min": 10,
            "exclusion_zone_5_elevation_max": 50,
            "priority_zone_enabled": False,
            "priority_zone_weight": 1.5,
            "priority_zone_azimuth_min": 160,
            "priority_zone_azimuth_max": 200,
            "priority_zone_elevation_min": 60,
            "priority_zone_elevation_max": 80,
        },
        "delays": {
            "command_response": 0.02,
        },
        "error_modes": {
            "communication_timeout": {
                "description": "Microphone stops answering requests",
                "behavior": "no_response",
            },
            "wrong_password": {
                "description": "Microphone rejects every credential (HTTP 401)",
                "behavior": "custom",
            },
            "third_party_disabled": {
                "description": "Third-party access switched off in Control Cockpit (HTTP 403)",
                "behavior": "custom",
            },
            "firmware_update": {
                "description": "A firmware update is running",
                "behavior": "custom",
                "set_state": {"device_state": "FirmwareUpdate", "update_state": "Updating"},
            },
        },
        "controls": [
            {"type": "toggle", "key": "mute", "label": "Mute"},
            {"type": "toggle", "key": "identify_active", "label": "Identify"},
            {"type": "toggle", "key": "room_in_use", "label": "Room In Use"},
            {
                "type": "slider", "key": "beam_azimuth", "label": "Talker Azimuth",
                "min": 0, "max": 360,
            },
            {
                "type": "slider", "key": "beam_elevation", "label": "Talker Elevation",
                "min": 0, "max": 90,
            },
            {"type": "toggle", "key": "beam_freeze_active", "label": "Beam Frozen"},
            {
                "type": "slider", "key": "mic_peak_db", "label": "Mic Peak (dB)",
                "min": -90, "max": 0,
            },
            {
                "type": "slider", "key": "reference_rms_dbfs", "label": "Reference RMS (dBFS)",
                "min": -120, "max": 0,
            },
            {
                "type": "slider", "key": "room_activity_db", "label": "Room Activity (dB)",
                "min": 0, "max": 90,
            },
            {"type": "toggle", "key": "poe_output_in_use", "label": "PoE Output In Use"},
            {"type": "indicator", "key": "device_state", "label": "Device State"},
            {"type": "indicator", "key": "led_brightness", "label": "LED Brightness"},
            {"type": "indicator", "key": "denoiser", "label": "Denoiser"},
            {"type": "indicator", "key": "farend_gain_db", "label": "Far-end Gain (dB)"},
            {"type": "indicator", "key": "local_gain_db", "label": "Local Gain (dB)"},
            {"type": "indicator", "key": "firmware_version", "label": "Firmware"},
            {"type": "indicator", "key": "serial", "label": "Serial"},
        ],
    }

    # The subscription stream endpoint, held open by the HTTP base.
    sse_paths = [_SUBSCRIPTIONS]

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # session uuid -> {"queue": asyncio.Queue[str | None], "paths": set}
        self._sessions: dict[str, dict[str, Any]] = {}
        # A password to insist on; empty accepts anything but "invalid".
        self._password = str((config or {}).get("password", "") or "")
        # Resources an older firmware lacks (404 on read, refused in a
        # subscription list): ``"hidden_paths": [...]`` in the config.
        self._hidden: set[str] = {
            self._normalize(str(p)) for p in (config or {}).get("hidden_paths", []) or []
        }

    # ── Authentication ──

    def _authorized(self, headers: dict[str, str]) -> bool:
        if "wrong_password" in self.active_errors:
            return False
        header = ""
        for name, value in headers.items():
            if name.lower() == "authorization":
                header = value
                break
        if not header.lower().startswith("basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        user, _, password = decoded.partition(":")
        if user != _API_USER or not password or password == "invalid":
            return False
        if self._password and password != self._password:
            return False
        return True

    def _auth_error(self, path: str) -> tuple[int, dict] | None:
        if "third_party_disabled" in self.active_errors and path not in _OPEN_PATHS:
            return 403, {"error": "third-party access is disabled"}
        return None

    # ── Bodies ──

    def _body(self, path: str) -> dict[str, Any]:
        _writable, fields = _RESOURCES[path]
        body: dict[str, Any] = {}
        if path == "/api/audio/equalizer":
            body["gains"] = []
        for field, key, _spec in fields:
            _nest_set(body, field, self.get_state(key))
        if path == "/api/device/identity":
            body["vendor"] = _VENDOR
        if path == "/api/device/state":
            raw = str(self.get_state("warnings", "") or "")
            body["warnings"] = [w.strip() for w in raw.split(";") if w.strip()]
        return body

    def _zone_entry(self, path: str, zone_id: int) -> dict[str, Any]:
        return {"id": zone_id, **self._body(path)}

    def _collection_body(self, collection: str) -> list[dict[str, Any]]:
        if collection == _EXCLUSION_COLLECTION:
            return [self._zone_entry(f"{collection}/{n}", n) for n in range(5)]
        return [self._zone_entry(f"{collection}/0", 0)]

    # ── Requests ──

    def handle_request(
        self, method: str, path: str, headers: dict[str, str], body: str,
    ) -> tuple[int, dict | str] | tuple[int, dict | str, dict[str, str]]:
        path = path.split("?", 1)[0]
        if path not in _OPEN_PATHS and not self._authorized(headers):
            return 401, {"error": "unauthorized"}
        refused = self._auth_error(path)
        if refused is not None:
            return refused

        if path == "/api/ssc/version":
            if method != "GET":
                return 405, {"error": "method not allowed"}
            return 200, {"protocol": "2.3", "schema": "1.9"}
        if path == "/api/device/licenseAgreements/hash":
            return 200, {"value": "e4491bb82bf36d124f92d4bba1edac60f6178e92a3b89c436fb660b99c10d538"}

        if path.startswith(_SUBSCRIPTIONS):
            return self._handle_subscription(method, path, body)

        if path in (_EXCLUSION_COLLECTION, _PRIORITY_COLLECTION):
            if method != "GET":
                return 405, {"error": "method not allowed"}
            # A bare JSON array; the HTTP base only encodes dicts itself.
            return 200, json.dumps(self._collection_body(path))

        if path not in _RESOURCES or path in self._hidden:
            return 404, {"error": "not found"}
        writable, _fields = _RESOURCES[path]
        if method == "GET":
            return 200, self._body(path)
        if method != "PUT" or not writable:
            return 405, {"error": "method not allowed"}
        return self._handle_put(path, body)

    def _handle_put(self, path: str, raw: str) -> tuple[int, dict]:
        try:
            payload = json.loads(raw) if raw.strip() else None
        except ValueError:
            return 400, {"error": "malformed JSON"}
        if not isinstance(payload, dict):
            return 400, {"error": "a JSON object is required"}
        _writable, fields = _RESOURCES[path]
        known = {field for field, _key, _spec in fields}
        for leaf in _leaf_paths(payload):
            if leaf not in known:
                return 400, {"error": f"unknown field {leaf}"}

        writes: dict[str, Any] = {}
        for field, key, spec in fields:
            present, value = _dig(payload, field)
            if not present:
                continue
            ok, coerced, status = self._check(spec, value)
            if not ok:
                return status, {"error": f"{field}: {coerced}"}
            writes[key] = coerced
        if path == "/api/audio/equalizer":
            gains = payload.get("gains")
            if not isinstance(gains, list) or len(gains) != 7:
                return 400, {"error": "gains must hold seven values"}

        # The device's own cross-field rules.
        if path == "/api/audio/inputs/dante/reference":
            wants_gain = "reference_gain_db" in writes
            auto_now = bool(self.get_state("reference_auto_adjust"))
            if wants_gain and writes.get("reference_auto_adjust") is True:
                return 422, {"error": "manual gain conflicts with farEndAutoAdjustEnabled"}
            if wants_gain and auto_now and "reference_auto_adjust" not in writes:
                return 409, {"error": "manual gain while farEndAutoAdjustEnabled is true"}
        if path.startswith(_EXCLUSION_COLLECTION) or path.startswith(_PRIORITY_COLLECTION):
            min_width = 15 if path.startswith(_PRIORITY_COLLECTION) else 10
            prefix = self._zone_prefix(path)
            merged = {
                axis: (
                    writes.get(f"{prefix}_{axis}_min", self.get_state(f"{prefix}_{axis}_min")),
                    writes.get(f"{prefix}_{axis}_max", self.get_state(f"{prefix}_{axis}_max")),
                )
                for axis in ("azimuth", "elevation")
            }
            for axis, (lo, hi) in merged.items():
                if int(hi) - int(lo) < min_width:
                    return 422, {
                        "error": f"{axis} width must be at least {min_width} degrees",
                    }

        for key, value in writes.items():
            self.set_state(key, value)
        return 200, {}

    @staticmethod
    def _zone_prefix(path: str) -> str:
        if path.startswith(_PRIORITY_COLLECTION):
            return "priority_zone"
        return f"exclusion_zone_{int(path.rsplit('/', 1)[-1]) + 1}"

    @staticmethod
    def _check(spec: tuple, value: Any) -> tuple[bool, Any, int]:
        """(ok, coerced value or reason, status when refused)."""
        kind = spec[0]
        if kind == "bool":
            if isinstance(value, bool):
                return True, value, 200
            return False, "a boolean is required", 400
        if kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                return False, "an integer is required", 400
            lo, hi = spec[1], spec[2]
            if not lo <= value <= hi:
                return False, f"out of range {lo}..{hi}", 422
            if len(spec) > 3 and value % spec[3] != 0:
                return False, f"must be a multiple of {spec[3]}", 422
            return True, value, 200
        if kind == "num":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False, "a number is required", 400
            lo, hi, step = spec[1], spec[2], spec[3]
            if not lo <= value <= hi:
                return False, f"out of range {lo}..{hi}", 422
            if abs(round(value / step) * step - value) > 1e-6:
                return False, f"must be a multiple of {step}", 422
            return True, round(float(value), 1), 200
        if kind == "enum":
            if isinstance(value, str) and value in spec[1]:
                return True, value, 200
            return False, "unknown value", 422
        if isinstance(value, str):
            return True, value, 200
        return False, "a string is required", 400

    # ── Subscriptions ──

    def _handle_subscription(self, method: str, path: str, raw: str) -> tuple[int, dict]:
        rest = path[len(_SUBSCRIPTIONS):].strip("/")
        if not rest:
            # GET without Accept: text/event-stream reached the plain handler.
            return 400, {"error": "Accept: text/event-stream is required"}
        parts = rest.split("/")
        session = self._sessions.get(parts[0])
        if session is None:
            return 422, {"error": "unknown sessionUUID"}
        action = parts[1] if len(parts) > 1 else ""
        if method == "GET" and not action:
            return 200, json.dumps(sorted(session["paths"]))
        if method == "DELETE" and not action:
            self.close_session(parts[0], notify=True)
            return 200, {}
        if method != "PUT" or action not in ("", "add", "remove"):
            return 405, {"error": "method not allowed"}
        try:
            wanted = json.loads(raw) if raw.strip() else []
        except ValueError:
            return 400, {"error": "malformed JSON"}
        if not isinstance(wanted, list) or not all(isinstance(p, str) for p in wanted):
            return 400, {"error": "a list of resource paths is required"}
        wanted = [self._normalize(p) for p in wanted]
        if action == "remove":
            for p in wanted:
                if p not in session["paths"]:
                    return 400, {"path": p, "error": 404}
            session["paths"].difference_update(wanted)
            return 200, {}
        for p in wanted:
            if not self._subscribable(p):
                return 400, {"path": p, "error": 404}
        if action == "add":
            new = [p for p in wanted if p not in session["paths"]]
            session["paths"].update(wanted)
        else:
            new = [p for p in wanted if p not in session["paths"]]
            session["paths"] = set(wanted)
        for p in new:
            self._push(session, p)
        return 200, {}

    @staticmethod
    def _normalize(path: str) -> str:
        path = path.rstrip("/")
        if not path.startswith("/api/"):
            path = "/api" + path
        return path

    def _subscribable(self, path: str) -> bool:
        if path in self._hidden:
            return False
        return (
            path in _RESOURCES
            or path in (_EXCLUSION_COLLECTION, _PRIORITY_COLLECTION)
            or path == "/api/ssc/version"
        )

    @staticmethod
    def _chunk(event: str, data: Any) -> str:
        text = json.dumps(data, separators=(",", ":"))
        if event:
            return f"event: {event}\ndata: {text}\n\n"
        return f"data: {text}\n\n"

    def open_session(self, headers: dict[str, str]) -> tuple[int, str, asyncio.Queue | None]:
        """Start a subscription session. Returns (status, uuid, queue): the
        queue yields SSE text chunks (already framed) and None at the end;
        the first chunk is the ``open`` event."""
        if not self._authorized(headers):
            return 401, "", None
        if "third_party_disabled" in self.active_errors:
            return 403, "", None
        session_uuid = str(uuidlib.uuid4())
        queue: asyncio.Queue = asyncio.Queue()
        self._sessions[session_uuid] = {"queue": queue, "paths": set()}
        clients = getattr(self, "_sse_clients", None)
        if isinstance(clients, set):
            clients.add(queue)
        queue.put_nowait(self._chunk("open", {
            "path": f"{_SUBSCRIPTIONS}/{session_uuid}",
            "sessionUUID": session_uuid,
        }))
        return 200, session_uuid, queue

    def close_session(self, session_uuid: str, *, notify: bool) -> None:
        session = self._sessions.pop(session_uuid, None)
        if session is None:
            return
        queue = session["queue"]
        if notify:
            queue.put_nowait(self._chunk("close", {
                "path": f"{_SUBSCRIPTIONS}/{session_uuid}",
                "sessionUUID": session_uuid,
            }))
        queue.put_nowait(None)
        clients = getattr(self, "_sse_clients", None)
        if isinstance(clients, set):
            clients.discard(queue)

    def session_paths(self, session_uuid: str) -> set[str]:
        session = self._sessions.get(session_uuid)
        return set(session["paths"]) if session else set()

    @property
    def sessions(self) -> list[str]:
        return list(self._sessions)

    def _push(self, session: dict[str, Any], path: str) -> None:
        if path in (_EXCLUSION_COLLECTION, _PRIORITY_COLLECTION):
            data = {path: self._collection_body(path)}
        elif path == "/api/ssc/version":
            data = {path: {"protocol": "2.3", "schema": "1.9"}}
        else:
            data = {path: self._body(path)}
        session["queue"].put_nowait(self._chunk("", data))

    def _notify(self, path: str) -> None:
        """A resource changed: push it to every session subscribed to it, or
        to the collection that contains it."""
        targets = {path}
        if path.startswith(_EXCLUSION_COLLECTION + "/"):
            targets.add(_EXCLUSION_COLLECTION)
        if path.startswith(_PRIORITY_COLLECTION + "/"):
            targets.add(_PRIORITY_COLLECTION)
        for session in list(self._sessions.values()):
            for target in targets & session["paths"]:
                self._push(session, target)

    # ── State ──

    def set_state(self, key: str, value: Any) -> None:
        previous = self.get_state(key)
        super().set_state(key, value)
        if key == "identify_active" and previous != value:
            # Identifying shows on the device state too.
            super().set_state("device_state", "Identifying" if value else "Normal")
            self._notify("/api/device/state")
        if previous == value:
            return
        path = _KEY_TO_PATH.get(key)
        if path is not None:
            self._notify(path)
        elif key == "warnings":
            self._notify("/api/device/state")

    # ── The stream itself (aiohttp, on the real platform) ──

    async def _serve_sse(self, request: Any, path: str) -> Any:
        from aiohttp import web

        status, session_uuid, queue = self.open_session(dict(request.headers))
        if status != 200 or queue is None:
            self.log_protocol("in", f"GET {path} (subscription refused {status})")
            return web.Response(status=status, text=json.dumps({"error": "unauthorized"}),
                                content_type="application/json")
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Content-Location": f"{_SUBSCRIPTIONS}/{session_uuid}",
            },
        )
        await response.prepare(request)
        self.log_protocol("in", f"GET {path} (subscription {session_uuid[:8]} opened)")
        try:
            while self._running:
                chunk = await queue.get()
                if chunk is None:
                    break
                await response.write(chunk.encode("utf-8"))
        except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.close_session(session_uuid, notify=False)
            self.log_protocol("in", f"GET {path} (subscription {session_uuid[:8]} closed)")
        return response
