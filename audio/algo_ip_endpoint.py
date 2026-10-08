"""
OpenAVC Algo IP endpoint driver.

Controls Algo IP speakers, paging adapters, visual alerters, display
speakers, intercoms, the 8063 door controller and the 8450 console through
the RESTful API built into their firmware: HTTPS on port 443 (HTTP on 80 is
on by default too) with JSON bodies and a self-signed certificate. The API is
off until it is turned on under Advanced Settings > Admin on the device.

Protocol reference (the manufacturer's own): Algo's RESTful API Guide,
https://docs.algosolutions.com/docs/restful-api-guide, with the Algo Strobe
Light Pattern Reference Guide and the speaker, paging adapter and intercom
provisioning parameter guides for the setting names and values.

Authentication:
  The device offers three methods (``api.auth.basic``): Standard (0, Algo's
  recommendation), Basic (1) and None (-1). Standard signs every request with
  HMAC-SHA256 keyed on the RESTful API password over
  ``METHOD:URI:CONTENT_MD5:CONTENT_TYPE:TIMESTAMP:NONCE`` (the MD5 and type
  only when the request has a JSON body), sent as ``Authorization: hmac
  admin:<nonce>:<hex digest>`` with ``Date`` and, with a body,
  ``Content-MD5``. The device refuses a timestamp more than 30 seconds
  (``api.auth.tsvar``) from its own clock, so a device without NTP, or a
  clock corrected while connected, refuses a correct password. A refusal is
  checked against the device's own ``Date`` response header first: when that
  is more than five seconds from the request's timestamp, the driver keeps
  the difference, signs the request again in the device's clock and sends it
  once more. Only a refusal with the clocks in step is reported as
  ``auth_failed``, which stops OpenAVC reconnecting. Basic sends
  ``admin:<password>``; None sends nothing.

One driver for the whole line:
  Every product speaks the same API, and Algo's guide marks each call with
  the products it applies to. After connecting, the driver reads the model
  from the device's About page (firmware 5.4 or newer) and offers only the
  commands and device settings that model has, with that model's own strobe
  patterns. A model it cannot identify (older firmware) keeps the full set,
  and a call the device does not have is refused by the device with its
  reason in ``last_error``.

Polling:
  The API has no notification channel, so everything is polled: the Status
  page (call, multicast, SIP, scheduler, console and sensor status) and the
  relay inputs, ambient noise and console events every ``poll_interval``
  seconds; the device settings every minute and straight after a write; the
  About page and the tone list every ten minutes. Tones, strobes and screens
  are commands only: the device reports nothing about what is playing or
  showing, so the driver keeps no state for them.

Why Python (not YAML):
  The Standard method signs each request (a nonce, a timestamp, an MD5 of the
  body, an HMAC over all of them), which no HTTP ``auth_type`` produces. The
  per-model narrowing of commands, settings and strobe patterns, the
  strobe brightness scale that differs by model (1 to 3 on the 8190S, 0 to
  255 elsewhere) and the tone stop that needs the tone's name on firmware
  5.4 and older are the other reasons; HMAC signing alone would not convert
  it.
"""

from __future__ import annotations

import base64
import copy
import email.utils
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import date, timezone
from typing import Any

import httpx

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.utils.logger import get_logger

log = get_logger(__name__)

# The account every request is made as (the guide's Authorization header and
# Basic example; "algo" is accepted too, for legacy compatibility).
_USER = "admin"

# api.auth.tsvar: how far the request timestamp may be from the device's clock.
_TIMESTAMP_TOLERANCE_S = 30

# A refusal whose Date header is further than this from the timestamp the
# request was signed with is put down to the clocks, and the request is signed
# again in the device's clock. Well inside the 30 s default because the window
# is a setting on the device (api.auth.tsvar) and may be set tighter; above the
# second or two that whole-second Date headers and a round trip leave between
# clocks in step. A false auth_failed leaves the device offline until someone
# presses Reconnect; the cost of this margin is one more refused request when
# the password is wrong and the clocks are 5 s or more apart.
_CLOCK_REFUSAL_S = 5

_ABOUT = "/api/info/about"
_STATUS = "/api/info/status"
_TONELIST = "/api/info/tonelist"
_RELAY_INPUT = "/api/info/input.relay.status"
_RELAY_INPUT_1 = "/api/info/input.relay1.status"
_RELAY_INPUT_2 = "/api/info/input.relay2.status"
_NOISE_LEVEL = "/api/info/audio.noise.level"
_CONSOLE_EVENTS = "/api/console/event/status"
_SETTINGS = "/api/settings"
_SCHEDULES = "/api/schedules"

# The setting every unit has (the REST API's own on/off). Read to prove the
# API answers on firmware older than the About page, and by the liveness probe.
_API_FLAG = "admin.web.api"

_SETTINGS_INTERVAL_S = 60.0
_RESYNC_INTERVAL_S = 600.0

# ── Product families, from the guide's "Applicable Products" lines ──

_SPEAKERS = frozenset({
    "8180", "8186", "8188", "8189", "8190", "8190S", "8196", "8197", "8198",
    "8199", "8507", "8516", "8410", "8420",
})
_PAGING_ADAPTERS = frozenset({"8301", "8305", "8312", "8373", "8375"})
_SCHEDULERS = frozenset({"8301", "8305", "8312"})
_VISUAL_ALERTERS = frozenset({"8128", "8138"})
_DISPLAYS = frozenset({"8410", "8420"})
_INTERCOMS = frozenset({"8028", "8039", "8201", "8203"})
_DOOR_CONTROLLER = frozenset({"8063"})
_CONSOLE = frozenset({"8450"})
_STROBES = frozenset({"8128", "8138", "8190S", "8410", "8420"})
_DOORS = frozenset({"8028", "8039", "8201", "8063"})

_KNOWN_MODELS = (
    _SPEAKERS | _PAGING_ADAPTERS | _VISUAL_ALERTERS | _INTERCOMS
    | _DOOR_CONTROLLER | _CONSOLE
)
_TEST = _SPEAKERS | _PAGING_ADAPTERS | _INTERCOMS | _VISUAL_ALERTERS
_TONES = _SPEAKERS | _PAGING_ADAPTERS | _INTERCOMS
_CALLS = _SPEAKERS | _SCHEDULERS | _DOOR_CONTROLLER
_NOISE = _SPEAKERS | _CONSOLE
_NOISE_UPDATE = _SPEAKERS | _CONSOLE | _SCHEDULERS
_EMERGENCY = _SPEAKERS | _PAGING_ADAPTERS | _VISUAL_ALERTERS
_RELAY_INPUT_MODELS = _KNOWN_MODELS - _CONSOLE

# Commands -> the models that have them. A command not listed here is on
# every model. Where the guide's lists for a start and its stop differ, the
# pair takes the union.
_COMMAND_MODELS: dict[str, frozenset[str]] = {
    "test_start": _TEST,
    "test_loop": _TEST,
    "test_stop": _TEST,
    "play_tone": _TONES,
    "play_tone_multicast": _TONES,
    "stop_tone": _TONES,
    "start_audio_stream": _KNOWN_MODELS - _CONSOLE,
    "stop_audio_stream": _KNOWN_MODELS - _CONSOLE,
    "set_ambient_noise_level": _NOISE_UPDATE,
    "call_extension": _CALLS,
    "end_call": _CALLS,
    "page_from_extension": _SPEAKERS,
    "emergency_alert_start": _EMERGENCY,
    "emergency_alert_stop": _EMERGENCY,
    "strobe_start": _STROBES,
    "strobe_stop": _STROBES,
    "show_image": _DISPLAYS,
    "show_image_with_text": _DISPLAYS,
    "show_slide": _DISPLAYS,
    "show_slideshow": _DISPLAYS,
    "show_clock": _DISPLAYS,
    "show_flashing_images": _DISPLAYS,
    "show_template": _DISPLAYS,
    "stop_screen": _DISPLAYS,
    "show_text": _DISPLAYS,
    "stop_text": _DISPLAYS,
    "lock_door": _DOORS,
    "unlock_door": _DOORS,
    "unlock_door_momentary": _DOORS,
    "relay_on": _DOOR_CONTROLLER,
    "relay_off": _DOOR_CONTROLLER,
    "relay_pulse": _DOOR_CONTROLLER,
    "aux_24v_on": _DOOR_CONTROLLER,
    "aux_24v_off": _DOOR_CONTROLLER,
    "activate_console_button": _CONSOLE,
    "stop_console_events": _CONSOLE,
    "skip_scheduled_events": _SCHEDULERS,
    "restore_scheduled_events": _SCHEDULERS,
    "set_page_volume": _SPEAKERS | _PAGING_ADAPTERS,
    "microphone_mute_on": _SPEAKERS,
    "microphone_mute_off": _SPEAKERS,
}

# Commands -> the oldest firmware the guide says has them, for the refusal
# message when the device turns one down.
_COMMAND_FIRMWARE: dict[str, tuple[int, ...]] = {
    "test_start": (5, 4), "test_loop": (5, 4), "test_stop": (5, 4),
    "play_tone": (5, 4), "play_tone_multicast": (5, 4),
    "start_audio_stream": (5, 3, 4), "stop_audio_stream": (5, 3, 4),
    "set_ambient_noise_level": (5, 4),
    "page_from_extension": (5, 3, 4),
    "emergency_alert_start": (5, 7), "emergency_alert_stop": (5, 7),
    "show_image": (5, 3, 4), "show_image_with_text": (5, 3, 4),
    "show_slide": (5, 3, 4), "show_slideshow": (5, 3, 4),
    "show_clock": (5, 3, 4), "show_flashing_images": (5, 3, 4),
    "show_template": (5, 3, 4), "stop_screen": (5, 3, 4),
    "show_text": (5, 7), "stop_text": (5, 7),
    "unlock_door_momentary": (5, 5),
    "relay_on": (5, 0), "relay_off": (5, 0), "relay_pulse": (5, 6),
    "aux_24v_on": (5, 0), "aux_24v_off": (5, 0),
    "activate_console_button": (5, 6), "stop_console_events": (5, 6),
    "skip_scheduled_events": (5, 7), "restore_scheduled_events": (5, 7),
    "factory_reset": (5, 4),
    "check_firmware": (4, 1), "update_firmware": (4, 1),
}

# Device settings -> (provisioning parameter, kind, models).
_SETTING_PARAMS: dict[str, tuple[str, str, frozenset[str]]] = {
    "page_volume": ("audio.page.vol", "db", _SPEAKERS | _PAGING_ADAPTERS),
    "ring_volume": ("audio.ring.vol", "db", _SPEAKERS | frozenset({"8301", "8305"})),
    "speaker_volume": ("audio.vol.spk", "db", _INTERCOMS | _DOOR_CONTROLLER),
    "ambient_noise_compensation": ("audio.noise.use", "bool", _SPEAKERS | frozenset({"8301", "8305"})),
    "noise_max_volume": ("audio.noise.max", "db", _SPEAKERS | frozenset({"8301", "8305"})),
    "input_volume": ("audio.input.gain", "db", frozenset({"8301", "8305"})),
    "microphone_mute": ("audio.mic.mute", "bool", _SPEAKERS),
}

# Strobe patterns per model (Algo Strobe Light Pattern Reference Guide).
_PATTERNS_8128 = [
    (0, "Rotate Fast"), (1, "Rotate Slow"), (3, "Multi-strobe Fast"),
    (5, "Multi-strobe Slow"), (7, "Rotating Strobe"), (8, "Steady"),
    (11, "Side to Side"), (12, "Flashing"), (13, "Classic Strobe Fast"),
    (14, "Classic Strobe Medium"), (15, "Classic Strobe Slow"),
]
_PATTERNS_8138 = [
    (0, "Rotate Fast"), (1, "Rotate Slow"), (2, "Multicolor Spin"),
    (3, "Multi-strobe Fast"), (4, "Multi-strobe Fast Two-color"),
    (5, "Multi-strobe Slow"), (6, "Multi-strobe Slow Two-color"),
    (7, "Rotating Strobe"), (8, "Steady"), (9, "Steady Two-color"),
    (11, "Side to Side"), (12, "Flashing"), (13, "Classic Strobe Fast"),
    (14, "Classic Strobe Medium"), (15, "Classic Strobe Slow"),
]
_PATTERNS_8190S = [
    (0, "Steady"), (1, "Sparkle"), (2, "Multicolor"), (3, "Flash Fast"),
    (4, "Flash Slow"), (5, "Flash Fast, Alternating Sides"),
    (6, "Flash Slow, Alternating Sides"), (7, "Classic Strobe Fast"),
    (8, "Classic Strobe Medium"), (9, "Classic Strobe Slow"),
]
_PATTERNS_DISPLAY = [
    (0, "On Steady"), (2, "Strobe Fast"), (3, "Slow Sweep"), (4, "Fast Sweep"),
    (5, "Reverse Slow Sweep"), (6, "Reverse Fast Sweep"), (7, "Flash All"),
    (8, "Alternate Sides"), (9, "Scan Slow"), (10, "Scan Fast"),
    (11, "Inside Out"), (12, "Single Flash"), (13, "Double Flash"),
    (14, "Triple Flash"), (15, "Off"),
]
_STROBE_PATTERNS: dict[str, list[tuple[int, str]]] = {
    "8128": _PATTERNS_8128,
    "8138": _PATTERNS_8138,
    "8190S": _PATTERNS_8190S,
    "8410": _PATTERNS_DISPLAY,
    "8420": _PATTERNS_DISPLAY,
}

# Strobe brightness: the 8190S takes 1 / 2 / 3, the others 0 / 56 / 255.
_BRIGHTNESS = {"low": 0, "medium": 56, "high": 255}
_BRIGHTNESS_8190S = {"low": 1, "medium": 2, "high": 3}

# GET /api/info/status field -> state variable.
_STATUS_FIELDS: dict[str, str] = {
    "Device Name": "device_name",
    "SIP Registration": "sip_registration",
    "Call Status": "call_status",
    "Next Scheduled Action": "next_scheduled_action",
    "Next Scheduled Event": "next_scheduled_event",
    "Proxy Status": "sip_proxy_status",
    "Provisioning Status": "provisioning_status",
    "IPv4": "ipv4_address",
    "Switch Port ID": "switch_port_id",
    "Current Action": "current_action",
    "Multicast Mode": "multicast_mode",
    "OAuth Profile Status": "oauth_status",
    "Temperature": "temperature",
    "Stand (Docking Station)": "stand_status",
    "Panic Button Status": "panic_button_status",
    "Virtual Panic Button Status": "virtual_panic_button_status",
    "Action Button": "action_button",
    "Console": "console_status",
    "Weather Status": "weather_status",
    "Last Weather Update": "last_weather_update",
}

_DB_RE = re.compile(r"^\s*([+-]?\d+)\s*dB\s*$", re.IGNORECASE)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def model_from_product(name: str) -> str:
    """The model number in an About page's Product Name ("Algo 8301 IP Paging
    Adapter & Scheduler" -> "8301", "... 8190S ..." -> "8190S"). Empty when
    the name carries none."""
    match = re.search(r"(?<!\d)(8\d{3})(S\b)?", str(name or ""), re.IGNORECASE)
    if not match:
        return ""
    return match.group(1) + ("S" if match.group(2) else "")


def firmware_tuple(text: str) -> tuple[int, ...]:
    """"5.5_beta11" -> (5, 5); "5.3.4" -> (5, 3, 4); () when unreadable."""
    match = re.match(r"\s*(\d+)\.(\d+)(?:\.(\d+))?", str(text or ""))
    if not match:
        return ()
    return tuple(int(part) for part in match.groups() if part is not None)


def parse_db(value: Any) -> int | None:
    match = _DB_RE.match(str(value or ""))
    return int(match.group(1)) if match else None


def _flat(value: Any) -> str:
    """A Status page value as one line of text (some fields carry a list)."""
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(_flat(item) for item in value if item not in (None, ""))
    if isinstance(value, dict):
        return "; ".join(f"{k}: {_flat(v)}" for k, v in value.items())
    return str(value).strip()


def signature_input(
    method: str, uri: str, timestamp: int, nonce: str, content_md5: str | None,
) -> str:
    """The text the Standard method's HMAC is computed over."""
    if content_md5 is not None:
        return f"{method}:{uri}:{content_md5}:application/json:{timestamp}:{nonce}"
    return f"{method}:{uri}:{timestamp}:{nonce}"


def signature(password: str, text: str) -> str:
    return hmac.new(password.encode("utf-8"), text.encode("utf-8"), hashlib.sha256).hexdigest()


class AlgoIpEndpointDriver(BaseDriver):
    """Algo IP speakers, paging adapters, visual alerters, displays,
    intercoms and consoles over the RESTful API."""

    DRIVER_INFO = {
        "id": "algo_ip_endpoint",
        "name": "Algo IP Endpoint",
        "manufacturer": "Algo",
        "category": "audio",
        "version": "1.0.2",
        "author": "OpenAVC",
        "description": (
            "Controls Algo IP speakers, paging adapters, visual alerters, "
            "display speakers, intercoms, the 8063 door controller and the "
            "8450 console over their RESTful API. Plays tones and test tones, "
            "starts and stops emergency alerts, calls and pages extensions, "
            "runs strobe patterns, shows images, slides, clocks and text on "
            "the 8410 and 8420, locks and unlocks doors, skips a day's "
            "scheduled bells on the 8301, and sets page, ring and speaker "
            "volume. Reports call, multicast, SIP and relay input status, "
            "ambient noise and the console's active events. Each device is "
            "offered only the commands its model has."
        ),
        "source_url": "https://docs.algosolutions.com/docs/restful-api-guide",
        "tags": [
            "paging", "speaker", "strobe", "visual-alerter", "signage",
            "emergency-alert", "intercom", "bell-schedule",
        ],
        "verified": False,
        "simulated": True,
        "protocols": ["algo-rest-api"],
        "ports": [443],
        "min_platform_version": "0.36.0",
        "compatible_models": [
            {
                "manufacturer": "Algo",
                "models": [
                    "8180", "8186", "8188", "8189", "8190", "8190S", "8196",
                    "8197", "8198", "8199", "8507",
                ],
                "confidence": "untested",
                "notes": (
                    "IP speakers. Built from Algo's RESTful API Guide; not yet "
                    "run against a device. Firmware 5.4 or newer is needed for "
                    "the status page and model detection, 5.7 for emergency "
                    "alerts and play durations."
                ),
            },
            {
                "manufacturer": "Algo",
                "models": ["8301", "8305", "8312", "8373"],
                "confidence": "untested",
                "notes": (
                    "IP paging adapters. Skipping a day's scheduled events is "
                    "on the 8301, 8305 and 8312 with firmware 5.7 or newer."
                ),
            },
            {
                "manufacturer": "Algo",
                "models": ["8128", "8138"],
                "confidence": "untested",
                "notes": "IP visual alerters: strobe patterns, test strobe, emergency alerts.",
            },
            {
                "manufacturer": "Algo",
                "models": ["8410", "8420"],
                "confidence": "untested",
                "notes": (
                    "IP display speakers: everything a speaker does, plus "
                    "strobes and the screen (images, slides, clocks, "
                    "templates, text)."
                ),
            },
            {
                "manufacturer": "Algo",
                "models": ["8028", "8039", "8201", "8203", "8063"],
                "confidence": "untested",
                "notes": (
                    "IP intercoms and the 8063 door controller: door lock and "
                    "unlock (8028, 8039, 8201, 8063), the 8063's relays and "
                    "inputs, tones and test tones on the intercoms. Calls "
                    "placed from the intercom are the phone system's."
                ),
            },
            {
                "manufacturer": "Algo",
                "models": ["8450"],
                "confidence": "untested",
                "notes": "IP console: press a configured button, stop active events, read which are active.",
            },
        ],
        "transport": "http",
        "help": {
            "overview": (
                "Controls one Algo IP endpoint: a speaker, a paging adapter, "
                "a visual alerter, a display speaker, an intercom, the 8063 "
                "door controller or the 8450 console. Use it to play class "
                "change tones and pages, start an emergency alert or a strobe "
                "pattern, put a message on a display, or unlock a door from a "
                "panel button or a schedule. Once connected, the device page "
                "offers only the commands its model has."
            ),
            "setup": (
                "1. On the device's web interface, go to Advanced Settings > "
                "Admin and turn on RESTful API.\n"
                "2. Pick the Authentication Method there (Standard is Algo's "
                "recommendation) and set a RESTful API Password. Algo's "
                "factory password is algo.\n"
                "3. Turn on NTP on the device (Advanced Settings > Time) so "
                "its clock stays right.\n"
                "4. Enter the device's IP address, the same authentication "
                "method and the password here."
            ),
            "connection": (
                "HTTPS on port 443 with the RESTful API password. The device's "
                "certificate is self-signed, so certificate verification is "
                "off unless you turn it on."
            ),
        },
        "default_config": {
            "host": "",
            "port": 443,
            "ssl": True,
            "verify_ssl": False,
            "auth_method": "standard",
            "password": "",
            "poll_interval": 5,
            "timeout": 5.0,
        },
        "config_schema": {
            "host": {
                "type": "string", "required": True, "label": "IP Address",
                "help": "The device's IP address, shown on its Status page.",
            },
            "port": {
                "type": "integer", "default": 443, "label": "Port",
                "min": 1, "max": 65535, "advanced": True,
                "help": "443 for HTTPS, 80 for HTTP.",
            },
            "ssl": {
                "type": "boolean", "default": True, "label": "Use HTTPS",
                "advanced": True,
                "help": "HTTPS is always on at the device. Leave this on.",
            },
            "auth_method": {
                "type": "enum", "default": "standard", "label": "Authentication Method",
                "values": [
                    {"value": "standard", "label": "Standard"},
                    {"value": "basic", "label": "Basic"},
                    {"value": "none", "label": "None"},
                ],
                "help": "Must match Authentication Method under Advanced Settings > Admin on the device.",
            },
            "password": {
                "type": "string", "secret": True, "default": "",
                "label": "RESTful API Password",
                "help": "The RESTful API Password set under Advanced Settings > Admin. Algo's factory password is algo.",
            },
            "verify_ssl": {
                "type": "boolean", "default": False, "label": "Verify TLS Certificate",
                "advanced": True,
                "help": "The device's certificate is self-signed, so leave this off unless you have installed a trusted one.",
            },
            "poll_interval": {
                "type": "integer", "default": 5, "label": "Poll Interval (s)",
                "min": 0, "max": 300,
                "help": "How often call, relay input and console status are read. 0 stops reading them.",
            },
            "timeout": {
                "type": "number", "default": 5.0, "label": "Request Timeout (s)",
                "min": 1, "max": 30, "advanced": True,
                "help": "How long to wait for each answer from the device.",
            },
        },
        "state_variables": {
            # Identity (GET /api/info/about)
            "product_name": {"type": "string", "label": "Product"},
            "model": {
                "type": "string", "label": "Model",
                "help": "The model number read from the product name. Empty when the firmware is older than 5.4.",
            },
            "firmware_version": {"type": "string", "label": "Firmware Version"},
            "mac_address": {"type": "string", "label": "MAC Address"},
            "hardware_version": {"type": "string", "label": "Hardware Version"},
            "manufacturer_certificate": {"type": "string", "label": "Manufacturer Certificate"},
            # Status page (GET /api/info/status)
            "device_name": {"type": "string", "label": "Device Name"},
            "sip_registration": {"type": "string", "label": "SIP Registration"},
            "call_status": {
                "type": "string", "label": "Call Status",
                "help": "Idle, Ringing, Connected, Recording Message or Playing Delayed Page.",
            },
            "call_active": {
                "type": "boolean", "label": "Call Active", "cloud_priority": "high",
                "help": "True whenever Call Status is anything other than Idle.",
            },
            "next_scheduled_action": {"type": "string", "label": "Next Scheduled Action"},
            "next_scheduled_event": {"type": "string", "label": "Next Scheduled Event"},
            "sip_proxy_status": {"type": "string", "label": "SIP Proxy Status"},
            "provisioning_status": {"type": "string", "label": "Provisioning Status"},
            "ipv4_address": {"type": "string", "label": "IPv4 Address"},
            "switch_port_id": {"type": "string", "label": "Switch Port ID"},
            "current_action": {
                "type": "string", "label": "Current Action",
                "help": "What a scheduled event is doing now: None, or Streaming Audio.",
            },
            "multicast_mode": {
                "type": "string", "label": "Multicast Mode",
                "help": "Disabled, Receiver (Active), Receiver (Idle), Transmitter (Active) or Transmitter (Idle), with the zone when active.",
            },
            "multicast_active": {
                "type": "boolean", "label": "Multicast Active",
                "help": "True while the device is sending or receiving a multicast stream.",
            },
            "oauth_status": {"type": "string", "label": "OAuth Profile Status"},
            "temperature": {"type": "string", "label": "Temperature", "cloud_priority": "low"},
            "stand_status": {"type": "string", "label": "Handset Stand"},
            "panic_button_status": {"type": "string", "label": "Panic Buttons"},
            "virtual_panic_button_status": {"type": "string", "label": "Virtual Panic Buttons"},
            "action_button": {"type": "string", "label": "Action Button", "help": "Pressed or Idle."},
            "console_status": {
                "type": "string", "label": "Console Status",
                "help": "What the 8450 console is doing: Idle, Paging with Tone Active, Emergency Alert Active, Call Active and the like.",
            },
            "weather_status": {"type": "string", "label": "Weather Service"},
            "last_weather_update": {"type": "string", "label": "Last Weather Update"},
            # Relay inputs
            "relay_input": {
                "type": "string", "label": "Relay Input",
                "help": "Idle, Disabled, or what the active input is doing.",
            },
            "relay_input_active": {
                "type": "boolean", "label": "Relay Input Active", "cloud_priority": "high",
            },
            "relay_input_1": {"type": "string", "label": "Input 1 (8063)"},
            "relay_input_1_active": {
                "type": "boolean", "label": "Input 1 Active (8063)", "cloud_priority": "high",
            },
            "relay_input_2": {"type": "string", "label": "Input 2 (8063)"},
            "relay_input_2_active": {
                "type": "boolean", "label": "Input 2 Active (8063)", "cloud_priority": "high",
            },
            # Audio
            "ambient_noise_level": {
                "type": "integer", "label": "Ambient Noise", "unit": "dB",
                "cloud_priority": "low",
                "help": "Ambient noise the speaker's microphone measures. Measured only while Ambient Noise Compensation is on.",
            },
            "page_volume": {
                "type": "integer", "label": "Page Volume", "min": -45, "max": 0,
                "step": 3, "unit": "dB", "control": True,
                "help": "Speaker volume for SIP pages and multicast, 0 dB (level 10) down to -45 dB (level -5).",
            },
            "ring_volume": {
                "type": "integer", "label": "Ring Volume", "min": -45, "max": 0,
                "step": 3, "unit": "dB",
            },
            "speaker_volume": {
                "type": "integer", "label": "Speaker Volume", "min": -45, "max": 0,
                "step": 3, "unit": "dB", "control": True,
                "help": "The intercom's speaker volume.",
            },
            "ambient_noise_compensation": {
                "type": "boolean", "label": "Ambient Noise Compensation",
            },
            "noise_max_volume": {
                "type": "integer", "label": "Noise Compensation Max Volume",
                "min": -45, "max": 0, "step": 3, "unit": "dB",
            },
            "input_volume": {
                "type": "integer", "label": "Audio Input Volume", "min": -27, "max": 6,
                "step": 3, "unit": "dB",
            },
            "microphone_mute": {
                "type": "boolean", "label": "Microphone Mute", "control": True,
                "help": "True while the speaker's microphone is off (Global Microphone Mute).",
            },
            # Tones
            "tone_options": {
                "type": "string", "label": "Tones",
                "help": "The tone files on the device, for the Play Tone picker.",
            },
            # 8450 console
            "active_events": {
                "type": "string", "label": "Active Console Events",
                "help": "The 8450's active events (emergency, nonEmergency, call, pageMic, pageTone), comma-separated.",
            },
            "events_active": {
                "type": "boolean", "label": "Console Event Active", "cloud_priority": "high",
            },
            # Firmware
            "firmware_available": {
                "type": "string", "label": "Firmware Available",
                "help": "The result of Check for Firmware Update: Up to date, or the version the device found.",
            },
            "last_error": {
                "type": "string", "label": "Last Error",
                "help": "The last thing the device refused, and why.",
            },
        },
        "commands": {
            # Test tone / test strobe
            "test_start": {
                "label": "Test Tone or Strobe",
                "help": "Plays the default test tone once. A visual alerter lights a steady strobe for five seconds; a display speaker does both.",
            },
            "test_loop": {
                "label": "Loop Test Tone or Strobe",
                "help": "Plays the default test tone on a loop, or lights a steady strobe, until Stop Test.",
            },
            "test_stop": {
                "label": "Stop Test",
                "help": "Stops the test tone and the test strobe.",
            },
            # Tones
            "play_tone": {
                "label": "Play Tone",
                "help": "Plays a tone file from the device, once or on a loop.",
                "params": {
                    "tone": {
                        "type": "string", "required": True, "label": "Tone",
                        "options_state": "tone_options",
                        "help": "A file in the device's tones folder, such as bell-na.wav or chime.wav.",
                    },
                    "loop": {"type": "boolean", "default": False, "label": "Loop"},
                    "duration": {
                        "type": "integer", "min": 1, "max": 86400, "label": "Play For", "unit": "s",
                        "help": "Stop after this many seconds. Firmware 5.7 or newer.",
                    },
                    "interval": {
                        "type": "integer", "min": 0, "max": 3600, "label": "Pause Between Plays", "unit": "s",
                        "help": "When looping, the pause between plays.",
                    },
                },
            },
            "play_tone_multicast": {
                "label": "Play Tone to Multicast Zone",
                "help": "Sends a tone file to a multicast zone, so every speaker listening to it plays the tone.",
                "params": {
                    "tone": {
                        "type": "string", "required": True, "label": "Tone",
                        "options_state": "tone_options",
                    },
                    "address": {
                        "type": "string", "required": True, "label": "Zone Address",
                        "pattern": r"\d{1,3}(\.\d{1,3}){3}",
                        "help": "The multicast zone's IP address, as set under Basic Settings > Multicast.",
                    },
                    "port": {
                        "type": "integer", "required": True, "min": 1, "max": 65535,
                        "label": "Zone Port",
                    },
                    "type": {
                        "type": "enum", "default": "rtp", "label": "Multicast Type",
                        "values": [
                            {"value": "rtp", "label": "Regular RTP"},
                            {"value": "poly", "label": "Poly Group Page"},
                        ],
                    },
                    "group": {
                        "type": "integer", "min": 1, "max": 25, "label": "Poly Group",
                        "help": "The Poly group, for the Poly Group Page type.",
                    },
                    "loop": {"type": "boolean", "default": False, "label": "Loop"},
                    "play_locally": {
                        "type": "boolean", "default": False, "label": "Also Play Here",
                        "help": "Play the tone on this device's own speaker too.",
                    },
                    "duration": {
                        "type": "integer", "min": 1, "max": 86400, "label": "Play For", "unit": "s",
                        "help": "Stop after this many seconds. Firmware 5.7 or newer.",
                    },
                },
            },
            "stop_tone": {
                "label": "Stop Tone",
                "help": "Stops a looping tone.",
                "params": {
                    "tone": {
                        "type": "string", "label": "Tone", "options_state": "tone_options",
                        "help": "Only firmware 5.4 and older needs the tone's name. Leave it empty to stop the last tone played from here.",
                    },
                },
            },
            # Audio streams and ambient noise
            "start_audio_stream": {
                "label": "Listen to Audio Stream",
                "help": "Opens a port for a direct audio stream and plays what arrives on it.",
                "params": {
                    "port": {"type": "integer", "required": True, "min": 1, "max": 65535, "label": "Port"},
                },
            },
            "stop_audio_stream": {
                "label": "Stop Listening to Audio Stream",
                "help": "Closes the direct audio stream.",
            },
            "set_ambient_noise_level": {
                "label": "Set Ambient Noise Level",
                "help": "Gives the device an ambient noise level measured elsewhere, so its noise compensation raises the volume to suit. For a paging adapter feeding speakers in a noisy space.",
                "params": {
                    "level": {"type": "integer", "required": True, "min": 0, "max": 130, "label": "Level", "unit": "dB"},
                },
            },
            # Calls
            "call_extension": {
                "label": "Call Extension and Play Tone",
                "help": "Calls a phone extension and plays a tone file to it, repeating for a set time if asked.",
                "params": {
                    "extension": {"type": "string", "required": True, "label": "Extension"},
                    "tone": {
                        "type": "string", "required": True, "label": "Tone",
                        "options_state": "tone_options",
                    },
                    "interval": {
                        "type": "integer", "min": 0, "max": 3600, "label": "Pause Between Plays", "unit": "s",
                    },
                    "max_duration": {
                        "type": "integer", "min": 1, "max": 86400, "label": "Repeat For", "unit": "s",
                        "help": "How long to keep repeating the tone.",
                    },
                    "dtmf": {
                        "type": "string", "label": "DTMF Digits", "pattern": r"[0-9*#,]{1,15}",
                        "help": "Digits to dial once the call connects, for a multi-zone paging system. A comma pauses 500 ms. Firmware 5.7 or newer.",
                    },
                },
            },
            "end_call": {
                "label": "End Call",
                "help": "Hangs up the device's current call.",
            },
            "page_from_extension": {
                "label": "Receive Page from Extension",
                "help": "Calls an extension to receive a one-way page from it.",
                "params": {
                    "extension": {"type": "string", "required": True, "label": "Extension"},
                },
            },
            # Emergency alerts
            "emergency_alert_start": {
                "label": "Start Emergency Alert",
                "help": "Starts one of the emergency alerts set up under Additional Features > Emergency Alerts: its announcement, strobe and multicast.",
                "params": {
                    "announcement": {
                        "type": "integer", "required": True, "min": 1, "max": 10,
                        "label": "Announcement",
                        "help": "The announcement number, 1 to 10.",
                    },
                },
            },
            "emergency_alert_stop": {
                "label": "Stop Emergency Alert",
                "help": "Stops the emergency alert that is playing.",
            },
            # Strobe
            "strobe_start": {
                "label": "Start Strobe",
                "help": "Lights the strobe in a pattern and colour until Stop Strobe, or for a set time.",
                "params": {
                    "pattern": {
                        "type": "integer", "required": True, "min": 0, "max": 15, "label": "Pattern",
                        "help": "The pattern number. Once the device is connected, its model's patterns are offered by name.",
                    },
                    "color": {
                        "type": "enum", "required": True, "label": "Colour",
                        "values": ["red", "blue", "green", "amber"],
                    },
                    "color2": {
                        "type": "enum", "label": "Second Colour",
                        "values": ["red", "blue", "green", "amber"],
                        "help": "For two-colour patterns (8138, 8410 and 8420).",
                    },
                    "brightness": {
                        "type": "enum", "default": "high", "label": "Brightness",
                        "values": [
                            {"value": "low", "label": "Low"},
                            {"value": "medium", "label": "Medium"},
                            {"value": "high", "label": "High"},
                        ],
                    },
                    "duration": {
                        "type": "integer", "min": 1, "max": 86400, "label": "Light For", "unit": "s",
                        "help": "Turn off after this many seconds. Firmware 5.7 or newer.",
                    },
                },
            },
            "strobe_stop": {
                "label": "Stop Strobe",
                "help": "Turns the strobe off.",
            },
            # Screen (8410 / 8420)
            "show_image": {
                "label": "Show Image",
                "help": "Shows one image file from the device on the screen.",
                "params": {
                    "image": {"type": "string", "required": True, "label": "Image File"},
                    "duration": {
                        "type": "integer", "min": 1, "max": 86400, "label": "Show For", "unit": "s",
                        "help": "Return to the default screen after this many seconds. Firmware 5.7 or newer.",
                    },
                },
            },
            "show_image_with_text": {
                "label": "Show Image with Text",
                "help": "Shows an image with text over it, scrolling or still.",
                "params": {
                    "image": {"type": "string", "required": True, "label": "Image File"},
                    "text": {"type": "string", "required": True, "label": "Text"},
                    "text_color": {
                        "type": "string", "label": "Text Colour",
                        "help": "A colour name (forestgreen) or a hex code (#000000). Black when empty.",
                    },
                    "text_position": {
                        "type": "enum", "label": "Text Position",
                        "values": ["top", "middle", "bottom"],
                    },
                    "text_size": {
                        "type": "enum", "label": "Text Size",
                        "values": ["tiny", "small", "medium", "large"],
                    },
                    "font": {
                        "type": "enum", "label": "Font",
                        "values": ["acumin", "bookman", "din", "inter", "nixie", "overpass", "roboto"],
                    },
                    "scroll": {"type": "boolean", "default": True, "label": "Scroll"},
                    "scroll_speed": {
                        "type": "integer", "min": 1, "max": 5, "label": "Scroll Speed",
                        "help": "1 (slowest) to 5.",
                    },
                    "text_background": {"type": "boolean", "default": False, "label": "Text Background"},
                    "text_background_color": {"type": "string", "label": "Text Background Colour"},
                    "duration": {
                        "type": "integer", "min": 1, "max": 86400, "label": "Show For", "unit": "s",
                        "help": "Firmware 5.7 or newer.",
                    },
                },
            },
            "show_slide": {
                "label": "Show Slide",
                "help": "Shows a slide or slideshow made on the device (Display > Slides), by its name.",
                "params": {
                    "name": {"type": "string", "required": True, "label": "Slide or Slideshow"},
                    "duration": {
                        "type": "integer", "min": 1, "max": 86400, "label": "Show For", "unit": "s",
                        "help": "Firmware 5.7 or newer.",
                    },
                },
            },
            "show_slideshow": {
                "label": "Show Slideshow from Slides",
                "help": "Shows the named slides one after another.",
                "params": {
                    "slides": {
                        "type": "string", "required": True, "label": "Slides",
                        "help": "Slide names, comma-separated: slide1, slide2, slide3.",
                    },
                    "slide_duration": {
                        "type": "integer", "required": True, "min": 1, "max": 3600,
                        "label": "Each Slide For", "unit": "s",
                    },
                    "override_strobe": {
                        "type": "boolean", "default": False, "label": "Override Slide Strobes",
                        "help": "Use one strobe setting for the whole slideshow instead of each slide's own.",
                    },
                    "duration": {
                        "type": "integer", "min": 1, "max": 86400, "label": "Show For", "unit": "s",
                        "help": "Firmware 5.7 or newer.",
                    },
                },
            },
            "show_clock": {
                "label": "Show Clock",
                "help": "Shows a digital or analog clock on the screen.",
                "params": {
                    "style": {
                        "type": "enum", "default": "digitalClock", "label": "Style",
                        "values": [
                            {"value": "digitalClock", "label": "Digital"},
                            {"value": "analogClock", "label": "Analog"},
                        ],
                    },
                    "format": {
                        "type": "enum", "label": "Time Format", "values": ["12h", "24h"],
                        "help": "Digital clock only.",
                    },
                    "show_seconds": {"type": "boolean", "label": "Show Seconds"},
                    "size": {
                        "type": "enum", "label": "Size",
                        "values": ["x-small", "small", "medium", "large", "x-large"],
                    },
                    "position": {
                        "type": "enum", "label": "Position",
                        "values": [
                            "top-left", "top-center", "top-right", "left", "center",
                            "right", "bottom-left", "bottom-center", "bottom-right",
                        ],
                    },
                    "show_date": {"type": "boolean", "label": "Show Date"},
                    "background_image": {"type": "string", "label": "Background Image File"},
                    "duration": {
                        "type": "integer", "min": 1, "max": 86400, "label": "Show For", "unit": "s",
                        "help": "Firmware 5.7 or newer.",
                    },
                },
            },
            "show_flashing_images": {
                "label": "Show Flashing Images",
                "help": "Alternates two images on the screen.",
                "params": {
                    "image1": {"type": "string", "required": True, "label": "First Image File"},
                    "image2": {"type": "string", "required": True, "label": "Second Image File"},
                    "duration": {
                        "type": "integer", "min": 1, "max": 86400, "label": "Show For", "unit": "s",
                        "help": "Firmware 5.7 or newer.",
                    },
                },
            },
            "show_template": {
                "label": "Show Template Slide",
                "help": "Builds a slide from one of the device's templates and shows it. Which text and image fields a template uses is shown under Display > Slides > Template on the device.",
                "params": {
                    "template": {
                        "type": "enum", "required": True, "label": "Template",
                        "values": [
                            "announcement", "dont_forget", "reminder", "today_is",
                            "school_time", "school_date_time", "hospital_scrolling_text",
                            "hospital_text", "hospital_time", "time_date_bg",
                            "time_date_bg2", "dualclock1", "dualclock1_nocorner",
                            "dualclock2", "dualclock3", "calendar_time_date",
                            "calendar_time_image", "calendar_analog_clock",
                            "weather_current", "weather_hourly", "weather_weekly",
                        ],
                    },
                    "text1": {"type": "string", "label": "Text 1"},
                    "text2": {"type": "string", "label": "Text 2"},
                    "text3": {"type": "string", "label": "Text 3"},
                    "image": {"type": "string", "label": "Background Image File"},
                    "icon": {"type": "string", "label": "Icon File"},
                    "duration": {
                        "type": "integer", "min": 1, "max": 86400, "label": "Show For", "unit": "s",
                        "help": "Firmware 5.7 or newer.",
                    },
                },
            },
            "stop_screen": {
                "label": "Return Screen to Default",
                "help": "Stops what a screen command started and returns the screen to its default.",
            },
            "show_text": {
                "label": "Show Text",
                "help": "Shows text over whatever is on the screen.",
                "params": {
                    "text": {"type": "string", "required": True, "label": "Text"},
                    "color": {
                        "type": "string", "label": "Text Colour",
                        "help": "A colour name or a hex code (#FFFFFF). Black when empty.",
                    },
                    "position": {
                        "type": "enum", "label": "Position", "values": ["top", "middle", "bottom"],
                    },
                    "size": {
                        "type": "enum", "label": "Size", "values": ["tiny", "small", "medium", "large"],
                    },
                    "font": {
                        "type": "enum", "label": "Font",
                        "values": ["acumin", "bookman", "din", "inter", "nixie", "overpass", "roboto"],
                    },
                    "scroll": {"type": "boolean", "default": True, "label": "Scroll"},
                    "scroll_speed": {
                        "type": "integer", "min": 1, "max": 5, "label": "Scroll Speed",
                        "help": "1 (slowest) to 5.",
                    },
                    "background": {"type": "boolean", "default": False, "label": "Background"},
                    "background_color": {"type": "string", "label": "Background Colour"},
                    "persistent": {
                        "type": "boolean", "default": False, "label": "Keep When Screen Changes",
                        "help": "Keep the text up when a slide or image changes underneath it.",
                    },
                    "duration": {
                        "type": "integer", "min": 1, "max": 86400, "label": "Show For", "unit": "s",
                        "help": "Leave empty to show it until Stop Text.",
                    },
                },
            },
            "stop_text": {
                "label": "Stop Text",
                "help": "Takes the text off the screen.",
            },
            # Doors and relays
            "lock_door": {
                "label": "Lock Door",
                "help": "Locks the door on the device's own relay, or on an 8063 door controller paired with it.",
                "params": {
                    "door": {
                        "type": "enum", "default": "local", "label": "Door",
                        "values": [
                            {"value": "local", "label": "This device's relay"},
                            {"value": "netdc1", "label": "Paired 8063 door controller"},
                        ],
                    },
                },
            },
            "unlock_door": {
                "label": "Unlock Door",
                "help": "Unlocks the door until Lock Door.",
                "params": {
                    "door": {
                        "type": "enum", "default": "local", "label": "Door",
                        "values": [
                            {"value": "local", "label": "This device's relay"},
                            {"value": "netdc1", "label": "Paired 8063 door controller"},
                        ],
                    },
                },
            },
            "unlock_door_momentary": {
                "label": "Unlock Door Briefly",
                "help": "Unlocks the door for a set number of seconds, then locks it again.",
                "params": {
                    "door": {
                        "type": "enum", "default": "local", "label": "Door",
                        "values": [
                            {"value": "local", "label": "This device's relay"},
                            {"value": "netdc1", "label": "Paired 8063 door controller"},
                        ],
                    },
                    "seconds": {
                        "type": "integer", "required": True, "min": 1, "max": 3600,
                        "label": "Unlock For", "unit": "s",
                    },
                },
            },
            "relay_on": {
                "label": "Output Relay On",
                "help": "Energizes the 8063's output relay.",
            },
            "relay_off": {
                "label": "Output Relay Off",
                "help": "Releases the 8063's output relay.",
            },
            "relay_pulse": {
                "label": "Pulse Output Relay",
                "help": "Energizes the 8063's output relay for a set number of seconds.",
                "params": {
                    "seconds": {
                        "type": "integer", "required": True, "min": 1, "max": 3600,
                        "label": "On For", "unit": "s",
                    },
                },
            },
            "aux_24v_on": {
                "label": "24 V Aux Output On",
                "help": "Turns on the 8063's 24 V auxiliary output.",
            },
            "aux_24v_off": {
                "label": "24 V Aux Output Off",
                "help": "Turns off the 8063's 24 V auxiliary output.",
            },
            # 8450 console
            "activate_console_button": {
                "label": "Press Console Button",
                "help": "Runs the action of an 8450 button, as if it were pressed on the screen.",
                "params": {
                    "button": {
                        "type": "string", "required": True, "label": "Button Identifier",
                        "help": "The Identifier set on the button's configuration screen.",
                    },
                },
            },
            "stop_console_events": {
                "label": "Stop Console Events",
                "help": "Stops active 8450 events: pages, calls, tones or emergency paging.",
                "params": {
                    "type": {
                        "type": "enum", "default": "all", "label": "Events",
                        "values": [
                            {"value": "all", "label": "All"},
                            {"value": "emergency", "label": "Emergency paging"},
                            {"value": "nonEmergency", "label": "Other paging"},
                            {"value": "call", "label": "SIP call"},
                            {"value": "pageMic", "label": "Live page from the microphone"},
                            {"value": "pageTone", "label": "Tones and recorded announcements"},
                        ],
                    },
                },
            },
            # Scheduler (8301 / 8305 / 8312)
            "skip_scheduled_events": {
                "label": "Skip Scheduled Events for a Day",
                "help": "Skips every scheduled event on one day, a snow day or an exam day, without changing the schedule. Restore Scheduled Events brings them back.",
                "params": {
                    "date": {
                        "type": "string", "label": "Date", "pattern": r"\d{4}-\d{2}-\d{2}",
                        "help": "YYYY-MM-DD. Today when empty.",
                    },
                },
            },
            "restore_scheduled_events": {
                "label": "Restore Scheduled Events for a Day",
                "help": "Brings back every event skipped on one day.",
                "params": {
                    "date": {
                        "type": "string", "label": "Date", "pattern": r"\d{4}-\d{2}-\d{2}",
                        "help": "YYYY-MM-DD. Today when empty.",
                    },
                },
            },
            # Volume and microphone (written as device settings)
            "set_page_volume": {
                "label": "Set Page Volume",
                "help": "Sets the speaker volume for pages and multicast.",
                "params": {
                    "volume": {
                        "type": "integer", "required": True, "min": -45, "max": 0,
                        "label": "Volume", "unit": "dB",
                        "help": "0 dB down to -45 dB, in 3 dB steps.",
                    },
                },
                "sets": {"page_volume": "{volume}"},
            },
            "microphone_mute_on": {
                "label": "Microphone Off",
                "help": "Turns the speaker's microphone off entirely (Global Microphone Mute).",
                "sets": {"microphone_mute": True},
            },
            "microphone_mute_off": {
                "label": "Microphone On",
                "help": "Turns the speaker's microphone back on.",
                "sets": {"microphone_mute": False},
            },
            # Maintenance
            "check_firmware": {
                "label": "Check for Firmware Update",
                "help": "Asks Algo's server whether newer firmware exists. The answer appears as Firmware Available.",
            },
            "update_firmware": {
                "label": "Update Firmware",
                "help": "Downloads and installs the newest firmware from Algo's server. The device restarts when it installs one.",
                "confirm": "The device downloads new firmware from Algo, installs it and restarts. Nothing plays or pages until it is back.",
                "restarts_device_for": 300,
            },
            "restart_application": {
                "label": "Restart Application",
                "help": "Restarts the device's main application without rebooting it.",
                "restarts_device_for": 30,
            },
            "reboot": {
                "label": "Reboot",
                "help": "Reboots the device.",
                "restarts_device_for": 120,
            },
            "factory_reset": {
                "label": "Restore Factory Defaults",
                "help": "Returns every setting on the device to its factory value.",
                "confirm": (
                    "Erases every setting on the device, including its SIP, "
                    "multicast, schedule and network settings. The RESTful API "
                    "turns off too, so this driver loses the device until the "
                    "API is turned on again in its web interface."
                ),
            },
        },
        "actions": [
            {"id": "test_start", "kind": "command", "icon": "volume-2"},
            {"id": "test_stop", "kind": "command", "icon": "volume-x"},
            {"id": "play_tone", "kind": "command", "icon": "bell"},
            {"id": "stop_tone", "kind": "command", "icon": "bell-off"},
            {"id": "strobe_start", "kind": "command", "icon": "siren"},
            {"id": "strobe_stop", "kind": "command", "icon": "circle-off"},
            {"id": "show_text", "kind": "command", "icon": "type"},
            {"id": "stop_text", "kind": "command", "icon": "x"},
            {"id": "unlock_door_momentary", "kind": "command", "icon": "door-open"},
            {"id": "activate_console_button", "kind": "command", "icon": "pointer"},
        ],
        "device_settings": {
            "page_volume": {
                "type": "integer", "label": "Page Volume (dB)", "default": 0,
                "min": -45, "max": 0, "setup": False,
                "help": "Speaker volume for SIP pages and multicast, in 3 dB steps.",
            },
            "ring_volume": {
                "type": "integer", "label": "Ring Volume (dB)", "default": 0,
                "min": -45, "max": 0, "setup": False,
                "help": "Speaker volume when the SIP ring extension rings, in 3 dB steps.",
            },
            "speaker_volume": {
                "type": "integer", "label": "Speaker Volume (dB)", "default": 0,
                "min": -45, "max": 0, "setup": False,
                "help": "The intercom's speaker volume, in 3 dB steps.",
            },
            "ambient_noise_compensation": {
                "type": "boolean", "label": "Ambient Noise Compensation", "default": True,
                "setup": False,
                "help": "Raises the volume when the space is noisy, measured by the speaker's microphone while it is idle.",
            },
            "noise_max_volume": {
                "type": "integer", "label": "Noise Compensation Max Volume (dB)", "default": 0,
                "min": -45, "max": 0, "setup": False,
                "help": "The loudest that noise compensation may raise the volume.",
            },
            "input_volume": {
                "type": "integer", "label": "Audio Input Volume (dB)", "default": 0,
                "min": -27, "max": 6, "setup": False,
                "help": "Level of the paging adapter's audio input (music or a microphone).",
            },
            "microphone_mute": {
                "type": "boolean", "label": "Microphone Mute", "default": False, "setup": False,
                "help": "Turns the speaker's microphone off entirely.",
            },
        },
        "discovery": {
            # Algo Communication Products Ltd (IEEE MA-L), its only block. The
            # API is off by default and answers nothing without credentials,
            # and no landing page or certificate text is documented, so there
            # is no probe to send.
            "oui": ["00:22:ee"],
            # Algo Communication Products Ltd's IANA enterprise number. SNMP
            # is off by default on the device; when it is on, a sysObjectID
            # under this number identifies the maker.
            "snmp_pen": 41738,
            "manufacturer_alias": ["Algo", "Algo Communication Products"],
        },
    }

    # ── Construction ──

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._client: httpx.AsyncClient | None = None
        self._model = ""
        self._firmware: tuple[int, ...] = ()
        # Paths and setting parameters this device answered "not here" for.
        self._unsupported: set[str] = set()
        # Monotonic times of the last answer, settings read and resync.
        # Never is minus infinity: the monotonic clock starts near zero at
        # boot, so 0.0 would read as "just now" on a freshly started host.
        self._last_reply = float("-inf")
        self._last_settings = float("-inf")
        self._last_resync = float("-inf")
        # The device's clock minus this server's, in seconds, as a refused
        # request's Date header showed it. Standard signatures are timestamped
        # in the device's clock. Kept across reconnects: a stale value is
        # corrected by the first refusal it causes.
        self._clock_offset = 0.0
        # The tone played last from this driver, for Stop Tone on firmware
        # 5.4 and older (which must name it).
        self._last_tone = ""

    # ── Connection lifecycle ──

    def _scheme(self) -> str:
        return "https" if _as_bool(self.config.get("ssl", True)) else "http"

    def _base_url(self) -> str:
        host = str(self.config.get("host", "")).strip()
        default = 443 if self._scheme() == "https" else 80
        port = int(self.config.get("port", default) or default)
        return f"{self._scheme()}://{host}:{port}"

    def _auth_method(self) -> str:
        method = str(self.config.get("auth_method", "standard") or "standard").strip().lower()
        return method if method in ("standard", "basic", "none") else "standard"

    def _password(self) -> str:
        return str(self.config.get("password", "") or "")

    async def _pre_connect(self) -> None:
        if not str(self.config.get("host", "")).strip():
            raise ConnectionFaultError(
                "The device's IP address is required.", code="invalid_config",
            )
        password = self._password()
        if password:
            self.redact_in_log(password)
            self.redact_in_log(base64.b64encode(f"{_USER}:{password}".encode()).decode())

    async def _create_transport(self, transport_type: str) -> None:
        """Driver-owned session: one httpx client. ``self.transport`` stays
        None; _link_alive()/_close_session() report and retire the client."""
        timeout = float(self.config.get("timeout", 5.0) or 5.0)
        self._client = httpx.AsyncClient(
            base_url=self._base_url(),
            verify=_as_bool(self.config.get("verify_ssl", False)),
            timeout=timeout,
            follow_redirects=False,
        )
        self._unsupported = set()

    async def _post_connect(self) -> None:
        """Prove the REST API answers with these credentials and identify the
        model before ``connected`` is declared."""
        host = str(self.config.get("host", "")).strip()
        try:
            about = await self._get_json(_ABOUT)
            if about is None:
                # Firmware older than 5.4 has no About page. Any setting
                # answers on 3.3 and newer; this one exists on every unit.
                flag = await self._get_json(f"{_SETTINGS}/{_API_FLAG}")
                if flag is None:
                    raise ConnectionFaultError(
                        f"The device at {host} answered, but not its RESTful "
                        f"API. Turn on RESTful API under Advanced Settings > "
                        f"Admin on the device.",
                        code="invalid_config",
                    )
        except httpx.ConnectError as exc:
            text = str(exc)
            if "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text:
                raise ConnectionFaultError(
                    "The device's TLS certificate is not trusted. Turn off "
                    "\"Verify TLS Certificate\" for this device, or install a "
                    "trusted certificate on it.",
                    code="tls_cert_untrusted",
                ) from exc
            raise ConnectionError(f"Could not reach the device at {host}: {exc}") from exc
        except httpx.TransportError as exc:
            raise ConnectionError(f"Could not reach the device at {host}: {exc}") from exc
        self._apply_about(about or {})
        self._narrow()
        self.set_state("last_error", None)
        what = f"Algo {self._model}" if self._model else "Algo device (model not reported)"
        log.info(f"[{self.device_id}] Connected to {what} at {host}")

    async def _initial_sync(self) -> None:
        await self._read_tones()
        await self._read_status()
        await self._read_settings()
        self._last_resync = time.monotonic()

    def _link_alive(self) -> bool:
        return self._client is not None

    async def _close_session(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _liveness_probe(self) -> None:
        """Any answer in the last interval counts: polling proves the link
        most of the time. Otherwise read the REST API's own on/off setting,
        which every unit has; any HTTP answer, a refusal included, means the
        device is there."""
        if time.monotonic() - self._last_reply < float(self.HEALTH_INTERVAL_S):
            return
        client = self._client
        if client is None:
            raise ConnectionError("Not connected")
        path = f"{_SETTINGS}/{_API_FLAG}"
        headers, _, _ = self._signed("GET", path, None)
        await client.get(path, headers=headers)
        self._last_reply = time.monotonic()

    # ── Model narrowing ──

    def _narrow(self) -> None:
        """Offer only what this model has. An unidentified model keeps the
        full declaration."""
        base = type(self).DRIVER_INFO
        model = self._model
        if model not in _KNOWN_MODELS:
            self.DRIVER_INFO = base
            return
        commands: dict[str, Any] = {}
        for command_id, cdef in base["commands"].items():
            models = _COMMAND_MODELS.get(command_id)
            if models is not None and model not in models:
                continue
            if command_id == "strobe_start" and model in _STROBE_PATTERNS:
                cdef = copy.deepcopy(cdef)
                cdef["params"]["pattern"] = {
                    "type": "enum", "required": True, "label": "Pattern",
                    "values": [
                        {"value": str(number), "label": name}
                        for number, name in _STROBE_PATTERNS[model]
                    ],
                }
            commands[command_id] = cdef
        settings = {
            key: sdef for key, sdef in base["device_settings"].items()
            if model in _SETTING_PARAMS[key][2]
        }
        actions = [
            action for action in base["actions"]
            if action.get("command", action["id"]) in commands
        ]
        self.DRIVER_INFO = {
            **base,
            "commands": commands,
            "device_settings": settings,
            "actions": actions,
        }

    def _has(self, command: str) -> bool:
        models = _COMMAND_MODELS.get(command)
        return not self._model or self._model not in _KNOWN_MODELS or models is None or self._model in models

    # ── Requests ──

    def _signed(
        self, method: str, path: str, body: Any,
    ) -> tuple[dict[str, str], bytes | None, int | None]:
        """Headers, the exact body bytes, and the timestamp signed (Standard
        only, in the device's clock) for one request under the configured
        authentication method."""
        headers: dict[str, str] = {}
        content: bytes | None = None
        timestamp: int | None = None
        if body is not None:
            content = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        method_name = self._auth_method()
        password = self._password()
        if method_name == "basic":
            token = base64.b64encode(f"{_USER}:{password}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        elif method_name == "standard":
            timestamp = int(time.time() + self._clock_offset)
            nonce = str(secrets.randbelow(10**9))
            content_md5 = hashlib.md5(content).hexdigest() if content is not None else None
            if content_md5 is not None:
                headers["Content-MD5"] = content_md5
            digest = signature(
                password, signature_input(method, path, timestamp, nonce, content_md5),
            )
            headers["Authorization"] = f"hmac {_USER}:{nonce}:{digest}"
            headers["Date"] = email.utils.formatdate(timestamp, usegmt=True)
        return headers, content, timestamp

    async def _exchange(
        self, client: httpx.AsyncClient, method: str, path: str, body: Any,
    ) -> tuple[httpx.Response, int | None]:
        """Sign and send one request; the answer and the timestamp signed."""
        headers, content, signed_at = self._signed(method, path, body)
        response = await client.request(method, path, headers=headers, content=content)
        self._last_reply = time.monotonic()
        return response, signed_at

    async def _request(self, method: str, path: str, body: Any = None) -> httpx.Response:
        """One request. Transport errors propagate (the poll contract). A
        Standard signature refused because the clocks are apart is signed
        again in the device's clock and sent once more; a refusal that stands
        drops the connection as ``auth_failed``. Other statuses come back to
        the caller."""
        client = self._client
        if client is None:
            raise ConnectionError("Not connected")
        response, signed_at = await self._exchange(client, method, path, body)
        if response.status_code in (401, 403) and self._clock_caused(response, signed_at):
            response, signed_at = await self._exchange(client, method, path, body)
        if response.status_code in (401, 403):
            message = self._refusal_of_credentials(response, signed_at)
            if getattr(self, "_connected", False):
                self._force_disconnect("auth_failed", message)
            raise ConnectionFaultError(message, code="auth_failed")
        if 300 <= response.status_code < 400:
            location = response.headers.get("location", "")
            if location.lower().startswith("https:") and self._scheme() == "http":
                message = (
                    "The device accepts HTTPS only. Turn on Use HTTPS for this "
                    "device and set the port to 443."
                )
            else:
                message = (
                    f"The device redirected the request to {location or 'another page'}. "
                    f"Turn on RESTful API under Advanced Settings > Admin on the device."
                )
            raise ConnectionFaultError(message, code="invalid_config")
        return response

    def _clock_caused(self, response: httpx.Response, signed_at: int | None) -> bool:
        """Whether a refusal is the clocks rather than the password: a
        Standard signature whose timestamp is more than ``_CLOCK_REFUSAL_S``
        from the device's clock (the refusal's Date header). When it is, the
        device's clock is kept, so the request signed again, and every one
        after it, is timestamped in that clock."""
        if signed_at is None:
            return False
        device_time = self._device_time(response)
        if device_time is None or abs(device_time - signed_at) <= _CLOCK_REFUSAL_S:
            return False
        self._clock_offset = device_time - time.time()
        direction = "ahead of" if self._clock_offset > 0 else "behind"
        log.warning(
            f"[{self.device_id}] The device's clock is {abs(self._clock_offset):.0f} "
            f"seconds {direction} this server's; signing requests in the "
            f"device's clock. Turn on NTP on the device (Advanced Settings > "
            f"Time) and check this server's clock."
        )
        return True

    def _refusal_of_credentials(self, response: httpx.Response, signed_at: int | None) -> str:
        """Why the device turned the request down, as far as it can be told.
        The clocks are named only when they are still too far apart for the
        timestamp the refused request was signed with."""
        device_time = self._device_time(response) if signed_at is not None else None
        if device_time is not None and abs(device_time - signed_at) > _CLOCK_REFUSAL_S:
            skew = device_time - time.time()
            direction = "ahead of" if skew > 0 else "behind"
            return (
                f"The device refused the request. Its clock is "
                f"{abs(skew):.0f} seconds {direction} this server's, and "
                f"Standard authentication needs them within "
                f"{_TIMESTAMP_TOLERANCE_S} seconds. Turn on NTP on the "
                f"device (Advanced Settings > Time) and check this "
                f"server's clock."
            )
        return (
            f"The device refused the RESTful API credentials (HTTP "
            f"{response.status_code}). Check that RESTful API is turned on "
            f"under Advanced Settings > Admin on the device and that the "
            f"authentication method and password here match it there. "
            f"Algo's factory password is algo."
        )

    @staticmethod
    def _device_time(response: httpx.Response) -> float | None:
        """The device's clock when it answered, from the response's Date
        header (whole seconds), as a Unix time."""
        stamp = response.headers.get("date")
        if not stamp:
            return None
        try:
            moment = email.utils.parsedate_to_datetime(stamp)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if moment.tzinfo is None:
            # An HTTP date is always GMT; a "-0000" zone parses without one.
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp()

    async def _get_json(self, path: str) -> dict[str, Any] | None:
        """GET a resource. None when this device does not have it (remembered,
        so it is not asked again) or answered with something unreadable."""
        if path in self._unsupported:
            return None
        response = await self._request("GET", path)
        if response.is_success:
            try:
                payload = response.json()
            except ValueError:
                self._unsupported.add(path)
                log.info(f"[{self.device_id}] {path} answered with something other than JSON; not asking again")
                return None
            return payload if isinstance(payload, dict) else None
        if response.status_code in (400, 404, 405, 501):
            self._unsupported.add(path)
            log.info(f"[{self.device_id}] {path} is not available on this device (HTTP {response.status_code})")
            return None
        log.info(f"[{self.device_id}] {path} answered HTTP {response.status_code}; skipping this time")
        return None

    async def _send(self, command: str, method: str, path: str, body: Any = None) -> httpx.Response:
        """Send a command. A refusal names the command and the reason, goes to
        last_error, and raises."""
        response = await self._request(method, path, body)
        if response.is_success:
            return response
        label = self.DRIVER_INFO["commands"].get(command, {}).get("label", command)
        message = f"The device refused {label}: {self._refusal_reason(response)}"
        floor = _COMMAND_FIRMWARE.get(command)
        if floor and self._firmware and self._firmware < floor:
            message += (
                f". It needs firmware {'.'.join(map(str, floor))} or newer; "
                f"this device reports {'.'.join(map(str, self._firmware))}."
            )
        self.set_state("last_error", message)
        raise ValueError(message)

    @staticmethod
    def _refusal_reason(response: httpx.Response) -> str:
        text = (response.text or "").strip()
        detail = ""
        if text:
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                for key in ("error", "message", "status", "detail"):
                    if payload.get(key):
                        detail = str(payload[key])
                        break
                if not detail:
                    detail = text[:200]
            elif not text.startswith("<"):
                detail = text[:200]
        code = response.status_code
        return f"HTTP {code} ({detail})" if detail else f"HTTP {code}"

    # ── State mirroring ──

    def _apply_about(self, body: dict[str, Any]) -> None:
        product = str(body.get("Product Name", "") or "").strip()
        firmware = str(body.get("Firmware Version", "") or "").strip()
        self._model = model_from_product(product)
        self._firmware = firmware_tuple(firmware)
        updates: dict[str, Any] = {"model": self._model or None}
        if product:
            updates["product_name"] = product
        if firmware:
            updates["firmware_version"] = firmware
        for field, key in (
            ("MAC Address", "mac_address"),
            ("Hardware Info", "hardware_version"),
            ("Manufacturer Certificate", "manufacturer_certificate"),
        ):
            if body.get(field) not in (None, ""):
                updates[key] = str(body[field]).strip()
        for key, value in updates.items():
            self.set_state(key, value)

    def _apply_status(self, body: dict[str, Any]) -> None:
        for field, key in _STATUS_FIELDS.items():
            if field in body:
                self.set_state(key, _flat(body[field]))
        if "Call Status" in body:
            status = _flat(body["Call Status"])
            self.set_state("call_active", bool(status) and status.lower() != "idle")
        if "Multicast Mode" in body:
            mode = _flat(body["Multicast Mode"]).lower()
            self.set_state("multicast_active", "(active)" in mode)

    def _apply_relay(self, key: str, body: dict[str, Any] | None, field: str) -> None:
        if body is None or field not in body:
            return
        value = _flat(body[field])
        self.set_state(key, value)
        self.set_state(f"{key}_active", value.lower().startswith("active"))

    # ── Polling ──

    async def _read_tones(self) -> None:
        body = await self._get_json(_TONELIST)
        if body is None:
            return
        tones = body.get("tonelist")
        if isinstance(tones, list):
            names = [str(tone) for tone in tones if str(tone).strip()]
            self.set_state("tone_options", json.dumps(names))

    async def _read_status(self) -> None:
        model = self._model
        known = model in _KNOWN_MODELS
        status = await self._get_json(_STATUS)
        if status is not None:
            self._apply_status(status)
        if not known or model in _RELAY_INPUT_MODELS:
            self._apply_relay("relay_input", await self._get_json(_RELAY_INPUT), "input.relay.status")
        if model in _DOOR_CONTROLLER:
            self._apply_relay("relay_input_1", await self._get_json(_RELAY_INPUT_1), "input.relay1.status")
            self._apply_relay("relay_input_2", await self._get_json(_RELAY_INPUT_2), "input.relay2.status")
        if not known or model in _NOISE:
            body = await self._get_json(_NOISE_LEVEL)
            if body is not None and "audio.noise.level" in body:
                try:
                    self.set_state("ambient_noise_level", int(float(str(body["audio.noise.level"]).strip())))
                except ValueError:
                    pass
        if model in _CONSOLE:
            body = await self._get_json(_CONSOLE_EVENTS)
            if body is not None:
                events = body.get("active")
                kinds = [
                    str(event.get("type", "")) for event in events
                    if isinstance(event, dict) and event.get("type")
                ] if isinstance(events, list) else []
                self.set_state("active_events", ", ".join(kinds))
                self.set_state("events_active", bool(kinds))

    def _setting_applies(self, key: str) -> bool:
        return not self._model or self._model not in _KNOWN_MODELS or self._model in _SETTING_PARAMS[key][2]

    async def _read_settings(self) -> None:
        self._last_settings = time.monotonic()
        for key, (param, kind, _models) in _SETTING_PARAMS.items():
            if not self._setting_applies(key):
                continue
            await self._read_setting(key, param, kind)

    async def _read_setting(self, key: str, param: str, kind: str) -> None:
        body = await self._get_json(f"{_SETTINGS}/{param}")
        if body is None or param not in body:
            return
        raw = body[param]
        if kind == "db":
            value = parse_db(raw)
            if value is not None:
                self.set_state(key, value)
        else:
            text = str(raw).strip()
            if text in ("0", "1"):
                self.set_state(key, text == "1")

    async def poll(self) -> None:
        """Status every poll; settings every minute; identity and tones every
        ten minutes. Transport errors propagate so the platform's missed-poll
        watchdog sees them."""
        if self._client is None:
            return
        await self._read_status()
        now = time.monotonic()
        if now - self._last_settings >= _SETTINGS_INTERVAL_S:
            await self._read_settings()
        if now - self._last_resync >= _RESYNC_INTERVAL_S:
            self._last_resync = now
            about = await self._get_json(_ABOUT)
            if about is not None:
                previous = self._model
                self._apply_about(about)
                if self._model != previous:
                    self._narrow()
            await self._read_tones()

    # ── Commands ──

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        if not self._has(command):
            label = type(self).DRIVER_INFO["commands"].get(command, {}).get("label", command)
            raise ValueError(f"The Algo {self._model} has no {label} command.")

        simple = _SIMPLE_POSTS.get(command)
        if simple is not None:
            await self._send(command, "POST", simple)
            return True

        if command in ("play_tone", "play_tone_multicast"):
            tone = str(params.get("tone", "")).strip()
            if not tone:
                raise ValueError("Choose a tone to play.")
            body: dict[str, Any] = {"path": tone, "loop": _as_bool(params.get("loop", False))}
            self._optional_int(body, "duration", params.get("duration"))
            self._optional_int(body, "interval", params.get("interval"))
            if command == "play_tone_multicast":
                state: dict[str, Any] = {
                    "mode": "sender",
                    "address": str(params.get("address", "")).strip(),
                    "port": str(int(params.get("port"))),
                    "type": str(params.get("type") or "rtp"),
                }
                if state["type"] == "poly":
                    state["group"] = int(params.get("group") or 1)
                body["mcast"] = True
                body["playback"] = _as_bool(params.get("play_locally", False))
                body["state"] = state
            await self._send(command, "POST", "/api/controls/tone/start", body)
            self._last_tone = tone
            return True
        if command == "stop_tone":
            tone = str(params.get("tone", "") or "").strip() or self._last_tone
            # Firmware 5.4 and older must name the tone; firmware too old to
            # report its version (no About page) is older than that.
            if (not self._firmware or self._firmware < (5, 5)) and tone:
                await self._send(command, "POST", "/api/controls/tone/stop", {"path": tone})
            else:
                await self._send(command, "POST", "/api/controls/tone/stop")
            return True
        if command == "start_audio_stream":
            await self._send(command, "POST", "/api/controls/rx/start", {"port": str(int(params.get("port")))})
            return True
        if command == "set_ambient_noise_level":
            await self._send(command, "POST", "/api/controls/noise/update", {"level": str(int(params.get("level")))})
            return True
        if command == "call_extension":
            body = {
                "extension": str(params.get("extension", "")).strip(),
                "tone": str(params.get("tone", "")).strip(),
            }
            for name, field in (("interval", "interval"), ("max_duration", "maxdur")):
                if params.get(name) not in (None, ""):
                    body[field] = str(int(params[name]))
            if str(params.get("dtmf", "") or "").strip():
                body["dtmf"] = str(params["dtmf"]).strip()
            await self._send(command, "POST", "/api/controls/call/start", body)
            return True
        if command == "page_from_extension":
            await self._send(command, "POST", "/api/controls/call/page", {"extension": str(params.get("extension", "")).strip()})
            return True
        if command == "emergency_alert_start":
            await self._send(
                command, "POST", "/api/controls/emergency-alert/start",
                {"announcement": int(params.get("announcement"))},
            )
            return True
        if command == "strobe_start":
            await self._send(command, "POST", "/api/controls/strobe/start", self._strobe_body(params))
            return True
        if command in _SCREEN_BUILDERS:
            await self._send(command, "POST", "/api/controls/screen/start", _SCREEN_BUILDERS[command](params))
            return True
        if command == "show_text":
            await self._send(command, "POST", "/api/controls/screen-text/start", _text_body(params))
            return True
        if command in ("lock_door", "unlock_door"):
            path = "/api/controls/door/lock" if command == "lock_door" else "/api/controls/door/unlock"
            await self._send(command, "POST", path, {"doorid": str(params.get("door") or "local")})
            return True
        if command == "unlock_door_momentary":
            await self._send(command, "POST", "/api/controls/door/munlock", {
                "doorid": str(params.get("door") or "local"),
                "duration": str(int(params.get("seconds"))),
            })
            return True
        if command == "relay_pulse":
            await self._send(command, "POST", "/api/controls/relay/menable", {"duration": int(params.get("seconds"))})
            return True
        if command == "activate_console_button":
            button = str(params.get("button", "")).strip()
            if not button:
                raise ValueError("Enter the button's Identifier.")
            await self._send(command, "POST", "/api/controls/console/button/activate", {"id": button})
            await self._read_status()
            return True
        if command == "stop_console_events":
            await self._send(command, "POST", "/api/controls/console/event/stop", {"type": str(params.get("type") or "all")})
            await self._read_status()
            return True
        if command in ("skip_scheduled_events", "restore_scheduled_events"):
            day = str(params.get("date", "") or "").strip() or date.today().isoformat()
            operation = "skip" if command == "skip_scheduled_events" else "remove_skip"
            await self._send(command, "POST", _SCHEDULES, {operation: [{"evid": -1, "date": day}]})
            await self._read_status()
            return True
        if command == "set_page_volume":
            await self._write_setting("page_volume", params.get("volume"))
            return True
        if command in ("microphone_mute_on", "microphone_mute_off"):
            await self._write_setting("microphone_mute", command == "microphone_mute_on")
            return True
        if command == "check_firmware":
            response = await self._send(command, "POST", "/api/controls/upgrade/check")
            version = ""
            try:
                payload = response.json()
                version = str(payload.get("version", "")).strip() if isinstance(payload, dict) else ""
            except ValueError:
                pass
            if version:
                self.set_state("firmware_available", "Up to date" if version.lower() == "updated" else version)
            return True
        raise ValueError(f"Unknown command: {command}")

    @staticmethod
    def _optional_int(body: dict[str, Any], field: str, value: Any) -> None:
        if value not in (None, ""):
            body[field] = int(value)

    def _strobe_body(self, params: dict[str, Any]) -> dict[str, Any]:
        pattern = self._pattern_number(params.get("pattern"))
        brightness = str(params.get("brightness") or "high").strip().lower()
        scale = _BRIGHTNESS_8190S if self._model == "8190S" else _BRIGHTNESS
        if brightness not in scale:
            raise ValueError("Brightness must be low, medium or high.")
        body: dict[str, Any] = {
            "pattern": pattern,
            "color1": str(params.get("color") or "red").strip().lower(),
            "ledlvl": scale[brightness],
        }
        if str(params.get("color2", "") or "").strip():
            body["color2"] = str(params["color2"]).strip().lower()
        self._optional_int(body, "duration", params.get("duration"))
        return body

    def _pattern_number(self, value: Any) -> int:
        """A pattern number, or one of this model's pattern names."""
        text = str(value if value is not None else "").strip()
        if re.fullmatch(r"\d+", text):
            return int(text)
        for number, name in _STROBE_PATTERNS.get(self._model, []):
            if name.lower() == text.lower():
                return number
        raise ValueError(f"Unknown strobe pattern: {text or '(none)'}.")

    # ── Device settings ──

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if key not in self.DRIVER_INFO.get("device_settings", {}):
            raise ValueError(f"Unknown setting: {key}")
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self._write_setting(key, value)
        return True

    async def _write_setting(self, key: str, value: Any) -> None:
        param, kind, _models = _SETTING_PARAMS[key]
        sdef = type(self).DRIVER_INFO["device_settings"][key]
        label = sdef.get("label", key)
        if kind == "db":
            try:
                number = int(round(float(value)))
            except (TypeError, ValueError):
                raise ValueError(f"{label} must be a number of dB.") from None
            low, high = sdef["min"], sdef["max"]
            if not low <= number <= high:
                raise ValueError(f"{label} must be between {low} dB and {high} dB.")
            if (number - high) % 3:
                raise ValueError(f"{label} moves in 3 dB steps (0, -3, -6 and so on).")
            wire = f"{number}dB"
        else:
            wire = "1" if _as_bool(value) else "0"
        response = await self._request("PUT", _SETTINGS, {param: wire})
        if not response.is_success:
            message = f"The device refused {label}: {self._refusal_reason(response)}"
            self.set_state("last_error", message)
            raise ValueError(message)
        self._unsupported.discard(f"{_SETTINGS}/{param}")
        await self._read_setting(key, param, kind)


# Commands that are a bare POST.
_SIMPLE_POSTS: dict[str, str] = {
    "test_start": "/api/controls/test/start",
    "test_loop": "/api/controls/test/loop",
    "test_stop": "/api/controls/test/stop",
    "stop_audio_stream": "/api/controls/rx/stop",
    "end_call": "/api/controls/call/stop",
    "emergency_alert_stop": "/api/controls/emergency-alert/stop",
    "strobe_stop": "/api/controls/strobe/stop",
    "stop_screen": "/api/controls/screen/stop",
    "stop_text": "/api/controls/screen-text/stop",
    "relay_on": "/api/controls/relay/enable",
    "relay_off": "/api/controls/relay/disable",
    "aux_24v_on": "/api/controls/24v/enable",
    "aux_24v_off": "/api/controls/24v/disable",
    "update_firmware": "/api/controls/upgrade/start",
    "restart_application": "/api/controls/reload",
    "reboot": "/api/controls/reboot",
    "factory_reset": "/api/settings/action/restore",
}


def _put(body: dict[str, Any], field: str, value: Any, kind: str = "str") -> None:
    """Add an optional parameter to a screen body, left out when empty."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return
    if kind == "int":
        body[field] = int(value)
    elif kind == "bool":
        body[field] = _as_bool(value)
    else:
        body[field] = str(value).strip()


def _image_body(params: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"type": "image", "image1": str(params.get("image", "")).strip()}
    _put(body, "stopAfter", params.get("duration"), "int")
    return body


def _image_text_body(params: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": "image",
        "image1": str(params.get("image", "")).strip(),
        "text1": str(params.get("text", "")),
    }
    _put(body, "textColor", params.get("text_color"))
    _put(body, "textPosition", params.get("text_position"))
    _put(body, "textSize", params.get("text_size"))
    _put(body, "textFont", params.get("font"))
    _put(body, "textScroll", params.get("scroll"), "bool")
    _put(body, "textScrollSpeed", params.get("scroll_speed"))
    _put(body, "textBg", params.get("text_background"), "bool")
    _put(body, "textBgColor", params.get("text_background_color"))
    _put(body, "stopAfter", params.get("duration"), "int")
    return body


def _slide_body(params: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"screenName": str(params.get("name", "")).strip()}
    _put(body, "stopAfter", params.get("duration"), "int")
    return body


def _slideshow_body(params: dict[str, Any]) -> dict[str, Any]:
    names = [name.strip() for name in str(params.get("slides", "")).split(",") if name.strip()]
    body: dict[str, Any] = {
        "duration": int(params.get("slide_duration")),
        "slideNames": ", ".join(names),
        "overrideStrobe": _as_bool(params.get("override_strobe", False)),
    }
    _put(body, "stopAfter", params.get("duration"), "int")
    return body


def _clock_body(params: dict[str, Any]) -> dict[str, Any]:
    style = str(params.get("style") or "digitalClock")
    body: dict[str, Any] = {"type": style}
    _put(body, "clockFormat", params.get("format"))
    if params.get("show_seconds") not in (None, ""):
        field = "clockSeconds" if style == "digitalClock" else "clockSecondsAnalog"
        body[field] = _as_bool(params["show_seconds"])
    _put(body, "clockSize", params.get("size"))
    _put(body, "clockPosition", params.get("position"))
    _put(body, "date", params.get("show_date"), "bool")
    _put(body, "clockBgImage", params.get("background_image"))
    _put(body, "stopAfter", params.get("duration"), "int")
    return body


def _flashing_body(params: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": "blitz",
        "image1": str(params.get("image1", "")).strip(),
        "image2": str(params.get("image2", "")).strip(),
    }
    _put(body, "stopAfter", params.get("duration"), "int")
    return body


def _template_body(params: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"type": "template", "template": str(params.get("template", "")).strip()}
    _put(body, "text1", params.get("text1"))
    _put(body, "text2", params.get("text2"))
    _put(body, "text3", params.get("text3"))
    _put(body, "image1", params.get("image"))
    _put(body, "icon1", params.get("icon"))
    _put(body, "stopAfter", params.get("duration"), "int")
    return body


def _text_body(params: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"textContent": str(params.get("text", ""))}
    _put(body, "textColor", params.get("color"))
    _put(body, "textPosition", params.get("position"))
    _put(body, "textSize", params.get("size"))
    _put(body, "textFont", params.get("font"))
    _put(body, "textScroll", params.get("scroll"), "bool")
    _put(body, "textScrollSpeed", params.get("scroll_speed"))
    _put(body, "textBg", params.get("background"), "bool")
    _put(body, "textBgColor", params.get("background_color"))
    _put(body, "persistent", params.get("persistent"), "bool")
    _put(body, "duration", params.get("duration"), "int")
    return body


_SCREEN_BUILDERS = {
    "show_image": _image_body,
    "show_image_with_text": _image_text_body,
    "show_slide": _slide_body,
    "show_slideshow": _slideshow_body,
    "show_clock": _clock_body,
    "show_flashing_images": _flashing_body,
    "show_template": _template_body,
}
