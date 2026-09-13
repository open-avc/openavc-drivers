"""
OpenAVC Sennheiser TeamConnect Ceiling Medium (TCC M) driver.

Controls the TeamConnect Ceiling Medium beamforming ceiling microphone over
Sennheiser Sound Control Protocol v2 (SSCv2): a REST API over HTTPS on port
443, JSON bodies, HTTP Basic authentication as the user ``api`` with the
third-party password set in Sennheiser Control Cockpit.

Protocol reference (the manufacturer's own):
  - "Sennheiser 3rd Party API" v6.2 (08/2026), the SSCv2 specification 2.3
    plus the per-device pages, docs.cloud.sennheiser.com/en-us/api-docs/.
    Archived: driver-roadmap/reference-docs/sennheiser-3rd-party-api-v6.2-08-2026.pdf
  - "SSCv2 Schema for TCCM", OpenAPI 1.9 (the device's own spec, served by
    that site's Swagger page). Archived: reference-docs/sennheiser-tccm-openapi-1.9.0.json
  - "Sound Control Protocol v2 (SSCv2), TeamConnect Ceiling Medium" v1.1
    (06/2023), the enabling and subscription walk-through. Archived:
    reference-docs/sennheiser-sscv2-tccm-06-2023.pdf

Push, not polling (SSCv2 spec, "SSCv2 Subscriptions"):
  The microphone streams every change of a subscribed resource as a
  Server-Sent Event. A subscription is a small handshake: GET
  /api/ssc/state/subscriptions opens the stream, the reply's Content-Location
  header (and its ``open`` event) carries a session UUID, and a PUT of the
  resource list to /api/ssc/state/subscriptions/{uuid} arms it. The device
  then sends the current value of every subscribed resource, so state is
  fully populated before the first poll, and each later change as it
  happens, including changes made from Control Cockpit. Polling stays on as
  the resync baseline (30 s by default): a dropped stream degrades to poll
  speed, never to stale state. A stream that has been silent for a while is
  reopened, which makes the microphone resend every value.

  Three resources are "FastResource" feeds the spec says to subscribe only
  while the data is in use: the beam direction (talker azimuth and
  elevation, for camera tracking), the microphone and reference level meters
  and the room-activity level. Each feed is a config switch that arms it on
  connect and a pair of commands that switch it at runtime; the values relay
  to the cloud at low priority.

Why Python (not YAML):
  The declarative ``push: {type: sse}`` shape holds a GET open and parses
  events; it has no step for reading the session UUID out of the stream's
  headers and PUTting the resource list to it, and the list must be re-sent
  every time the stream reopens. That handshake is the whole push mechanism
  here, so the driver runs the stream itself. Everything else, the request
  and response bodies and the device settings, would have fitted YAML.

Scope:
  Every resource the TCC M's OpenAPI 1.9 declares except the open-source
  licence download and the raw address tree. Global mute, identify, the beam
  (installation type, source detection, azimuth offset, the beam-freeze
  auto-hold), the analog and both Dante outputs (gain, delay, which
  processing applies), the reference input, the shared 7-band equalizer and
  noise gate, Voice Lift, the denoiser, the input low-cuts, single-capsule
  calibration mode, the five exclusion zones and the priority zone, the LED
  ring, room-in-use with its thresholds, PoE pass-through, firmware update
  progress and the device's own state and warnings. Naming (device name,
  location, position) is read-only on this device's API; Control Cockpit
  sets it.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.utils.logger import get_logger

log = get_logger(__name__)

# The SSCv2 user every TCC M accepts third-party requests as (SSCv2 guide,
# "Authentication"). The password is per device, set in Control Cockpit.
_API_USER = "api"

# The product string /api/device/identity answers with on a TCC M.
_PRODUCT = "TCCM"

# Subscription stream endpoints.
_SUBSCRIPTIONS = "/api/ssc/state/subscriptions"

# The device sends no documented keepalive on the event stream. After this
# much silence the stream is reopened, which re-arms the subscription and
# makes the microphone resend every subscribed value (a free full resync).
_STREAM_IDLE_REOPEN_S = 300.0

# Fastest rate a FastResource feed writes state, per resource. A talker
# moving across the room or a meter bouncing produces many notifications a
# second; a panel cannot draw more than this and the cloud should not carry
# it.
_FAST_MIN_INTERVAL_S = 0.2

# Enum vocabularies, verbatim from the OpenAPI schema (JSON is case
# sensitive; the device refuses "flushMounted").
_INSTALLATION_TYPES = ["FlushMounted", "SurfaceMounted", "Suspended"]
_DETECTION_THRESHOLDS = ["QuietRoom", "NormalRoom", "LoudRoom"]
_OUTPUT_SOURCES = ["FarendOutput", "LocalOutput"]
_DENOISER_LEVELS = ["Off", "Low", "Medium", "High"]
_LED_COLORS = [
    "LightGreen", "Green", "Blue", "Red", "Yellow", "Orange", "Cyan", "Pink",
]
_DEVICE_STATES = ["Normal", "Identifying", "FirmwareUpdate"]
_UPDATE_STATES = ["Idle", "Updating"]
_UPDATE_RESULTS = ["None", "ChecksumError"]

_EQ_BANDS = ["125", "250", "500", "1k", "2k", "4k", "8k"]
_EQ_KEYS = [f"eq_{band}_db" for band in _EQ_BANDS]

_EXCLUSION_ZONES = 5


class _Resource:
    """One API resource: its path, which JSON fields map to which state keys,
    and how it is read.

    ``fields`` maps a dotted JSON path in the resource body ("micOn.color")
    to a state key. ``fast`` marks a FastResource feed (subscribed only when
    armed, never polled, rate-limited on the way in). ``poll`` is False for a
    resource the driver reads once (identity) rather than every cycle.
    """

    __slots__ = ("path", "fields", "fast", "poll", "subscribe")

    def __init__(
        self,
        path: str,
        fields: dict[str, str],
        *,
        fast: bool = False,
        poll: bool = True,
        subscribe: bool = True,
    ) -> None:
        self.path = path
        self.fields = fields
        self.fast = fast
        self.poll = poll
        self.subscribe = subscribe


def _zone_fields(prefix: str) -> dict[str, str]:
    return {
        "enabled": f"{prefix}_enabled",
        "azimuth.min": f"{prefix}_azimuth_min",
        "azimuth.max": f"{prefix}_azimuth_max",
        "elevation.min": f"{prefix}_elevation_min",
        "elevation.max": f"{prefix}_elevation_max",
    }


_RESOURCES: list[_Resource] = [
    _Resource(
        "/api/device/identity",
        {
            "product": "product",
            "serial": "serial",
            "hardwareRevision": "hardware_revision",
        },
        poll=False,
        subscribe=False,
    ),
    _Resource("/api/device/identification", {"visual": "identify_active"}),
    _Resource(
        "/api/device/site",
        {
            "deviceName": "device_name",
            "location": "location",
            "position": "position",
        },
    ),
    _Resource(
        "/api/device/state",
        {"state": "device_state", "warnings": "warnings"},
    ),
    _Resource(
        "/api/firmware/update/state",
        {
            "deviceVersion": "firmware_version",
            "danteVersion": "dante_version",
            "state": "update_state",
            "progress": "update_progress",
            "lastStatus": "update_last_status",
        },
    ),
    _Resource("/api/audio/outputs/global/mute", {"enabled": "mute"}),
    _Resource(
        "/api/audio/inputs/microphone/beam",
        {
            "installationType": "installation_type",
            "sourceDetectionThreshold": "source_detection_threshold",
            "offset": "beam_offset",
        },
    ),
    _Resource(
        "/api/audio/inputs/microphone/beam/direction",
        {
            "azimuth": "beam_azimuth",
            "elevation": "beam_elevation",
            "beamFreezeActive": "beam_freeze_active",
        },
        fast=True,
    ),
    _Resource(
        "/api/audio/inputs/microphone/level", {"peak": "mic_peak_db"}, fast=True,
    ),
    _Resource(
        "/api/audio/inputs/reference/level",
        {"rms": "reference_rms_dbfs"},
        fast=True,
    ),
    _Resource("/api/audio/roomInUse", {"active": "room_in_use"}),
    _Resource(
        "/api/audio/roomInUse/activityLevel",
        {"peak": "room_activity_db"},
        fast=True,
    ),
    _Resource(
        "/api/audio/roomInUse/config",
        {
            "triggerTime": "room_in_use_trigger_s",
            "releaseTime": "room_in_use_release_s",
            "threshold": "room_in_use_threshold_db",
        },
    ),
    _Resource(
        "/api/audio/outputs/analog",
        {"gain": "analog_gain_db", "switch": "analog_output_source"},
    ),
    _Resource(
        "/api/audio/outputs/dante/farEnd",
        {
            "gain": "farend_gain_db",
            "noiseGateEnabled": "farend_noise_gate",
            "equalizerEnabled": "farend_equalizer",
            "delay": "farend_delay_ms",
        },
    ),
    _Resource(
        "/api/audio/voiceLift",
        {
            "emergencyMuteThreshold": "voice_lift_mute_threshold_db",
            "emergencyMuteTime": "voice_lift_mute_time_s",
        },
    ),
    _Resource(
        "/api/audio/outputs/dante/local",
        {
            "gain": "local_gain_db",
            "noiseGateEnabled": "local_noise_gate",
            "equalizerEnabled": "local_equalizer",
            "voiceLiftEnabled": "local_voice_lift",
            "delay": "local_delay_ms",
        },
    ),
    _Resource(
        "/api/audio/inputs/dante/reference",
        {
            "gain": "reference_gain_db",
            "farEndAutoAdjustEnabled": "reference_auto_adjust",
        },
    ),
    _Resource(
        "/api/audio/equalizer",
        {f"gains.{i}": key for i, key in enumerate(_EQ_KEYS)},
    ),
    _Resource(
        "/api/audio/noiseGate",
        {"threshold": "noise_gate_threshold_db", "holdTime": "noise_gate_hold_ms"},
    ),
    _Resource(
        "/api/device/leds/ring",
        {
            "brightness": "led_brightness",
            "showFarendActivity": "led_show_farend_activity",
            "micOn.color": "led_mic_on_color",
            "micMute.color": "led_mic_mute_color",
            "micCustom.enabled": "led_custom_enabled",
            "micCustom.color": "led_custom_color",
        },
    ),
    _Resource(
        "/api/device/power/poe/daisychain",
        {"sufficientPower": "poe_sufficient_power", "inUse": "poe_output_in_use"},
    ),
    _Resource(
        "/api/audio/inputs/microphone/denoiser", {"setting": "denoiser"},
    ),
    _Resource(
        "/api/audio/inputs/microphone/inputLowcuts", {"enabled": "input_lowcuts"},
    ),
    _Resource(
        "/api/audio/inputs/microphone/singleCapsuleMode",
        {"enabled": "single_capsule_mode"},
    ),
    _Resource(
        "/api/audio/inputs/microphone/beam/beamfreeze/autoHold",
        {"enabled": "beam_freeze_auto_hold", "holdTime": "beam_freeze_hold_ms"},
    ),
]
for _n in range(1, _EXCLUSION_ZONES + 1):
    _RESOURCES.append(
        _Resource(
            f"/api/audio/inputs/microphone/exclusionZones/{_n - 1}",
            _zone_fields(f"exclusion_zone_{_n}"),
        )
    )
_RESOURCES.append(
    _Resource(
        "/api/audio/inputs/microphone/priorityZones/0",
        {"weight": "priority_zone_weight", **_zone_fields("priority_zone")},
    )
)

_RESOURCE_BY_PATH: dict[str, _Resource] = {r.path: r for r in _RESOURCES}
# Which resource a state key belongs to, for the device-settings writer.
_RESOURCE_BY_KEY: dict[str, _Resource] = {
    key: r for r in _RESOURCES for key in r.fields.values()
}
_FIELD_BY_KEY: dict[str, str] = {
    key: field for r in _RESOURCES for field, key in r.fields.items()
}
_ZONE_COLLECTIONS = {
    "/api/audio/inputs/microphone/exclusionZones",
    "/api/audio/inputs/microphone/priorityZones",
}

# Value rules the API states that a min/max cannot express (OpenAPI
# ``multipleOf``), checked before a write leaves the box so the refusal names
# the rule instead of the device answering 422.
_STEP_BY_KEY: dict[str, int | float] = {"beam_offset": 30, "priority_zone_weight": 0.1}
for _key in _FIELD_BY_KEY:
    if _key.endswith(("_azimuth_min", "_azimuth_max", "_elevation_min", "_elevation_max")):
        _STEP_BY_KEY[_key] = 5


def _dig(obj: Any, path: str) -> Any:
    """Read a dotted path ("micOn.color", "gains.3") out of a JSON body.
    Returns None when any segment is missing."""
    cur = obj
    for seg in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, list) and seg.isdigit() and int(seg) < len(cur):
            cur = cur[int(seg)]
        else:
            return None
        if cur is None:
            return None
    return cur


def _nest(path: str, value: Any) -> dict[str, Any]:
    """Build the nested body for one dotted field: "elevation.min" -> {"elevation": {"min": v}}."""
    parts = path.split(".")
    body: Any = value
    for seg in reversed(parts):
        body = {seg: body}
    return body


def _deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
    for key, val in src.items():
        if isinstance(val, dict) and isinstance(dst.get(key), dict):
            _deep_merge(dst[key], val)
        else:
            dst[key] = val
    return dst


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes")
    return bool(value)





class SennheiserTccmDriver(BaseDriver):
    """Sennheiser TeamConnect Ceiling Medium over SSCv2 (HTTPS + SSE)."""

    DRIVER_INFO = {
        "id": "sennheiser_tccm",
        "name": "Sennheiser TeamConnect Ceiling Medium",
        "manufacturer": "Sennheiser",
        "category": "audio",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": (
            "Controls the Sennheiser TeamConnect Ceiling Medium beamforming "
            "ceiling microphone over its SSCv2 REST API (HTTPS, port 443). "
            "Mute, identify, output gains and delays, the reference input, "
            "the shared equalizer and noise gate, Voice Lift, the denoiser, "
            "exclusion and priority zones, installation type, the LED ring, "
            "room-in-use, PoE pass-through and firmware status. The "
            "microphone reports where the active talker is (beam azimuth and "
            "elevation) and live level meters; both feeds are opt-in and can "
            "be switched from a macro or panel. State is push-driven: the "
            "driver subscribes and the microphone reports every change as it "
            "happens, including changes made from Sennheiser Control Cockpit."
        ),
        "source_url": "https://docs.cloud.sennheiser.com/en-us/api-docs/api-docs/open-api-tc-ceiling-medium.html",
        "tags": [
            "microphone", "ceiling-mic", "beamforming", "array", "conferencing",
            "dante", "camera-tracking", "ssc", "sscv2",
        ],
        "verified": False,
        "simulated": True,
        "protocols": ["sscv2"],
        "ports": [443],
        # Lifecycle hooks (_create_transport / _post_connect / _initial_sync /
        # _close_session / _link_alive) landed in 0.24.0; the platform's
        # required-parameter gate the enum params rely on in 0.24.0 too.
        "min_platform_version": "0.25.0",
        "compatible_models": [
            {
                "manufacturer": "Sennheiser",
                "models": ["TeamConnect Ceiling Medium", "TCC M"],
                "confidence": "untested",
                "notes": (
                    "Built from Sennheiser's SSCv2 specification and the TCC M "
                    "OpenAPI 1.9 schema (firmware 1.2 or newer; the input "
                    "low-cut, single-capsule and beam-freeze auto-hold "
                    "resources arrived in later API versions and are skipped "
                    "on a microphone that lacks them). Not yet run against a "
                    "microphone. Covers every product variant (TCC M-F flush, "
                    "TCC M-S surface, TCC M CT grid-ceiling); the mounting "
                    "kit does not change the API."
                ),
            },
        ],
        "transport": "http",
        "help": {
            "overview": (
                "Controls the Sennheiser TeamConnect Ceiling Medium, a ceiling "
                "microphone that steers one pickup beam at whoever is talking. "
                "Mute it, set its output levels and processing, change the LED "
                "ring, and read where the talker is (azimuth and elevation) to "
                "drive camera presets from a macro. Everything the microphone "
                "changes, from Control Cockpit or its own button, shows up "
                "here as it happens."
            ),
            "setup": (
                "1. Set the microphone up once in Sennheiser Control Cockpit "
                "(it must be claimed there before its API works).\n"
                "2. On the microphone's device page in Control Cockpit, enable "
                "third-party access and set a third-party password.\n"
                "3. Enter the microphone's IP address and that password here. "
                "The user name is always \"api\".\n"
                "4. Turn on \"Report talker position\" if a macro will follow "
                "the talker with a camera, and \"Report level meters\" if a "
                "panel shows the levels. Both are continuous feeds, so they "
                "are off unless needed."
            ),
            "connection": (
                "HTTPS on port 443 with the third-party password from Control "
                "Cockpit. The microphone's own certificate is self-signed, so "
                "certificate verification is off unless you turn it on."
            ),
        },
        "default_config": {
            "host": "",
            "port": 443,
            "ssl": True,
            "verify_ssl": False,
            "auth_type": "basic",
            "username": "api",
            "password": "",
            "timeout": 5.0,
            "poll_interval": 30,
            "enable_talker_position": False,
            "enable_meters": False,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
                "help": "The microphone's IP address, as shown in Control Cockpit.",
            },
            "port": {
                "type": "integer",
                "default": 443,
                "label": "HTTPS Port",
                "advanced": True,
                "help": "443 on every microphone; change it only behind a port forward.",
            },
            "password": {
                "type": "string",
                "secret": True,
                "required": True,
                "label": "Third-party Password",
                "help": (
                    "The third-party password set on the microphone's device "
                    "page in Sennheiser Control Cockpit. The user name is "
                    "always \"api\" and needs no entry."
                ),
            },
            "verify_ssl": {
                "type": "boolean",
                "default": False,
                "label": "Verify TLS Certificate",
                "advanced": True,
                "help": (
                    "Off by default: the microphone ships a self-signed "
                    "certificate. Turn on only if a trusted certificate has "
                    "been installed on it."
                ),
            },
            "enable_talker_position": {
                "type": "boolean",
                "default": False,
                "label": "Report Talker Position",
                "help": (
                    "Streams the beam's azimuth and elevation as the talker "
                    "moves, for camera tracking from a macro. Off by default "
                    "because it is a continuous feed; a macro or panel can "
                    "also switch it with the Talker Position On/Off commands."
                ),
            },
            "enable_meters": {
                "type": "boolean",
                "default": False,
                "label": "Report Level Meters",
                "help": (
                    "Streams the microphone peak, reference input and room "
                    "activity levels. Off by default because it is a "
                    "continuous feed; a macro or panel can also switch it "
                    "with the Meters On/Off commands."
                ),
            },
        },
        "state_variables": {
            # ── Identity and device ──
            "product": {"type": "string", "label": "Product"},
            "serial": {"type": "string", "label": "Serial Number"},
            "hardware_revision": {"type": "string", "label": "Hardware Revision"},
            "firmware_version": {"type": "string", "label": "Firmware Version"},
            "dante_version": {"type": "string", "label": "Dante Firmware Version"},
            "device_name": {"type": "string", "label": "Device Name"},
            "location": {"type": "string", "label": "Location"},
            "position": {"type": "string", "label": "Position"},
            "device_state": {
                "type": "enum", "values": ['Normal', 'Identifying', 'FirmwareUpdate'], "label": "Device State",
                "help": "Normal, Identifying (LED ring flashing), or FirmwareUpdate.",
            },
            "warnings": {
                "type": "string", "label": "Warnings",
                "help": "The microphone's own warnings, semicolon-separated; empty when there are none.",
            },
            "identify_active": {
                "type": "boolean", "label": "Identify",
                "help": "True while the LED ring flashes to identify this microphone.",
            },
            "update_state": {"type": "enum", "values": ['Idle', 'Updating'], "label": "Firmware Update"},
            "update_progress": {
                "type": "integer", "label": "Firmware Update Progress",
                "min": 0, "max": 100, "unit": "%",
            },
            "update_last_status": {
                "type": "enum", "values": ['None', 'ChecksumError'], "label": "Last Firmware Update Result",
            },
            "poe_sufficient_power": {
                "type": "boolean", "label": "PoE Pass-through Power Available",
                "help": "True when the PoE input has enough power to feed a device on the PoE output.",
            },
            "poe_output_in_use": {
                "type": "boolean", "label": "PoE Output In Use",
                "help": "True while a device is being powered from the PoE output.",
            },
            # ── Audio ──
            "mute": {
                "type": "boolean", "label": "Mute", "control": True,
                "cloud_priority": "high",
                "help": "All outputs muted.",
            },
            "room_in_use": {
                "type": "boolean", "label": "Room In Use", "cloud_priority": "high",
                "help": "The microphone has heard sustained talking in the room.",
            },
            "room_in_use_trigger_s": {
                "type": "integer", "label": "Room In Use Trigger Time",
                "min": 1, "max": 20, "unit": "s",
                "help": "Seconds of activity above the threshold before Room In Use turns on. Set in Control Cockpit.",
            },
            "room_in_use_release_s": {
                "type": "integer", "label": "Room In Use Release Time",
                "min": 5, "max": 600, "unit": "s",
                "help": "Seconds of quiet before Room In Use turns off. Set in Control Cockpit.",
            },
            "room_in_use_threshold_db": {
                "type": "integer", "label": "Room In Use Threshold",
                "min": 0, "max": 36, "unit": "dB",
                "help": "Activity level that counts as the room being used. Set in Control Cockpit.",
            },
            "room_activity_db": {
                "type": "integer", "label": "Room Activity Level",
                "min": 0, "max": 90, "unit": "dB", "cloud_priority": "low",
                "help": "Current near-end activity. Live only while the level meters are on.",
            },
            "beam_azimuth": {
                "type": "integer", "label": "Beam Azimuth",
                "min": 0, "max": 360, "unit": "deg", "cloud_priority": "low",
                "help": "Direction of the active talker around the microphone. Live only while Report Talker Position is on.",
            },
            "beam_elevation": {
                "type": "integer", "label": "Beam Elevation",
                "min": 0, "max": 90, "unit": "deg", "cloud_priority": "low",
                "help": "Angle of the active talker below the microphone. Live only while Report Talker Position is on.",
            },
            "beam_freeze_active": {
                "type": "boolean", "label": "Beam Frozen", "cloud_priority": "low",
                "help": "True while far-end speech holds the beam still. Live only while Report Talker Position is on.",
            },
            "mic_peak_db": {
                "type": "integer", "label": "Microphone Peak Level",
                "min": -90, "max": 0, "unit": "dB", "cloud_priority": "low",
                "help": "Live only while the level meters are on.",
            },
            "reference_rms_dbfs": {
                "type": "integer", "label": "Reference Input Level",
                "min": -120, "max": 0, "unit": "dBFS", "cloud_priority": "low",
                "help": "RMS level of the Dante reference (far-end) input. Live only while the level meters are on.",
            },
            "talker_position_feed": {
                "type": "boolean", "label": "Talker Position Feed On",
                "help": "True while this driver is subscribed to the beam direction.",
            },
            "meters_feed": {
                "type": "boolean", "label": "Level Meters Feed On",
                "help": "True while this driver is subscribed to the level meters.",
            },
            "installation_type": {
                "type": "enum", "values": ['FlushMounted', 'SurfaceMounted', 'Suspended'], "label": "Installation Type",
            },
            "source_detection_threshold": {
                "type": "enum", "values": ['QuietRoom', 'NormalRoom', 'LoudRoom'],
                "label": "Source Detection Threshold",
            },
            "beam_offset": {
                "type": "integer", "label": "Beam Azimuth Offset",
                "min": 0, "max": 330, "step": 30, "unit": "deg",
            },
            "analog_gain_db": {
                "type": "integer", "label": "Analog Output Gain", "control": True,
                "min": -18, "max": 0, "unit": "dB",
            },
            "analog_output_source": {
                "type": "enum", "values": ['FarendOutput', 'LocalOutput'], "label": "Analog Output Source",
            },
            "farend_gain_db": {
                "type": "integer", "label": "Far-end Output Gain", "control": True,
                "min": 0, "max": 24, "unit": "dB",
            },
            "farend_noise_gate": {"type": "boolean", "label": "Far-end Output Noise Gate"},
            "farend_equalizer": {"type": "boolean", "label": "Far-end Output Equalizer"},
            "farend_delay_ms": {
                "type": "integer", "label": "Far-end Output Delay",
                "min": 0, "max": 100, "unit": "ms",
            },
            "local_gain_db": {
                "type": "integer", "label": "Local Output Gain", "control": True,
                "min": 0, "max": 24, "unit": "dB",
            },
            "local_noise_gate": {"type": "boolean", "label": "Local Output Noise Gate"},
            "local_equalizer": {"type": "boolean", "label": "Local Output Equalizer"},
            "local_voice_lift": {"type": "boolean", "label": "Local Output Voice Lift"},
            "local_delay_ms": {
                "type": "integer", "label": "Local Output Delay",
                "min": 0, "max": 100, "unit": "ms",
            },
            "voice_lift_mute_threshold_db": {
                "type": "integer", "label": "Voice Lift Emergency Mute Threshold",
                "min": -50, "max": -3, "unit": "dB",
            },
            "voice_lift_mute_time_s": {
                "type": "integer", "label": "Voice Lift Emergency Mute Time",
                "min": 1, "max": 30, "unit": "s",
            },
            "reference_gain_db": {
                "type": "integer", "label": "Reference Input Gain",
                "min": -60, "max": 10, "unit": "dB",
            },
            "reference_auto_adjust": {
                "type": "boolean", "label": "Reference Input Auto Adjust",
                "help": "Automatic far-end detection threshold from the noise floor. While on, the reference gain cannot be set by hand.",
            },
            "eq_125_db": {"type": "integer", "label": "EQ 125 Hz", "min": -8, "max": 8, "unit": "dB"},
            "eq_250_db": {"type": "integer", "label": "EQ 250 Hz", "min": -8, "max": 8, "unit": "dB"},
            "eq_500_db": {"type": "integer", "label": "EQ 500 Hz", "min": -8, "max": 8, "unit": "dB"},
            "eq_1k_db": {"type": "integer", "label": "EQ 1 kHz", "min": -8, "max": 8, "unit": "dB"},
            "eq_2k_db": {"type": "integer", "label": "EQ 2 kHz", "min": -8, "max": 8, "unit": "dB"},
            "eq_4k_db": {"type": "integer", "label": "EQ 4 kHz", "min": -8, "max": 8, "unit": "dB"},
            "eq_8k_db": {"type": "integer", "label": "EQ 8 kHz", "min": -8, "max": 8, "unit": "dB"},
            "noise_gate_threshold_db": {
                "type": "integer", "label": "Noise Gate Threshold",
                "min": -90, "max": -40, "unit": "dB",
            },
            "noise_gate_hold_ms": {
                "type": "integer", "label": "Noise Gate Hold Time",
                "min": 50, "max": 1000, "unit": "ms",
            },
            "led_brightness": {
                "type": "integer", "label": "LED Ring Brightness", "control": True,
                "min": 0, "max": 5, "step": 1,
                "help": "0 is off, 5 is full brightness.",
            },
            "led_show_farend_activity": {
                "type": "boolean", "label": "LED Shows Far-end Activity",
            },
            "led_mic_on_color": {"type": "enum", "values": ['LightGreen', 'Green', 'Blue', 'Red', 'Yellow', 'Orange', 'Cyan', 'Pink'], "label": "LED Colour When Live"},
            "led_mic_mute_color": {"type": "enum", "values": ['LightGreen', 'Green', 'Blue', 'Red', 'Yellow', 'Orange', 'Cyan', 'Pink'], "label": "LED Colour When Muted"},
            "led_custom_enabled": {
                "type": "boolean", "label": "LED Custom Colour Enabled",
                "help": "While on, the ring shows the custom colour instead of the live and muted colours.",
            },
            "led_custom_color": {"type": "enum", "values": ['LightGreen', 'Green', 'Blue', 'Red', 'Yellow', 'Orange', 'Cyan', 'Pink'], "label": "LED Custom Colour"},
            "denoiser": {"type": "enum", "values": ['Off', 'Low', 'Medium', 'High'], "label": "Denoiser"},
            "input_lowcuts": {"type": "boolean", "label": "Input Low-cut Filters"},
            "single_capsule_mode": {
                "type": "boolean", "label": "Single Capsule Mode",
                "help": "Routes one raw capsule to the output, for calibration. Leave off in normal use.",
            },
            "beam_freeze_auto_hold": {
                "type": "boolean", "label": "Beam Freeze Auto Hold",
                "help": "Near-end speech releases a frozen beam after the hold time.",
            },
            "beam_freeze_hold_ms": {
                "type": "integer", "label": "Beam Freeze Hold Time",
                "min": 50, "max": 500, "unit": "ms",
            },
            'exclusion_zone_1_enabled': {'type': 'boolean', 'label': 'Exclusion Zone 1 Enabled'},
            'exclusion_zone_1_azimuth_min': {'type': 'integer', 'label': 'Exclusion Zone 1 Azimuth Min', 'min': 0, 'max': 360, 'step': 5, 'unit': 'deg'},
            'exclusion_zone_1_azimuth_max': {'type': 'integer', 'label': 'Exclusion Zone 1 Azimuth Max', 'min': 0, 'max': 360, 'step': 5, 'unit': 'deg'},
            'exclusion_zone_1_elevation_min': {'type': 'integer', 'label': 'Exclusion Zone 1 Elevation Min', 'min': 0, 'max': 90, 'step': 5, 'unit': 'deg'},
            'exclusion_zone_1_elevation_max': {'type': 'integer', 'label': 'Exclusion Zone 1 Elevation Max', 'min': 0, 'max': 90, 'step': 5, 'unit': 'deg'},
            'exclusion_zone_2_enabled': {'type': 'boolean', 'label': 'Exclusion Zone 2 Enabled'},
            'exclusion_zone_2_azimuth_min': {'type': 'integer', 'label': 'Exclusion Zone 2 Azimuth Min', 'min': 0, 'max': 360, 'step': 5, 'unit': 'deg'},
            'exclusion_zone_2_azimuth_max': {'type': 'integer', 'label': 'Exclusion Zone 2 Azimuth Max', 'min': 0, 'max': 360, 'step': 5, 'unit': 'deg'},
            'exclusion_zone_2_elevation_min': {'type': 'integer', 'label': 'Exclusion Zone 2 Elevation Min', 'min': 0, 'max': 90, 'step': 5, 'unit': 'deg'},
            'exclusion_zone_2_elevation_max': {'type': 'integer', 'label': 'Exclusion Zone 2 Elevation Max', 'min': 0, 'max': 90, 'step': 5, 'unit': 'deg'},
            'exclusion_zone_3_enabled': {'type': 'boolean', 'label': 'Exclusion Zone 3 Enabled'},
            'exclusion_zone_3_azimuth_min': {'type': 'integer', 'label': 'Exclusion Zone 3 Azimuth Min', 'min': 0, 'max': 360, 'step': 5, 'unit': 'deg'},
            'exclusion_zone_3_azimuth_max': {'type': 'integer', 'label': 'Exclusion Zone 3 Azimuth Max', 'min': 0, 'max': 360, 'step': 5, 'unit': 'deg'},
            'exclusion_zone_3_elevation_min': {'type': 'integer', 'label': 'Exclusion Zone 3 Elevation Min', 'min': 0, 'max': 90, 'step': 5, 'unit': 'deg'},
            'exclusion_zone_3_elevation_max': {'type': 'integer', 'label': 'Exclusion Zone 3 Elevation Max', 'min': 0, 'max': 90, 'step': 5, 'unit': 'deg'},
            'exclusion_zone_4_enabled': {'type': 'boolean', 'label': 'Exclusion Zone 4 Enabled'},
            'exclusion_zone_4_azimuth_min': {'type': 'integer', 'label': 'Exclusion Zone 4 Azimuth Min', 'min': 0, 'max': 360, 'step': 5, 'unit': 'deg'},
            'exclusion_zone_4_azimuth_max': {'type': 'integer', 'label': 'Exclusion Zone 4 Azimuth Max', 'min': 0, 'max': 360, 'step': 5, 'unit': 'deg'},
            'exclusion_zone_4_elevation_min': {'type': 'integer', 'label': 'Exclusion Zone 4 Elevation Min', 'min': 0, 'max': 90, 'step': 5, 'unit': 'deg'},
            'exclusion_zone_4_elevation_max': {'type': 'integer', 'label': 'Exclusion Zone 4 Elevation Max', 'min': 0, 'max': 90, 'step': 5, 'unit': 'deg'},
            'exclusion_zone_5_enabled': {'type': 'boolean', 'label': 'Exclusion Zone 5 Enabled'},
            'exclusion_zone_5_azimuth_min': {'type': 'integer', 'label': 'Exclusion Zone 5 Azimuth Min', 'min': 0, 'max': 360, 'step': 5, 'unit': 'deg'},
            'exclusion_zone_5_azimuth_max': {'type': 'integer', 'label': 'Exclusion Zone 5 Azimuth Max', 'min': 0, 'max': 360, 'step': 5, 'unit': 'deg'},
            'exclusion_zone_5_elevation_min': {'type': 'integer', 'label': 'Exclusion Zone 5 Elevation Min', 'min': 0, 'max': 90, 'step': 5, 'unit': 'deg'},
            'exclusion_zone_5_elevation_max': {'type': 'integer', 'label': 'Exclusion Zone 5 Elevation Max', 'min': 0, 'max': 90, 'step': 5, 'unit': 'deg'},
            'priority_zone_enabled': {'type': 'boolean', 'label': 'Priority Zone Enabled'},
            'priority_zone_azimuth_min': {'type': 'integer', 'label': 'Priority Zone Azimuth Min', 'min': 0, 'max': 360, 'step': 5, 'unit': 'deg'},
            'priority_zone_azimuth_max': {'type': 'integer', 'label': 'Priority Zone Azimuth Max', 'min': 0, 'max': 360, 'step': 5, 'unit': 'deg'},
            'priority_zone_elevation_min': {'type': 'integer', 'label': 'Priority Zone Elevation Min', 'min': 0, 'max': 90, 'step': 5, 'unit': 'deg'},
            'priority_zone_elevation_max': {'type': 'integer', 'label': 'Priority Zone Elevation Max', 'min': 0, 'max': 90, 'step': 5, 'unit': 'deg'},
            "priority_zone_weight": {
                "type": "number", "label": "Priority Zone Weight",
                "min": 1, "max": 4, "step": 0.1,
                "help": "How strongly the beam favours a talker inside the priority zone; 1 is no preference.",
            },
            "last_error": {
                "type": "string", "label": "Last Error",
                "help": "The microphone's most recent refusal of a command or setting.",
            },
        },
        "commands": {
            # ── Mute and identify ──
            "mute_on": {"label": "Mute", "help": "Mute all outputs.", "sets": {"mute": True}},
            "mute_off": {"label": "Unmute", "help": "Unmute all outputs.", "sets": {"mute": False}},
            "mute_toggle": {
                "label": "Mute Toggle",
                "help": "Mute if live, unmute if muted, from the microphone's reported state.",
            },
            "identify_on": {
                "label": "Identify On",
                "help": "Flash the LED ring so the microphone can be found in the room.",
                "sets": {"identify_active": True},
            },
            "identify_off": {
                "label": "Identify Off",
                "help": "Stop the identify flash.",
                "sets": {"identify_active": False},
            },
            # ── Levels ──
            "set_analog_gain": {
                "label": "Set Analog Output Gain",
                "help": "Analog output level, -18 to 0 dB.",
                "params": {
                    "gain": {
                        "type": "integer", "required": True, "label": "Gain",
                        "min": -18, "max": 0, "unit": "dB",
                    },
                },
                "sets": {"analog_gain_db": "{gain}"},
            },
            "set_analog_output_source": {
                "label": "Set Analog Output Source",
                "help": "Whether the analog output carries the local (room) mix or the far-end mix.",
                "params": {
                    "source": {
                        "type": "enum", "required": True, "label": "Source",
                        "values": [
                            {"value": "LocalOutput", "label": "Local (room)"},
                            {"value": "FarendOutput", "label": "Far end"},
                        ],
                    },
                },
                "sets": {"analog_output_source": "{source}"},
            },
            "set_farend_gain": {
                "label": "Set Far-end Output Gain",
                "help": "Dante far-end output level, 0 to 24 dB.",
                "params": {
                    "gain": {
                        "type": "integer", "required": True, "label": "Gain",
                        "min": 0, "max": 24, "unit": "dB",
                    },
                },
                "sets": {"farend_gain_db": "{gain}"},
            },
            "set_local_gain": {
                "label": "Set Local Output Gain",
                "help": "Dante local output level, 0 to 24 dB.",
                "params": {
                    "gain": {
                        "type": "integer", "required": True, "label": "Gain",
                        "min": 0, "max": 24, "unit": "dB",
                    },
                },
                "sets": {"local_gain_db": "{gain}"},
            },
            "set_reference_gain": {
                "label": "Set Reference Input Gain",
                "help": (
                    "Manual gain for far-end detection, -60 to 10 dB. Turn "
                    "Reference Input Auto Adjust off first; the microphone "
                    "refuses a manual gain while it is on."
                ),
                "params": {
                    "gain": {
                        "type": "integer", "required": True, "label": "Gain",
                        "min": -60, "max": 10, "unit": "dB",
                    },
                },
                "sets": {"reference_gain_db": "{gain}"},
            },
            "reference_auto_adjust_on": {
                "label": "Reference Auto Adjust On",
                "help": "Let the microphone set the far-end detection threshold from the noise floor.",
                "sets": {"reference_auto_adjust": True},
            },
            "reference_auto_adjust_off": {
                "label": "Reference Auto Adjust Off",
                "help": "Use the manual reference input gain.",
                "sets": {"reference_auto_adjust": False},
            },
            # ── Processing ──
            "set_denoiser": {
                "label": "Set Denoiser",
                "help": "Noise reduction strength.",
                "params": {
                    "level": {
                        "type": "enum", "required": True, "label": "Level",
                        "values": ['Off', 'Low', 'Medium', 'High'],
                    },
                },
                "sets": {"denoiser": "{level}"},
            },
            "set_eq_band": {
                "label": "Set EQ Band",
                "help": "One band of the shared 7-band equalizer, -8 to 8 dB. Applies to each output whose Equalizer setting is on.",
                "params": {
                    "band": {
                        "type": "enum", "required": True, "label": "Band",
                        "values": [
                            {"value": "125", "label": "125 Hz"},
                            {"value": "250", "label": "250 Hz"},
                            {"value": "500", "label": "500 Hz"},
                            {"value": "1k", "label": "1 kHz"},
                            {"value": "2k", "label": "2 kHz"},
                            {"value": "4k", "label": "4 kHz"},
                            {"value": "8k", "label": "8 kHz"},
                        ],
                    },
                    "gain": {
                        "type": "integer", "required": True, "label": "Gain",
                        "min": -8, "max": 8, "unit": "dB",
                    },
                },
            },
            "eq_flat": {
                "label": "EQ Flat",
                "help": "Set every equalizer band to 0 dB.",
            },
            # ── Zones ──
            "exclusion_zone_on": {
                "label": "Exclusion Zone On",
                "help": "Switch on one of the five exclusion zones. Its angles are in this device's settings.",
                "params": {
                    "zone": {
                        "type": "enum", "required": True, "label": "Zone",
                        "values": [
                            {"value": "1", "label": "Zone 1"},
                            {"value": "2", "label": "Zone 2"},
                            {"value": "3", "label": "Zone 3"},
                            {"value": "4", "label": "Zone 4"},
                            {"value": "5", "label": "Zone 5"},
                        ],
                    },
                },
            },
            "exclusion_zone_off": {
                "label": "Exclusion Zone Off",
                "help": "Switch off one of the five exclusion zones.",
                "params": {
                    "zone": {
                        "type": "enum", "required": True, "label": "Zone",
                        "values": [
                            {"value": "1", "label": "Zone 1"},
                            {"value": "2", "label": "Zone 2"},
                            {"value": "3", "label": "Zone 3"},
                            {"value": "4", "label": "Zone 4"},
                            {"value": "5", "label": "Zone 5"},
                        ],
                    },
                },
            },
            "priority_zone_on": {
                "label": "Priority Zone On",
                "help": "Make the beam favour talkers inside the priority zone.",
                "sets": {"priority_zone_enabled": True},
            },
            "priority_zone_off": {
                "label": "Priority Zone Off",
                "help": "Treat every direction equally.",
                "sets": {"priority_zone_enabled": False},
            },
            # ── LED ring ──
            "set_led_brightness": {
                "label": "Set LED Brightness",
                "help": "LED ring brightness, 0 (off) to 5.",
                "params": {
                    "brightness": {
                        "type": "integer", "required": True, "label": "Brightness",
                        "min": 0, "max": 5,
                    },
                },
                "sets": {"led_brightness": "{brightness}"},
            },
            "set_led_colors": {
                "label": "Set LED Colours",
                "help": "The ring's colour when live and when muted. Leave either blank to keep it.",
                "params": {
                    "mic_on": {"type": "enum", "label": "When Live", "values": [{'value': 'LightGreen', 'label': 'Light green'}, {'value': 'Green', 'label': 'Green'}, {'value': 'Blue', 'label': 'Blue'}, {'value': 'Red', 'label': 'Red'}, {'value': 'Yellow', 'label': 'Yellow'}, {'value': 'Orange', 'label': 'Orange'}, {'value': 'Cyan', 'label': 'Cyan'}, {'value': 'Pink', 'label': 'Pink'}]},
                    "mic_mute": {"type": "enum", "label": "When Muted", "values": [{'value': 'LightGreen', 'label': 'Light green'}, {'value': 'Green', 'label': 'Green'}, {'value': 'Blue', 'label': 'Blue'}, {'value': 'Red', 'label': 'Red'}, {'value': 'Yellow', 'label': 'Yellow'}, {'value': 'Orange', 'label': 'Orange'}, {'value': 'Cyan', 'label': 'Cyan'}, {'value': 'Pink', 'label': 'Pink'}]},
                },
            },
            "led_custom_on": {
                "label": "LED Custom Colour On",
                "help": "Show one fixed colour on the ring regardless of mute, for a room status light.",
                "params": {
                    "color": {
                        "type": "enum", "required": True, "label": "Colour",
                        "values": [{'value': 'LightGreen', 'label': 'Light green'}, {'value': 'Green', 'label': 'Green'}, {'value': 'Blue', 'label': 'Blue'}, {'value': 'Red', 'label': 'Red'}, {'value': 'Yellow', 'label': 'Yellow'}, {'value': 'Orange', 'label': 'Orange'}, {'value': 'Cyan', 'label': 'Cyan'}, {'value': 'Pink', 'label': 'Pink'}],
                    },
                },
                "sets": {"led_custom_enabled": True, "led_custom_color": "{color}"},
            },
            "led_custom_off": {
                "label": "LED Custom Colour Off",
                "help": "Back to the live and muted colours.",
                "sets": {"led_custom_enabled": False},
            },
            # ── Feeds ──
            "talker_position_on": {
                "label": "Talker Position On",
                "help": "Start streaming the beam direction (azimuth, elevation, beam freeze).",
                "sets": {"talker_position_feed": True},
            },
            "talker_position_off": {
                "label": "Talker Position Off",
                "help": "Stop the beam direction stream.",
                "sets": {"talker_position_feed": False},
            },
            "meters_on": {
                "label": "Meters On",
                "help": "Start streaming the microphone peak, reference and room activity levels.",
                "sets": {"meters_feed": True},
            },
            "meters_off": {
                "label": "Meters Off",
                "help": "Stop the level meter streams.",
                "sets": {"meters_feed": False},
            },
            "get_beam_position": {
                "label": "Get Beam Position",
                "help": "Read the beam direction once, for a macro that needs it at one moment without the feed on.",
                "query_for": "beam_azimuth",
            },
            "get_levels": {
                "label": "Get Levels",
                "help": "Read the microphone peak, reference and room activity levels once.",
                "query_for": "mic_peak_db",
            },
        },
        "quick_actions": ["identify_on", "get_beam_position"],
        "actions": [
            {
                "id": "mute_on",
                "icon": "mic-off",
                "visible_when": {"key": "device.$id.mute", "operator": "falsy"},
            },
            {
                "id": "mute_off",
                "icon": "mic",
                "visible_when": {"key": "device.$id.mute", "operator": "truthy"},
            },
        ],
        "device_settings": {
            "installation_type": {
                "type": "enum", "label": "Installation Type", "default": "FlushMounted",
                "values": [
                    {"value": "FlushMounted", "label": "Flush mounted"},
                    {"value": "SurfaceMounted", "label": "Surface mounted"},
                    {"value": "Suspended", "label": "Suspended"},
                ],
                "setup": False,
                "help": "How the microphone is mounted; the beam is tuned for it.",
            },
            "source_detection_threshold": {
                "type": "enum", "label": "Source Detection Threshold", "default": "NormalRoom",
                "values": [
                    {"value": "QuietRoom", "label": "Quiet room"},
                    {"value": "NormalRoom", "label": "Normal room"},
                    {"value": "LoudRoom", "label": "Loud room"},
                ],
                "setup": False,
                "help": "How loud a talker must be to steer the beam, relative to the room's noise.",
            },
            "beam_offset": {
                "type": "integer", "label": "Beam Azimuth Offset", "default": 0,
                "min": 0, "max": 330, "setup": False,
                "help": "Rotates the azimuth readings and every zone, in 30 degree steps, so 0 points where you want it.",
            },
            "analog_gain_db": {
                "type": "integer", "label": "Analog Output Gain", "default": 0,
                "min": -18, "max": 0, "setup": False, "help": "-18 to 0 dB.",
            },
            "analog_output_source": {
                "type": "enum", "label": "Analog Output Source", "default": "LocalOutput",
                "values": [
                    {"value": "LocalOutput", "label": "Local (room)"},
                    {"value": "FarendOutput", "label": "Far end"},
                ],
                "setup": False,
                "help": "Whether the analog output carries the local mix or the far-end mix.",
            },
            "farend_gain_db": {
                "type": "integer", "label": "Far-end Output Gain", "default": 12,
                "min": 0, "max": 24, "setup": False, "help": "0 to 24 dB.",
            },
            "farend_noise_gate": {
                "type": "boolean", "label": "Far-end Output Noise Gate", "default": False,
                "setup": False, "help": "Apply the shared noise gate to the far-end output.",
            },
            "farend_equalizer": {
                "type": "boolean", "label": "Far-end Output Equalizer", "default": False,
                "setup": False, "help": "Apply the shared equalizer to the far-end output.",
            },
            "farend_delay_ms": {
                "type": "integer", "label": "Far-end Output Delay", "default": 0,
                "min": 0, "max": 100, "setup": False, "help": "0 to 100 ms.",
            },
            "local_gain_db": {
                "type": "integer", "label": "Local Output Gain", "default": 12,
                "min": 0, "max": 24, "setup": False, "help": "0 to 24 dB.",
            },
            "local_noise_gate": {
                "type": "boolean", "label": "Local Output Noise Gate", "default": False,
                "setup": False, "help": "Apply the shared noise gate to the local output.",
            },
            "local_equalizer": {
                "type": "boolean", "label": "Local Output Equalizer", "default": False,
                "setup": False, "help": "Apply the shared equalizer to the local output.",
            },
            "local_voice_lift": {
                "type": "boolean", "label": "Local Output Voice Lift", "default": False,
                "setup": False, "help": "Apply Voice Lift to the local output.",
            },
            "local_delay_ms": {
                "type": "integer", "label": "Local Output Delay", "default": 0,
                "min": 0, "max": 100, "setup": False, "help": "0 to 100 ms.",
            },
            "voice_lift_mute_threshold_db": {
                "type": "integer", "label": "Voice Lift Emergency Mute Threshold", "default": -20,
                "min": -50, "max": -3, "setup": False,
                "help": "Level at which Voice Lift mutes to stop feedback, -50 to -3 dB.",
            },
            "voice_lift_mute_time_s": {
                "type": "integer", "label": "Voice Lift Emergency Mute Time", "default": 3,
                "min": 1, "max": 30, "setup": False,
                "help": "How long the emergency mute lasts, 1 to 30 s.",
            },
            "reference_auto_adjust": {
                "type": "boolean", "label": "Reference Input Auto Adjust", "default": True,
                "setup": False,
                "help": "Set the far-end detection threshold automatically. Turn off to use the manual gain below.",
            },
            "reference_gain_db": {
                "type": "integer", "label": "Reference Input Gain", "default": 0,
                "min": -60, "max": 10, "setup": False,
                "help": "Manual far-end detection gain, -60 to 10 dB. Only accepted while Auto Adjust is off.",
            },
            "eq_125_db": {"type": "integer", "label": "EQ 125 Hz", "default": 0, "min": -8, "max": 8, "setup": False, "help": "-8 to 8 dB; shared by every output with its Equalizer on."},
            "eq_250_db": {"type": "integer", "label": "EQ 250 Hz", "default": 0, "min": -8, "max": 8, "setup": False, "help": "-8 to 8 dB."},
            "eq_500_db": {"type": "integer", "label": "EQ 500 Hz", "default": 0, "min": -8, "max": 8, "setup": False, "help": "-8 to 8 dB."},
            "eq_1k_db": {"type": "integer", "label": "EQ 1 kHz", "default": 0, "min": -8, "max": 8, "setup": False, "help": "-8 to 8 dB."},
            "eq_2k_db": {"type": "integer", "label": "EQ 2 kHz", "default": 0, "min": -8, "max": 8, "setup": False, "help": "-8 to 8 dB."},
            "eq_4k_db": {"type": "integer", "label": "EQ 4 kHz", "default": 0, "min": -8, "max": 8, "setup": False, "help": "-8 to 8 dB."},
            "eq_8k_db": {"type": "integer", "label": "EQ 8 kHz", "default": 0, "min": -8, "max": 8, "setup": False, "help": "-8 to 8 dB."},
            "noise_gate_threshold_db": {
                "type": "integer", "label": "Noise Gate Threshold", "default": -80,
                "min": -90, "max": -40, "setup": False,
                "help": "-90 to -40 dB; shared by every output with its Noise Gate on.",
            },
            "noise_gate_hold_ms": {
                "type": "integer", "label": "Noise Gate Hold Time", "default": 350,
                "min": 50, "max": 1000, "setup": False, "help": "50 to 1000 ms.",
            },
            "led_brightness": {
                "type": "integer", "label": "LED Ring Brightness", "default": 5,
                "min": 0, "max": 5, "setup": False, "help": "0 is off, 5 is full brightness.",
            },
            "led_show_farend_activity": {
                "type": "boolean", "label": "LED Shows Far-end Activity", "default": False,
                "setup": False, "help": "Indicate far-end speech on the ring.",
            },
            "led_mic_on_color": {
                "type": "enum", "label": "LED Colour When Live", "default": "Green",
                "values": [{'value': 'LightGreen', 'label': 'Light green'}, {'value': 'Green', 'label': 'Green'}, {'value': 'Blue', 'label': 'Blue'}, {'value': 'Red', 'label': 'Red'}, {'value': 'Yellow', 'label': 'Yellow'}, {'value': 'Orange', 'label': 'Orange'}, {'value': 'Cyan', 'label': 'Cyan'}, {'value': 'Pink', 'label': 'Pink'}], "setup": False,
            },
            "led_mic_mute_color": {
                "type": "enum", "label": "LED Colour When Muted", "default": "Red",
                "values": [{'value': 'LightGreen', 'label': 'Light green'}, {'value': 'Green', 'label': 'Green'}, {'value': 'Blue', 'label': 'Blue'}, {'value': 'Red', 'label': 'Red'}, {'value': 'Yellow', 'label': 'Yellow'}, {'value': 'Orange', 'label': 'Orange'}, {'value': 'Cyan', 'label': 'Cyan'}, {'value': 'Pink', 'label': 'Pink'}], "setup": False,
            },
            "led_custom_enabled": {
                "type": "boolean", "label": "LED Custom Colour Enabled", "default": False,
                "setup": False,
                "help": "Show the custom colour regardless of mute, for a room status light.",
            },
            "led_custom_color": {
                "type": "enum", "label": "LED Custom Colour", "default": "Green",
                "values": [{'value': 'LightGreen', 'label': 'Light green'}, {'value': 'Green', 'label': 'Green'}, {'value': 'Blue', 'label': 'Blue'}, {'value': 'Red', 'label': 'Red'}, {'value': 'Yellow', 'label': 'Yellow'}, {'value': 'Orange', 'label': 'Orange'}, {'value': 'Cyan', 'label': 'Cyan'}, {'value': 'Pink', 'label': 'Pink'}], "setup": False,
            },
            "denoiser": {
                "type": "enum", "label": "Denoiser", "default": "Off",
                "values": ['Off', 'Low', 'Medium', 'High'], "setup": False,
                "help": "Noise reduction strength.",
            },
            "input_lowcuts": {
                "type": "boolean", "label": "Input Low-cut Filters", "default": True,
                "setup": False, "help": "Cut rumble below the voice band. Normally on.",
            },
            "single_capsule_mode": {
                "type": "boolean", "label": "Single Capsule Mode", "default": False,
                "setup": False,
                "help": "Route one raw capsule to the output, for calibration only.",
            },
            "beam_freeze_auto_hold": {
                "type": "boolean", "label": "Beam Freeze Auto Hold", "default": True,
                "setup": False,
                "help": "Let near-end speech release a beam frozen by far-end speech.",
            },
            "beam_freeze_hold_ms": {
                "type": "integer", "label": "Beam Freeze Hold Time", "default": 100,
                "min": 50, "max": 500, "setup": False,
                "help": "How long near-end speech must last before it releases the beam, 50 to 500 ms.",
            },
            'exclusion_zone_1_enabled': {'type': 'boolean', 'label': 'Exclusion Zone 1: Enabled', 'default': False, 'setup': False, 'help': 'Switch exclusion zone 1 on or off. Its angles are the four settings below.'},
            'exclusion_zone_1_azimuth_min': {'type': 'integer', 'label': 'Exclusion Zone 1: Azimuth Min', 'default': 0, 'min': 0, 'max': 360, 'setup': False, 'help': 'Start of the zone around the microphone, 0 to 360 in 5 degree steps. The zone must be at least 10 degrees wide in both directions; the microphone refuses a narrower one.'},
            'exclusion_zone_1_azimuth_max': {'type': 'integer', 'label': 'Exclusion Zone 1: Azimuth Max', 'default': 360, 'min': 0, 'max': 360, 'setup': False, 'help': 'End of the zone around the microphone, 0 to 360 in 5 degree steps. The zone must be at least 10 degrees wide in both directions; the microphone refuses a narrower one.'},
            'exclusion_zone_1_elevation_min': {'type': 'integer', 'label': 'Exclusion Zone 1: Elevation Min', 'default': 0, 'min': 0, 'max': 90, 'setup': False, 'help': 'Lower edge of the zone, 0 (horizontal) to 90 (straight down) in 5 degree steps. The zone must be at least 10 degrees wide in both directions; the microphone refuses a narrower one.'},
            'exclusion_zone_1_elevation_max': {'type': 'integer', 'label': 'Exclusion Zone 1: Elevation Max', 'default': 10, 'min': 0, 'max': 90, 'setup': False, 'help': 'Upper edge of the zone, 0 (horizontal) to 90 (straight down) in 5 degree steps. The zone must be at least 10 degrees wide in both directions; the microphone refuses a narrower one.'},
            'exclusion_zone_2_enabled': {'type': 'boolean', 'label': 'Exclusion Zone 2: Enabled', 'default': False, 'setup': False, 'help': 'Switch exclusion zone 2 on or off. Its angles are the four settings below.'},
            'exclusion_zone_2_azimuth_min': {'type': 'integer', 'label': 'Exclusion Zone 2: Azimuth Min', 'default': 0, 'min': 0, 'max': 360, 'setup': False, 'help': 'Start of the zone around the microphone, 0 to 360 in 5 degree steps. The zone must be at least 10 degrees wide in both directions; the microphone refuses a narrower one.'},
            'exclusion_zone_2_azimuth_max': {'type': 'integer', 'label': 'Exclusion Zone 2: Azimuth Max', 'default': 360, 'min': 0, 'max': 360, 'setup': False, 'help': 'End of the zone around the microphone, 0 to 360 in 5 degree steps. The zone must be at least 10 degrees wide in both directions; the microphone refuses a narrower one.'},
            'exclusion_zone_2_elevation_min': {'type': 'integer', 'label': 'Exclusion Zone 2: Elevation Min', 'default': 0, 'min': 0, 'max': 90, 'setup': False, 'help': 'Lower edge of the zone, 0 (horizontal) to 90 (straight down) in 5 degree steps. The zone must be at least 10 degrees wide in both directions; the microphone refuses a narrower one.'},
            'exclusion_zone_2_elevation_max': {'type': 'integer', 'label': 'Exclusion Zone 2: Elevation Max', 'default': 10, 'min': 0, 'max': 90, 'setup': False, 'help': 'Upper edge of the zone, 0 (horizontal) to 90 (straight down) in 5 degree steps. The zone must be at least 10 degrees wide in both directions; the microphone refuses a narrower one.'},
            'exclusion_zone_3_enabled': {'type': 'boolean', 'label': 'Exclusion Zone 3: Enabled', 'default': False, 'setup': False, 'help': 'Switch exclusion zone 3 on or off. Its angles are the four settings below.'},
            'exclusion_zone_3_azimuth_min': {'type': 'integer', 'label': 'Exclusion Zone 3: Azimuth Min', 'default': 0, 'min': 0, 'max': 360, 'setup': False, 'help': 'Start of the zone around the microphone, 0 to 360 in 5 degree steps. The zone must be at least 10 degrees wide in both directions; the microphone refuses a narrower one.'},
            'exclusion_zone_3_azimuth_max': {'type': 'integer', 'label': 'Exclusion Zone 3: Azimuth Max', 'default': 360, 'min': 0, 'max': 360, 'setup': False, 'help': 'End of the zone around the microphone, 0 to 360 in 5 degree steps. The zone must be at least 10 degrees wide in both directions; the microphone refuses a narrower one.'},
            'exclusion_zone_3_elevation_min': {'type': 'integer', 'label': 'Exclusion Zone 3: Elevation Min', 'default': 0, 'min': 0, 'max': 90, 'setup': False, 'help': 'Lower edge of the zone, 0 (horizontal) to 90 (straight down) in 5 degree steps. The zone must be at least 10 degrees wide in both directions; the microphone refuses a narrower one.'},
            'exclusion_zone_3_elevation_max': {'type': 'integer', 'label': 'Exclusion Zone 3: Elevation Max', 'default': 10, 'min': 0, 'max': 90, 'setup': False, 'help': 'Upper edge of the zone, 0 (horizontal) to 90 (straight down) in 5 degree steps. The zone must be at least 10 degrees wide in both directions; the microphone refuses a narrower one.'},
            'exclusion_zone_4_enabled': {'type': 'boolean', 'label': 'Exclusion Zone 4: Enabled', 'default': False, 'setup': False, 'help': 'Switch exclusion zone 4 on or off. Its angles are the four settings below.'},
            'exclusion_zone_4_azimuth_min': {'type': 'integer', 'label': 'Exclusion Zone 4: Azimuth Min', 'default': 0, 'min': 0, 'max': 360, 'setup': False, 'help': 'Start of the zone around the microphone, 0 to 360 in 5 degree steps. The zone must be at least 10 degrees wide in both directions; the microphone refuses a narrower one.'},
            'exclusion_zone_4_azimuth_max': {'type': 'integer', 'label': 'Exclusion Zone 4: Azimuth Max', 'default': 360, 'min': 0, 'max': 360, 'setup': False, 'help': 'End of the zone around the microphone, 0 to 360 in 5 degree steps. The zone must be at least 10 degrees wide in both directions; the microphone refuses a narrower one.'},
            'exclusion_zone_4_elevation_min': {'type': 'integer', 'label': 'Exclusion Zone 4: Elevation Min', 'default': 0, 'min': 0, 'max': 90, 'setup': False, 'help': 'Lower edge of the zone, 0 (horizontal) to 90 (straight down) in 5 degree steps. The zone must be at least 10 degrees wide in both directions; the microphone refuses a narrower one.'},
            'exclusion_zone_4_elevation_max': {'type': 'integer', 'label': 'Exclusion Zone 4: Elevation Max', 'default': 10, 'min': 0, 'max': 90, 'setup': False, 'help': 'Upper edge of the zone, 0 (horizontal) to 90 (straight down) in 5 degree steps. The zone must be at least 10 degrees wide in both directions; the microphone refuses a narrower one.'},
            'exclusion_zone_5_enabled': {'type': 'boolean', 'label': 'Exclusion Zone 5: Enabled', 'default': False, 'setup': False, 'help': 'Switch exclusion zone 5 on or off. Its angles are the four settings below.'},
            'exclusion_zone_5_azimuth_min': {'type': 'integer', 'label': 'Exclusion Zone 5: Azimuth Min', 'default': 0, 'min': 0, 'max': 360, 'setup': False, 'help': 'Start of the zone around the microphone, 0 to 360 in 5 degree steps. The zone must be at least 10 degrees wide in both directions; the microphone refuses a narrower one.'},
            'exclusion_zone_5_azimuth_max': {'type': 'integer', 'label': 'Exclusion Zone 5: Azimuth Max', 'default': 360, 'min': 0, 'max': 360, 'setup': False, 'help': 'End of the zone around the microphone, 0 to 360 in 5 degree steps. The zone must be at least 10 degrees wide in both directions; the microphone refuses a narrower one.'},
            'exclusion_zone_5_elevation_min': {'type': 'integer', 'label': 'Exclusion Zone 5: Elevation Min', 'default': 0, 'min': 0, 'max': 90, 'setup': False, 'help': 'Lower edge of the zone, 0 (horizontal) to 90 (straight down) in 5 degree steps. The zone must be at least 10 degrees wide in both directions; the microphone refuses a narrower one.'},
            'exclusion_zone_5_elevation_max': {'type': 'integer', 'label': 'Exclusion Zone 5: Elevation Max', 'default': 10, 'min': 0, 'max': 90, 'setup': False, 'help': 'Upper edge of the zone, 0 (horizontal) to 90 (straight down) in 5 degree steps. The zone must be at least 10 degrees wide in both directions; the microphone refuses a narrower one.'},
            'priority_zone_enabled': {'type': 'boolean', 'label': 'Priority Zone: Enabled', 'default': False, 'setup': False, 'help': 'Switch priority zone on or off. Its angles are the four settings below.'},
            'priority_zone_azimuth_min': {'type': 'integer', 'label': 'Priority Zone: Azimuth Min', 'default': 0, 'min': 0, 'max': 360, 'setup': False, 'help': 'Start of the zone around the microphone, 0 to 360 in 5 degree steps. The zone must be at least 15 degrees wide in both directions; the microphone refuses a narrower one.'},
            'priority_zone_azimuth_max': {'type': 'integer', 'label': 'Priority Zone: Azimuth Max', 'default': 360, 'min': 0, 'max': 360, 'setup': False, 'help': 'End of the zone around the microphone, 0 to 360 in 5 degree steps. The zone must be at least 15 degrees wide in both directions; the microphone refuses a narrower one.'},
            'priority_zone_elevation_min': {'type': 'integer', 'label': 'Priority Zone: Elevation Min', 'default': 0, 'min': 0, 'max': 90, 'setup': False, 'help': 'Lower edge of the zone, 0 (horizontal) to 90 (straight down) in 5 degree steps. The zone must be at least 15 degrees wide in both directions; the microphone refuses a narrower one.'},
            'priority_zone_elevation_max': {'type': 'integer', 'label': 'Priority Zone: Elevation Max', 'default': 10, 'min': 0, 'max': 90, 'setup': False, 'help': 'Upper edge of the zone, 0 (horizontal) to 90 (straight down) in 5 degree steps. The zone must be at least 15 degrees wide in both directions; the microphone refuses a narrower one.'},
            "priority_zone_weight": {
                "type": "number", "label": "Priority Zone: Weight", "default": 1.5,
                "min": 1, "max": 4, "setup": False,
                "help": "How strongly the beam favours a talker in the zone, 1.0 (none) to 4.0, in steps of 0.1.",
            },
        },
        "discovery": {
            # /api/device/identity answers without credentials (its OpenAPI
            # entry declares no security), and only a TCC M says TCCM there.
            # HTTPS only, self-signed, so the probe is a TLS GET.
            "tcp_probe": {
                "port": 443,
                "tls": True,
                "send_ascii": (
                    "GET /api/device/identity HTTP/1.1\r\n"
                    "Host: tccm\r\nConnection: close\r\n\r\n"
                ),
                "expect_regex": '"product"\\s*:\\s*"TCCM"',
                "extract_manufacturer": "Sennheiser",
                "extract": {
                    "model": {"regex": '"product"\\s*:\\s*"([A-Za-z0-9 ]+)"', "group": 1},
                },
            },
            # Sennheiser electronic GmbH & Co. KG (IEEE registry).
            "oui": ["00:1b:66"],
            "manufacturer_alias": ["Sennheiser"],
        },
    }

    # ── Construction ──

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._client: httpx.AsyncClient | None = None
        self._event_task: asyncio.Task | None = None
        self._session_uuid: str = ""
        self._subscribed: set[str] = set()
        # Resources this microphone's firmware does not have (404 on read or
        # refused in a subscription): read once, skipped after.
        self._unsupported: set[str] = set()
        self._talker_feed = False
        self._meters_feed = False
        self._last_fast_write: dict[str, float] = {}
        # A feed value held until its rate-limit window expires.
        self._fast_pending: dict[str, Any] = {}

    # ── Connection lifecycle ──

    def _base_url(self) -> str:
        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", 443) or 443)
        return f"https://{host}:{port}"

    async def _pre_connect(self) -> None:
        if not str(self.config.get("host", "")).strip():
            raise ConnectionError("The microphone's IP address is required.")
        self._talker_feed = _as_bool(self.config.get("enable_talker_position", False))
        self._meters_feed = _as_bool(self.config.get("enable_meters", False))

    async def _create_transport(self, transport_type: str) -> None:
        """Driver-owned session: one httpx client for requests and the event
        stream. ``self.transport`` stays None; _link_alive()/_close_session()
        report and retire the client instead."""
        password = str(self.config.get("password", "") or "")
        timeout = float(self.config.get("timeout", 5.0) or 5.0)
        self._client = httpx.AsyncClient(
            base_url=self._base_url(),
            auth=httpx.BasicAuth(_API_USER, password),
            verify=_as_bool(self.config.get("verify_ssl", False)),
            timeout=timeout,
        )

    async def _post_connect(self) -> None:
        """Prove the device is a TCC M and the password is good before
        `connected` is declared."""
        host = str(self.config.get("host", "")).strip()
        if not str(self.config.get("password", "") or ""):
            # A login that cannot succeed is never sent (the device counts
            # failed logins). auth_failed pauses reconnects until the
            # credentials change.
            raise ConnectionFaultError(
                "No third-party password is set. Enable third-party access on "
                "the microphone's device page in Sennheiser Control Cockpit, "
                "set a password there, and enter it here.",
                code="auth_failed",
            )
        try:
            identity = await self._get_json("/api/device/identity")
            self._apply_body(_RESOURCE_BY_PATH["/api/device/identity"], identity)
            product = str(identity.get("product", ""))
            if product != _PRODUCT:
                raise ConnectionFaultError(
                    f"The device at {host} answers as \"{product or 'unknown'}\", "
                    f"not a TeamConnect Ceiling Medium. Check the IP address.",
                    code="invalid_config",
                )
            # First authenticated read: a wrong password is a 401 here.
            state = await self._get_json("/api/device/state")
            self._apply_body(_RESOURCE_BY_PATH["/api/device/state"], state)
        except httpx.ConnectError as exc:
            text = str(exc)
            if "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text:
                raise ConnectionFaultError(
                    "The microphone's TLS certificate is not trusted. Turn off "
                    "\"Verify TLS Certificate\" for this device, or install a "
                    "trusted certificate on the microphone.",
                    code="tls_cert_untrusted",
                ) from exc
            raise ConnectionError(
                f"Could not reach the microphone at {host}: {exc}"
            ) from exc
        except httpx.TransportError as exc:
            raise ConnectionError(
                f"Could not reach the microphone at {host}: {exc}"
            ) from exc
        self.set_state("last_error", None)
        log.info(f"[{self.device_id}] Connected to TeamConnect Ceiling Medium at {host}")

    async def _initial_sync(self) -> None:
        # Populate everything once, then let the stream keep it current.
        await self.poll()
        self.set_state("talker_position_feed", self._talker_feed)
        self.set_state("meters_feed", self._meters_feed)
        self._event_task = asyncio.create_task(self._event_loop())

    def _link_alive(self) -> bool:
        return self._client is not None

    async def _close_session(self) -> None:
        task, self._event_task = self._event_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        client, self._client = self._client, None
        self._session_uuid = ""
        self._subscribed = set()
        if client is not None:
            await client.aclose()

    # ── Requests ──

    async def _request(
        self, method: str, path: str, body: Any = None
    ) -> httpx.Response:
        """One request. Transport errors propagate (the poll contract);
        401/403 raise the typed auth fault; other statuses come back to the
        caller, which knows what each means for its resource."""
        if self._client is None:
            raise ConnectionError("Not connected")
        response = await self._client.request(method, path, json=body)
        if response.status_code == 401:
            raise ConnectionFaultError(
                "The microphone rejected the third-party password. Check it "
                "on the microphone's device page in Sennheiser Control "
                "Cockpit.",
                code="auth_failed",
            )
        if response.status_code == 403:
            raise ConnectionFaultError(
                "The microphone refused third-party access. Enable it on the "
                "microphone's device page in Sennheiser Control Cockpit.",
                code="auth_failed",
            )
        return response

    async def _get_json(self, path: str) -> dict[str, Any]:
        response = await self._request("GET", path)
        if response.status_code == 404:
            raise LookupError(path)
        if response.is_error:
            raise ConnectionError(
                f"The microphone answered HTTP {response.status_code} for {path}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ConnectionError(
                f"The microphone answered {path} with something other than JSON"
            ) from exc
        return payload if isinstance(payload, (dict, list)) else {}

    async def _put(self, path: str, body: dict[str, Any], what: str) -> None:
        """Write one resource. A refusal names the setting and the reason,
        and is recorded in last_error; the state itself is never assumed,
        the microphone reports the new value over the stream."""
        response = await self._request("PUT", path, body)
        if response.is_success:
            return
        reason = self._refusal_reason(response)
        message = f"The microphone refused {what}: {reason}"
        self.set_state("last_error", message)
        raise ValueError(message)

    @staticmethod
    def _refusal_reason(response: httpx.Response) -> str:
        detail = ""
        text = (response.text or "").strip()
        if text:
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                detail = str(
                    payload.get("message")
                    or payload.get("error")
                    or payload.get("description")
                    or ""
                )
            if not detail:
                detail = text[:160]
        code = response.status_code
        if code == 409:
            base = "the microphone's current state does not allow it"
        elif code == 422:
            base = "the value is out of range or conflicts with another setting"
        elif code == 400:
            base = "the request was not understood"
        elif code == 404:
            base = "this microphone's firmware does not have that setting"
        else:
            base = f"HTTP {code}"
        return f"{base} ({detail})" if detail else base

    # ── State mirroring ──

    def _apply_body(self, resource: _Resource, body: Any) -> None:
        """Copy a resource body into state, one write per field present."""
        if not isinstance(body, dict):
            return
        updates: dict[str, Any] = {}
        for field, key in resource.fields.items():
            value = _dig(body, field)
            if value is None and field not in body:
                continue
            updates[key] = self._coerce(key, value)
        if updates:
            self.set_states(updates)

    def _coerce(self, key: str, value: Any) -> Any:
        if key == "warnings":
            if isinstance(value, list):
                return "; ".join(str(v) for v in value)
            return str(value or "")
        var_def = self.DRIVER_INFO["state_variables"].get(key, {})
        vtype = var_def.get("type")
        if vtype == "boolean":
            return _as_bool(value)
        if vtype == "integer" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(round(value))
        if vtype == "number" and isinstance(value, (int, float)):
            return float(value)
        if vtype in ("string", "enum") and value is not None:
            return str(value)
        return value

    def _apply_notification(self, payload: Any) -> None:
        """One SSE data block: {"/api/<resource>": {...}, ...}. A resource
        the device sends under a different prefix is matched by its path
        after /api; a zone collection body (a list) is fanned out by id."""
        if not isinstance(payload, dict):
            return
        for raw_path, body in payload.items():
            path = self._normalize_path(str(raw_path))
            if path in _ZONE_COLLECTIONS and isinstance(body, list):
                for entry in body:
                    if isinstance(entry, dict) and "id" in entry:
                        sub = _RESOURCE_BY_PATH.get(f"{path}/{entry['id']}")
                        if sub is not None:
                            self._apply_body(sub, entry)
                continue
            resource = _RESOURCE_BY_PATH.get(path)
            if resource is None or body is None:
                continue
            if resource.fast:
                self._apply_fast(resource, body)
                continue
            self._apply_body(resource, body)

    def _apply_fast(self, resource: _Resource, body: Any) -> None:
        """Rate-limit a feed: at most one state write per resource per
        _FAST_MIN_INTERVAL_S, and the LATEST value always lands. A value
        inside the window is held and written when the window expires, so a
        talker's final position is never the one that was dropped."""
        path = resource.path
        now = time.monotonic()
        last = self._last_fast_write.get(path, 0.0)
        if now - last >= _FAST_MIN_INTERVAL_S and path not in self._fast_pending:
            self._last_fast_write[path] = now
            self._apply_body(resource, body)
            return
        first_hold = path not in self._fast_pending
        self._fast_pending[path] = body
        if first_hold:
            delay = max(0.0, _FAST_MIN_INTERVAL_S - (now - last))
            asyncio.get_running_loop().call_later(
                delay, self._flush_fast, resource,
            )

    def _flush_fast(self, resource: _Resource) -> None:
        body = self._fast_pending.pop(resource.path, None)
        if body is None or self._client is None:
            return
        self._last_fast_write[resource.path] = time.monotonic()
        self._apply_body(resource, body)

    @staticmethod
    def _normalize_path(path: str) -> str:
        path = path.split("?", 1)[0].rstrip("/")
        if not path.startswith("/api/"):
            path = "/api" + (path if path.startswith("/") else "/" + path)
        return path

    # ── Polling ──

    async def poll(self) -> None:
        """Resync every resource that is not a feed. Transport errors
        propagate so the platform's missed-poll watchdog sees them; a
        resource this firmware lacks is skipped after its first 404."""
        if self._client is None:
            return
        for resource in _RESOURCES:
            if not resource.poll or resource.fast or resource.path in self._unsupported:
                continue
            try:
                body = await self._get_json(resource.path)
            except LookupError:
                self._unsupported.add(resource.path)
                log.info(
                    f"[{self.device_id}] {resource.path} is not on this "
                    f"microphone's firmware; skipping it"
                )
                continue
            self._apply_body(resource, body)

    # ── Event stream (SSCv2 subscription) ──

    def _wanted_paths(self) -> list[str]:
        wanted: list[str] = []
        for resource in _RESOURCES:
            if not resource.subscribe or resource.path in self._unsupported:
                continue
            if resource.fast:
                if resource.path == "/api/audio/inputs/microphone/beam/direction":
                    if not self._talker_feed:
                        continue
                elif not self._meters_feed:
                    continue
            wanted.append(resource.path)
        return wanted

    async def _event_loop(self) -> None:
        """Hold the subscription stream open, arm it, apply what arrives.

        Every (re)open yields a new session: the UUID comes from the
        Content-Location header (and again in the ``open`` event), the
        resource list is PUT to it, and the microphone answers with the
        current value of each. Silence longer than _STREAM_IDLE_REOPEN_S
        reopens the stream; failures back off and never take the device
        down, since polling remains the safety net.
        """
        attempts = 0
        warned = False
        while self._client is not None:
            timeout_s = float(self.config.get("timeout", 5.0) or 5.0)
            timeout = httpx.Timeout(
                connect=timeout_s, read=_STREAM_IDLE_REOPEN_S,
                write=timeout_s, pool=None,
            )
            try:
                async with self._client.stream(
                    "GET", _SUBSCRIPTIONS,
                    headers={"Accept": "text/event-stream"}, timeout=timeout,
                ) as response:
                    if response.status_code != 200:
                        await response.aread()
                        raise ConnectionError(
                            f"subscription stream rejected with HTTP "
                            f"{response.status_code}"
                        )
                    attempts = 0
                    warned = False
                    location = response.headers.get("content-location", "")
                    uuid = location.rstrip("/").rsplit("/", 1)[-1] if location else ""
                    if uuid:
                        await self._arm_session(uuid)
                    event_type = ""
                    data_lines: list[str] = []
                    async for line in response.aiter_lines():
                        if line == "":
                            if data_lines:
                                await self._handle_event(event_type, "\n".join(data_lines))
                            event_type = ""
                            data_lines = []
                            continue
                        if line.startswith(":"):
                            continue
                        field, _, value = line.partition(":")
                        if value.startswith(" "):
                            value = value[1:]
                        if field == "data":
                            data_lines.append(value)
                        elif field == "event":
                            event_type = value.strip()
                    if data_lines:
                        await self._handle_event(event_type, "\n".join(data_lines))
                log.debug(f"[{self.device_id}] Subscription stream ended; reopening")
            except asyncio.CancelledError:
                raise
            except httpx.ReadTimeout:
                log.debug(f"[{self.device_id}] Subscription stream idle; reopening")
                continue
            except Exception as exc:
                if self._client is None:
                    return
                attempts += 1
                msg = (
                    f"[{self.device_id}] Subscription stream failed "
                    f"({str(exc) or type(exc).__name__}); retrying"
                )
                if warned:
                    log.debug(msg)
                else:
                    log.warning(msg)
                    warned = True
                await asyncio.sleep(min(2.0 * attempts, 30.0))
            finally:
                self._session_uuid = ""
                self._subscribed = set()

    async def _handle_event(self, event_type: str, data: str) -> None:
        try:
            payload = json.loads(data)
        except ValueError:
            log.debug(f"[{self.device_id}] Unparseable event: {data[:120]!r}")
            return
        if event_type == "close":
            return
        if isinstance(payload, dict) and "sessionUUID" in payload and not self._session_uuid:
            # The open event, when the header did not carry the UUID.
            await self._arm_session(str(payload["sessionUUID"]))
            return
        if event_type == "open":
            return
        self._apply_notification(payload)

    async def _arm_session(self, uuid: str) -> None:
        self._session_uuid = uuid
        self._subscribed = set()
        await self._sync_subscription()

    async def _sync_subscription(self) -> None:
        """PUT the wanted resource list to the session. The device refuses
        the whole list if one path is unknown to its firmware (400 naming
        it), so a refused path is dropped and the list re-sent."""
        uuid = self._session_uuid
        if not uuid or self._client is None:
            return
        wanted = self._wanted_paths()
        for _attempt in range(len(wanted) + 1):
            if set(wanted) == self._subscribed:
                return
            response = await self._request("PUT", f"{_SUBSCRIPTIONS}/{uuid}", wanted)
            if response.is_success:
                self._subscribed = set(wanted)
                log.info(
                    f"[{self.device_id}] Subscribed to {len(wanted)} resources"
                )
                return
            if response.status_code == 422:
                # Session gone (stream closed underneath us); the reopen
                # will arm a new one.
                self._session_uuid = ""
                return
            refused = ""
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    refused = self._normalize_path(str(payload.get("path", "")))
            except ValueError:
                pass
            if response.status_code == 400 and refused in wanted:
                self._unsupported.add(refused)
                wanted = [p for p in wanted if p != refused]
                log.info(
                    f"[{self.device_id}] {refused} cannot be subscribed on this "
                    f"microphone's firmware; skipping it"
                )
                continue
            log.warning(
                f"[{self.device_id}] Subscription refused: HTTP "
                f"{response.status_code} {response.text[:160]!r}"
            )
            return

    async def _set_feed(self, feed: str, enabled: bool) -> None:
        if feed == "talker":
            self._talker_feed = enabled
            self.set_state("talker_position_feed", enabled)
        else:
            self._meters_feed = enabled
            self.set_state("meters_feed", enabled)
        await self._sync_subscription()

    # ── Commands ──

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        if command == "mute_on":
            await self._put("/api/audio/outputs/global/mute", {"enabled": True}, "mute")
        elif command == "mute_off":
            await self._put("/api/audio/outputs/global/mute", {"enabled": False}, "unmute")
        elif command == "mute_toggle":
            muted = _as_bool(self.get_state("mute"))
            await self._put(
                "/api/audio/outputs/global/mute", {"enabled": not muted},
                "unmute" if muted else "mute",
            )
        elif command == "identify_on":
            await self._put("/api/device/identification", {"visual": True}, "identify")
        elif command == "identify_off":
            await self._put("/api/device/identification", {"visual": False}, "identify off")
        elif command == "set_analog_gain":
            await self._put(
                "/api/audio/outputs/analog", {"gain": int(params["gain"])},
                "the analog output gain",
            )
        elif command == "set_analog_output_source":
            await self._put(
                "/api/audio/outputs/analog", {"switch": str(params["source"])},
                "the analog output source",
            )
        elif command == "set_farend_gain":
            await self._put(
                "/api/audio/outputs/dante/farEnd", {"gain": int(params["gain"])},
                "the far-end output gain",
            )
        elif command == "set_local_gain":
            await self._put(
                "/api/audio/outputs/dante/local", {"gain": int(params["gain"])},
                "the local output gain",
            )
        elif command == "set_reference_gain":
            await self._write_reference_gain(int(params["gain"]))
        elif command == "reference_auto_adjust_on":
            await self._put(
                "/api/audio/inputs/dante/reference", {"farEndAutoAdjustEnabled": True},
                "reference auto adjust",
            )
        elif command == "reference_auto_adjust_off":
            await self._put(
                "/api/audio/inputs/dante/reference", {"farEndAutoAdjustEnabled": False},
                "reference auto adjust",
            )
        elif command == "set_denoiser":
            await self._put(
                "/api/audio/inputs/microphone/denoiser", {"setting": str(params["level"])},
                "the denoiser level",
            )
        elif command == "set_eq_band":
            band = str(params["band"])
            if band not in _EQ_BANDS:
                raise ValueError(f"Unknown EQ band {band!r}")
            gains = self._current_eq()
            gains[_EQ_BANDS.index(band)] = int(params["gain"])
            await self._put("/api/audio/equalizer", {"gains": gains}, f"the {band} Hz EQ band")
        elif command == "eq_flat":
            await self._put("/api/audio/equalizer", {"gains": [0] * 7}, "a flat EQ")
        elif command in ("exclusion_zone_on", "exclusion_zone_off"):
            zone = int(params["zone"])
            if not 1 <= zone <= _EXCLUSION_ZONES:
                raise ValueError(f"Exclusion zone must be 1 to {_EXCLUSION_ZONES}")
            await self._put(
                f"/api/audio/inputs/microphone/exclusionZones/{zone - 1}",
                {"enabled": command.endswith("_on")},
                f"exclusion zone {zone}",
            )
        elif command == "priority_zone_on":
            await self._put(
                "/api/audio/inputs/microphone/priorityZones/0", {"enabled": True},
                "the priority zone",
            )
        elif command == "priority_zone_off":
            await self._put(
                "/api/audio/inputs/microphone/priorityZones/0", {"enabled": False},
                "the priority zone",
            )
        elif command == "set_led_brightness":
            await self._put(
                "/api/device/leds/ring", {"brightness": int(params["brightness"])},
                "the LED brightness",
            )
        elif command == "set_led_colors":
            body: dict[str, Any] = {}
            if params.get("mic_on"):
                body["micOn"] = {"color": str(params["mic_on"])}
            if params.get("mic_mute"):
                body["micMute"] = {"color": str(params["mic_mute"])}
            if not body:
                raise ValueError("Pick a colour for live, muted, or both")
            await self._put("/api/device/leds/ring", body, "the LED colours")
        elif command == "led_custom_on":
            await self._put(
                "/api/device/leds/ring",
                {"micCustom": {"enabled": True, "color": str(params["color"])}},
                "the custom LED colour",
            )
        elif command == "led_custom_off":
            await self._put(
                "/api/device/leds/ring", {"micCustom": {"enabled": False}},
                "the custom LED colour",
            )
        elif command == "talker_position_on":
            await self._set_feed("talker", True)
        elif command == "talker_position_off":
            await self._set_feed("talker", False)
        elif command == "meters_on":
            await self._set_feed("meters", True)
        elif command == "meters_off":
            await self._set_feed("meters", False)
        elif command == "get_beam_position":
            resource = _RESOURCE_BY_PATH["/api/audio/inputs/microphone/beam/direction"]
            self._apply_body(resource, await self._get_json(resource.path))
        elif command == "get_levels":
            for path in (
                "/api/audio/inputs/microphone/level",
                "/api/audio/inputs/reference/level",
                "/api/audio/roomInUse/activityLevel",
            ):
                resource = _RESOURCE_BY_PATH[path]
                self._apply_body(resource, await self._get_json(path))
        else:
            raise ValueError(f"Unknown command: {command}")
        return None

    def _current_eq(self) -> list[int]:
        gains: list[int] = []
        for key in _EQ_KEYS:
            value = self.get_state(key)
            gains.append(int(value) if isinstance(value, (int, float)) else 0)
        return gains

    async def _write_reference_gain(self, gain: int) -> None:
        if _as_bool(self.get_state("reference_auto_adjust")):
            message = (
                "Turn Reference Input Auto Adjust off before setting the "
                "reference gain; the microphone refuses a manual gain while "
                "it is on."
            )
            self.set_state("last_error", message)
            raise ValueError(message)
        await self._put(
            "/api/audio/inputs/dante/reference", {"gain": gain},
            "the reference input gain",
        )

    # ── Device settings ──

    async def set_device_setting(self, key: str, value: Any) -> Any:
        """Write one setting to the resource that owns it. Multi-field
        resources take a partial body with just this field; the equalizer
        takes all seven gains; a nested zone field carries its sibling so
        the object stays whole."""
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        resource = _RESOURCE_BY_KEY.get(key)
        field = _FIELD_BY_KEY.get(key)
        if resource is None or field is None or key not in self.DRIVER_INFO["device_settings"]:
            raise ValueError(f"Unknown device setting: {key}")
        sdef = self.DRIVER_INFO["device_settings"][key]
        label = sdef.get("label", key)
        value = self._coerce_setting(sdef, value)
        step = _STEP_BY_KEY.get(key)
        if step is not None and not self._on_step(value, step):
            message = f"{label} must be a multiple of {step}"
            raise ValueError(message)
        if isinstance(value, float):
            value = round(value, 1)

        if key == "reference_gain_db":
            await self._write_reference_gain(int(value))
            return None
        if key in _EQ_KEYS:
            gains = self._current_eq()
            gains[_EQ_KEYS.index(key)] = int(value)
            await self._put(resource.path, {"gains": gains}, label)
            return None

        body: dict[str, Any] = _nest(field, value)
        if "." in field:
            # A nested object (elevation/azimuth/micOn/...): send the
            # sibling fields too, from the last reported values, so the
            # device sees the whole object and no field falls back.
            head = field.split(".", 1)[0]
            for other_field, other_key in resource.fields.items():
                if other_field == field or not other_field.startswith(head + "."):
                    continue
                current = self.get_state(other_key)
                if current is not None:
                    _deep_merge(body, _nest(other_field, current))
        await self._put(resource.path, body, label)
        return None

    @staticmethod
    def _coerce_setting(sdef: dict[str, Any], value: Any) -> Any:
        stype = sdef.get("type")
        if stype == "boolean":
            return _as_bool(value)
        if stype == "integer":
            return int(value)
        if stype == "number":
            return float(value)
        return str(value)

    @staticmethod
    def _on_step(value: Any, step: int | float) -> bool:
        if isinstance(step, float):
            scaled = round(float(value) / step)
            return abs(scaled * step - float(value)) < 1e-6
        return int(value) % int(step) == 0
