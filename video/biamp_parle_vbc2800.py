"""
Biamp Parlé VBC 2800 Driver.

Controls a Biamp Parlé VBC 2800 video bar over its Network API: the camera
(autoframing, tiling, pan, tilt, zoom, three position presets, backlight
compensation, flip and mirror, people count), the bar's audio paths (USB in
from the computer, USB out to it, the analog input, and the microphone feed to
the assistive listening output), the input and ALS source selections, and a
reboot. Firmware 1.6.0 or later, with the Ethernet port and Control Enabled on
in Biamp Camera Controller.

Transport
---------
SSH on port 22 as user ``control`` (the password is the bar's serial number
until it is changed in Biamp Camera Controller). SSH is on by default; Telnet
is off by default and its login is not documented, so it is not offered.
Commands are ``<subject> <verb> <attribute> [value]`` ended by a carriage
return; each one is answered ``+OK``, ``+OK "value":<value>`` or, by the Text
Protocol family's convention, a line starting ``-``. The SSH session has a
terminal, so the bar may echo each command back: only ``+OK``, ``-`` and
``!`` lines are read, and anything else (an echo, a greeting) is skipped.

Push vs poll
------------
Push. Every attribute that can change is subscribed at connect
(``<subject> subscribe <attribute> <name> [rate]``, 30 of the 50 a session
may hold): the bar answers with the current value at once and again whenever
it changes, as ``! "publishToken":"<name>" "value":<value>``. Subscriptions
end with the session, so every connect makes them again. An attribute whose
subscription is refused is read on the poll instead. The poll itself is one
firmware read, which is also the liveness check.

Why Python (not a YAML .avcdriver)
----------------------------------
SSH is the bar's control transport and ``transport: ssh`` is Python-only.
Replies also name nothing (``+OK "value":0``), so a value read is matched to
the request that asked for it, one request in flight at a time.

Sources
-------
Biamp, "Parlé VBC 2800 Network API Control"
https://support.biamp.com/Parl%C3%A9/Miscellaneous/Parl%C3%A9_VBC_2800_Network_API_Control
(modified 2026-08-17, read 2026-10-29): every command, reply and the
subscription service. The port, SSH, the user name and the carriage return
are from Biamp's ``Biamp - VBC 2800.dvc`` control file linked from that
article; ``DEVICE get modelName`` and the analog gain's 3 dB steps from
Biamp's "Parlé VBC 2800 Command String Calculator"; the zoom ceiling (2x to
16x) from Biamp's "Parlé VBC 2800 overview". All archived in the workspace.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.utils.logger import get_logger

try:  # OpenAVC 0.37.1+; older releases refuse the same way with RuntimeError.
    from openavc.drivers.base import CommandRefusedError
except ImportError:  # pragma: no cover - depends on the platform release
    CommandRefusedError = RuntimeError  # type: ignore[misc,assignment]

log = get_logger(__name__)


LINE_END = "\r"
REQUEST_TIMEOUT_S = 5.0
DEFAULT_UPDATE_RATE_MS = 200

# Every subscription name starts with this, and holds no spaces (the article:
# "subscription names must not contain spaces").
TOKEN_PREFIX = "oavc_"

INPUT_SOURCES = {0: "usb", 1: "analog", 2: "usb_analog"}
ALS_SOURCES = {0: "disabled", 1: "far_end", 2: "near_end", 3: "far_and_near_end"}

# (subject, attribute, state key, kind, rated). A rated subscription carries
# the Update Rate; the rest report every change at once. Read-only fixed
# values (min/max pan, tilt and USB level) are not here: the article says
# they never change.
ATTRIBUTES: tuple[tuple[str, str, str, str, bool], ...] = (
    ("InputSource", "input", "input_source", "input_source", False),
    ("ALSSource", "input", "als_source", "als_source", False),
    ("AnalogInput", "gain", "analog_gain", "number", False),
    ("AnalogInput", "level", "analog_level", "number", True),
    ("AnalogInput", "minLevel", "analog_min_level", "number", False),
    ("AnalogInput", "maxLevel", "analog_max_level", "number", False),
    ("AnalogInput", "mute", "analog_mute", "bool", False),
    ("MicrophoneALSInput", "level", "mic_als_level", "number", True),
    ("MicrophoneALSInput", "minLevel", "mic_als_min_level", "number", False),
    ("MicrophoneALSInput", "maxLevel", "mic_als_max_level", "number", False),
    ("MicrophoneALSInput", "mute", "mic_als_mute", "bool", False),
    ("Camera", "streaming", "camera_streaming", "bool", False),
    ("Camera", "autoframing", "autoframing", "bool", False),
    ("Camera", "tiling", "tiling", "bool", False),
    ("Camera", "peopleCount", "people_count", "int", True),
    ("Camera", "pan", "pan", "int", True),
    ("Camera", "tilt", "tilt", "int", True),
    ("Camera", "zoom", "zoom", "number", True),
    ("Camera", "backlightCompensation", "backlight_compensation", "bool", False),
    ("Camera", "flip", "flip", "bool", False),
    ("Camera", "mirror", "mirror", "bool", False),
    ("USBIn", "connected", "usb_in_connected", "bool", False),
    ("USBIn", "streaming", "usb_in_streaming", "bool", False),
    ("USBIn", "level", "usb_in_level", "int", True),
    ("USBIn", "mute", "usb_in_mute", "bool", False),
    ("USBOut", "connected", "usb_out_connected", "bool", False),
    ("USBOut", "streaming", "usb_out_streaming", "bool", False),
    ("USBOut", "level", "usb_out_level", "int", True),
    ("USBOut", "mute", "usb_out_mute", "bool", False),
    ("USBOut", "hook", "off_hook", "bool", False),
)

_BY_KEY = {key: (subject, attr, kind) for subject, attr, key, kind, _ in ATTRIBUTES}
_KEY_OF = {(subject, attr): key for subject, attr, key, _kind, _ in ATTRIBUTES}

# What a command changes beside its own attribute (the article: tiling and
# autoframing exclude each other; a new min or max level can move the level).
_ALSO_CHANGES: dict[tuple[str, str], tuple[str, ...]] = {
    ("Camera", "autoframing"): ("tiling",),
    ("Camera", "tiling"): ("autoframing",),
    ("Camera", "presetRecall"): ("pan", "tilt", "zoom"),
    ("AnalogInput", "minLevel"): ("analog_level",),
    ("AnalogInput", "maxLevel"): ("analog_level",),
    ("MicrophoneALSInput", "minLevel"): ("mic_als_level",),
    ("MicrophoneALSInput", "maxLevel"): ("mic_als_level",),
}

# Audio path (the command and state prefix) -> Network API subject.
AUDIO_PATHS = {
    "analog": "AnalogInput",
    "mic_als": "MicrophoneALSInput",
    "usb_in": "USBIn",
    "usb_out": "USBOut",
}

# Device setting -> (subject, attribute). The state key is the setting's own.
SETTINGS = {
    "input_source": ("InputSource", "input"),
    "als_source": ("ALSSource", "input"),
    "analog_gain": ("AnalogInput", "gain"),
    "analog_min_level": ("AnalogInput", "minLevel"),
    "analog_max_level": ("AnalogInput", "maxLevel"),
    "mic_als_min_level": ("MicrophoneALSInput", "minLevel"),
    "mic_als_max_level": ("MicrophoneALSInput", "maxLevel"),
    "backlight_compensation": ("Camera", "backlightCompensation"),
    "flip": ("Camera", "flip"),
    "mirror": ("Camera", "mirror"),
}

# A push line: ! "publishToken":"<name>" "value":<value>, with the subscribe's
# +OK sometimes on the same line (the article's example).
_PUBLISH_RE = re.compile(
    r'^!\s*"publishToken"\s*:\s*"([^"]*)"\s*"value"\s*:\s*(.*?)\s*(\+OK)?\s*$'
)
# A value answer: +OK "value":<value>. The article writes some without the
# space ("+OK"value":-100").
_VALUE_RE = re.compile(r'^"value"\s*:\s*(.*?)\s*$')
_ERROR_RE = re.compile(r"^-[A-Z]")


def _parse_value(text: str) -> Any:
    """A reply's value as Python: a quoted string, true / false, or a number."""
    text = text.strip()
    if not text:
        return None
    if text.startswith('"'):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text.strip('"')
    low = text.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _to_bool(value: Any) -> bool | None:
    """The article answers some booleans true / false and others 1 / 0."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "on"):
            return True
        if low in ("false", "0", "off"):
            return False
    return None


def _to_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _wire_float(value: Any) -> str:
    """A level or zoom as the article writes one: -3.0, 2.5."""
    number = float(value)
    text = f"{number:.2f}".rstrip("0")
    if text.endswith("."):
        text += "0"
    return text


def _wire_int(value: Any) -> str:
    return str(int(round(float(value))))


def _wire_bool(value: Any) -> str:
    flag = _to_bool(value)
    if flag is None:
        raise ValueError(f"Not true or false: {value!r}")
    return "true" if flag else "false"


def _source_digit(table: dict[int, str], value: Any) -> str:
    """A source's word (or its digit) as the digit the bar takes."""
    text = str(value).strip().lower()
    for digit, word in table.items():
        if text in (word, str(digit)):
            return str(digit)
    raise ValueError(f"Unknown source: {value!r}")


class BiampError(Exception):
    """A request the bar answered with an error line."""


class BiampParleVBC2800Driver(BaseDriver):
    """Biamp Parlé VBC 2800 video bar over the Network API (SSH)."""

    DRIVER_INFO = {
        "id": "biamp_parle_vbc2800",
        "name": "Biamp Parlé VBC 2800",
        "manufacturer": "Biamp",
        "category": "video",
        "version": "1.0.0",
        # The contract check computes 0.24.0 (the ssh transport), but 0.24.0
        # still shipped the platform as `server` and the code's hooks were
        # checked on 0.36.0, so this is the floor the driver was run on.
        # CommandRefusedError (0.37.1) falls back to RuntimeError below it.
        "min_platform_version": "0.36.0",
        "author": "OpenAVC",
        "description": (
            "Controls the Biamp Parlé VBC 2800 video bar over its Network API "
            "(SSH): camera autoframing, tiling, pan, tilt, zoom and presets, "
            "the USB, analog and assistive listening audio levels and mutes, "
            "source selection, and people count, updated as they change."
        ),
        "source_url": (
            "https://support.biamp.com/Parl%C3%A9/Miscellaneous/"
            "Parl%C3%A9_VBC_2800_Network_API_Control"
        ),
        "tags": ["video-bar", "conferencing", "camera", "ptz", "biamp", "parle", "ssh"],
        "verified": False,
        "simulated": True,
        "protocols": ["biamp_parle_network_api"],
        "ports": [22],
        "transport": "ssh",
        "discovery": {
            # Hints only. The API is SSH on 22, whose banner names no maker and
            # whose port says nothing on its own, so there is no probe to send.
            # The bar's factory hostname is "ParleVBC2800-<serial>".
            "hostname": [r"^ParleVBC2800-"],
            # Biamp's IEEE blocks (MA-L 78-45-01, MA-M 34-E1-D1-9). Not read off
            # a VBC 2800: the bar may carry another holder's prefix.
            "oui": ["78:45:01", "34:e1:d1:9"],
            "manufacturer_alias": ["biamp"],
        },
        "compatible_models": [
            {
                "manufacturer": "Biamp",
                "models": ["Parlé VBC 2800"],
                "confidence": "untested",
                "notes": (
                    "Firmware 1.6.0 or later (the Network API arrived in 1.6.0). "
                    "Built from Biamp's Network API Control article; not yet run "
                    "against a bar. The VBC 2500, VBC 2500a and ABC 2500 have no "
                    "network control."
                ),
            },
        ],
        "help": {
            "overview": (
                "Controls a Parlé VBC 2800 video bar over the network. Camera: "
                "autoframing, tiling, pan, tilt and zoom, three position presets, "
                "people count. Audio: the level and mute of the computer's audio "
                "on the bar's speakers (USB In), the bar's audio to the computer "
                "(USB Out), the analog input, and the microphones on the "
                "assistive listening output. Values update as they change on the "
                "bar, from any source."
            ),
            "setup": (
                "1. Update the bar to firmware 1.6.0 or later with Biamp Camera "
                "Controller.\n"
                "2. In Biamp Camera Controller, under Network, turn the Ethernet "
                "port on and give the bar a fixed address (static, or a DHCP "
                "reservation).\n"
                "3. Under Network Control, leave SSH Enabled and Control Enabled "
                "on. The user name is control. The password is the bar's serial "
                "number unless it was changed there.\n"
                "4. In OpenAVC, enter the bar's IP address and the password."
            ),
        },
        "quick_actions": [
            "toggle_usb_out_mute",
            "usb_in_level_up",
            "usb_in_level_down",
            "recall_camera_preset",
            "toggle_autoframing",
        ],
        "default_config": {
            "host": "",
            "port": 22,
            "username": "control",
            "password": "",
            "ssh_auth_method": "password",
            "update_rate_ms": 200,
            "poll_interval": 30,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {
                "type": "integer", "default": 22, "min": 1, "max": 65535,
                "label": "SSH Port",
            },
            "username": {
                "type": "string", "default": "control", "label": "Username",
                "description": "control, the bar's Network API user.",
            },
            "password": {
                "type": "string", "secret": True, "label": "Password",
                "description": (
                    "The bar's serial number, unless it was changed in Biamp "
                    "Camera Controller."
                ),
            },
            "ssh_auth_method": {
                "type": "enum", "values": ["password", "key"],
                "default": "password", "label": "SSH Auth Method",
                "description": "The bar's control user signs in with a password.",
            },
            "update_rate_ms": {
                "type": "integer", "default": 200, "min": 100, "max": 10000,
                "label": "Update Rate (ms)",
                "description": (
                    "The fastest the bar reports a changing level, camera "
                    "position or people count. Mutes, sources and switches are "
                    "reported at once."
                ),
            },
            "poll_interval": {
                "type": "integer", "default": 30, "min": 0,
                "label": "Check Interval (sec)",
                "description": "How often to check that the bar still answers.",
            },
        },
        "state_variables": {
            "serial_number": {"type": "string", "label": "Serial Number"},
            "firmware_version": {"type": "string", "label": "Firmware"},
            "model_name": {"type": "string", "label": "Model"},
            "input_source": {
                "type": "enum", "label": "Input Source",
                "values": ["usb", "analog", "usb_analog"],
                "help": (
                    "What the bar plays on its speakers: USB, the analog input, "
                    "or both mixed."
                ),
            },
            "als_source": {
                "type": "enum", "label": "ALS Source",
                "values": ["disabled", "far_end", "near_end", "far_and_near_end"],
                "help": (
                    "What the assistive listening output carries: nothing, the "
                    "far end (USB and analog input), the near end (the "
                    "microphones), or both."
                ),
            },
            "analog_gain": {
                "type": "number", "label": "Analog Input Gain",
                "min": 0, "max": 24, "step": 3, "unit": "dB",
            },
            "analog_level": {
                "type": "number", "label": "Analog Input Level",
                "min": -100, "max": 12, "unit": "dB", "control": True,
                "cloud_priority": "low",
            },
            "analog_min_level": {
                "type": "number", "label": "Analog Input Min Level",
                "min": -100, "max": 12, "unit": "dB",
            },
            "analog_max_level": {
                "type": "number", "label": "Analog Input Max Level",
                "min": -100, "max": 12, "unit": "dB",
            },
            "analog_mute": {
                "type": "boolean", "label": "Analog Input Mute", "control": True,
            },
            "mic_als_level": {
                "type": "number", "label": "Microphone ALS Level",
                "min": -100, "max": 12, "unit": "dB", "control": True,
                "cloud_priority": "low",
                "help": "The microphones' level on the assistive listening output.",
            },
            "mic_als_min_level": {
                "type": "number", "label": "Microphone ALS Min Level",
                "min": -100, "max": 12, "unit": "dB",
            },
            "mic_als_max_level": {
                "type": "number", "label": "Microphone ALS Max Level",
                "min": -100, "max": 12, "unit": "dB",
            },
            "mic_als_mute": {
                "type": "boolean", "label": "Microphone ALS Mute", "control": True,
            },
            "camera_streaming": {
                "type": "boolean", "label": "Camera Streaming",
                "help": "True while a computer is taking the camera's video.",
            },
            "autoframing": {"type": "boolean", "label": "Autoframing", "control": True},
            "tiling": {"type": "boolean", "label": "Tiling", "control": True},
            "people_count": {
                "type": "integer", "label": "People Count", "min": 0,
                "cloud_priority": "low",
                "help": "People the camera sees within 6 meters.",
            },
            "pan": {
                "type": "integer", "label": "Pan", "min": -100, "max": 100,
                "control": True, "cloud_priority": "low",
                "help": "-100 is full left, 100 full right.",
            },
            "tilt": {
                "type": "integer", "label": "Tilt", "min": -100, "max": 100,
                "control": True, "cloud_priority": "low",
                "help": "-100 is full down, 100 full up.",
            },
            "zoom": {
                "type": "number", "label": "Zoom", "min": 1, "max": 16, "step": 0.1,
                "control": True, "cloud_priority": "low",
                "help": "1.0 is the widest view.",
            },
            "max_zoom": {
                "type": "number", "label": "Max Zoom",
                "help": "The highest zoom the bar's lens and Max Zoom setting allow.",
            },
            "backlight_compensation": {"type": "boolean", "label": "Backlight Compensation"},
            "flip": {"type": "boolean", "label": "Flip"},
            "mirror": {"type": "boolean", "label": "Mirror"},
            "usb_in_connected": {"type": "boolean", "label": "USB In Connected"},
            "usb_in_streaming": {"type": "boolean", "label": "USB In Streaming"},
            "usb_in_level": {
                "type": "integer", "label": "USB In Level", "min": 0, "max": 100,
                "control": True, "cloud_priority": "low",
                "help": (
                    "The computer's audio on the bar's speakers, 0 to 100, the "
                    "same scale as the computer's volume slider."
                ),
            },
            "usb_in_mute": {"type": "boolean", "label": "USB In Mute", "control": True},
            "usb_out_connected": {"type": "boolean", "label": "USB Out Connected"},
            "usb_out_streaming": {"type": "boolean", "label": "USB Out Streaming"},
            "usb_out_level": {
                "type": "integer", "label": "USB Out Level", "min": 0, "max": 100,
                "control": True, "cloud_priority": "low",
                "help": "The bar's audio to the computer, 0 to 100.",
            },
            "usb_out_mute": {
                "type": "boolean", "label": "USB Out Mute", "control": True,
                "help": "The bar's audio to the computer: the call's microphone mute.",
            },
            "off_hook": {
                "type": "boolean", "label": "Off Hook",
                "help": "True while the computer has a call up on the bar.",
            },
            "last_error": {
                "type": "string", "label": "Last Error",
                "help": "The bar's most recent refusal, in words.",
            },
        },
        "device_settings": {
            "input_source": {
                "type": "enum", "label": "Input Source", "state_key": "input_source",
                "default": "usb_analog", "setup": False,
                "values": [
                    {"value": "usb", "label": "USB"},
                    {"value": "analog", "label": "Analog"},
                    {"value": "usb_analog", "label": "USB and Analog Mixed"},
                ],
                "help": "What the bar plays on its speakers. Kept through a restart.",
            },
            "als_source": {
                "type": "enum", "label": "ALS Source", "state_key": "als_source",
                "default": "disabled", "setup": False,
                "values": [
                    {"value": "disabled", "label": "Disabled"},
                    {"value": "far_end", "label": "Far End Only"},
                    {"value": "near_end", "label": "Near End Only"},
                    {"value": "far_and_near_end", "label": "Far and Near End Mixed"},
                ],
                "help": (
                    "What the assistive listening output carries. Kept through a "
                    "restart."
                ),
            },
            "analog_gain": {
                "type": "number", "label": "Analog Input Gain (dB)",
                "state_key": "analog_gain", "default": 0, "setup": False,
                "min": 0, "max": 24,
                "help": (
                    "The analog input's pre-gain, 0 to 24 dB in 3 dB steps. More "
                    "gain leaves less headroom."
                ),
            },
            "analog_min_level": {
                "type": "number", "label": "Analog Input Min Level (dB)",
                "state_key": "analog_min_level", "default": -100, "setup": False,
                "min": -100, "max": 12,
                "help": "The lowest the analog input's level can be set or stepped to.",
            },
            "analog_max_level": {
                "type": "number", "label": "Analog Input Max Level (dB)",
                "state_key": "analog_max_level", "default": 0, "setup": False,
                "min": -100, "max": 12,
                "help": "The highest the analog input's level can be set or stepped to.",
            },
            "mic_als_min_level": {
                "type": "number", "label": "Microphone ALS Min Level (dB)",
                "state_key": "mic_als_min_level", "default": -100, "setup": False,
                "min": -100, "max": 12,
                "help": "The lowest the microphones' ALS level can be set or stepped to.",
            },
            "mic_als_max_level": {
                "type": "number", "label": "Microphone ALS Max Level (dB)",
                "state_key": "mic_als_max_level", "default": 0, "setup": False,
                "min": -100, "max": 12,
                "help": "The highest the microphones' ALS level can be set or stepped to.",
            },
            "backlight_compensation": {
                "type": "boolean", "label": "Backlight Compensation",
                "state_key": "backlight_compensation", "default": False,
                "setup": False,
                "help": "Brightens subjects in front of a bright window.",
            },
            "flip": {
                "type": "boolean", "label": "Flip", "state_key": "flip",
                "default": False, "setup": False,
                "help": "Turns the picture upside down, for a bar mounted inverted.",
            },
            "mirror": {
                "type": "boolean", "label": "Mirror", "state_key": "mirror",
                "default": False, "setup": False,
                "help": "Reverses the picture left to right.",
            },
        },
        "commands": {
            "set_usb_in_level": {
                "label": "Set USB In Level",
                "params": {
                    "level": {
                        "type": "integer", "required": True, "label": "Level",
                        "min": 0, "max": 100,
                    },
                },
                "sets": {"usb_in_level": "{level}"},
                "help": "Sets the computer's audio on the bar's speakers, 0 to 100.",
            },
            "usb_in_level_up": {
                "label": "USB In Level Up",
                "params": {
                    "step": {
                        "type": "integer", "label": "Step", "default": 5,
                        "min": 1, "max": 100,
                    },
                },
                "help": "Raises the computer's audio on the bar's speakers by the step.",
            },
            "usb_in_level_down": {
                "label": "USB In Level Down",
                "params": {
                    "step": {
                        "type": "integer", "label": "Step", "default": 5,
                        "min": 1, "max": 100,
                    },
                },
                "help": "Lowers the computer's audio on the bar's speakers by the step.",
            },
            "usb_in_mute": {
                "label": "USB In Mute",
                "params": {},
                "sets": {"usb_in_mute": True},
                "help": "Mutes the computer's audio on the bar's speakers.",
            },
            "usb_in_unmute": {
                "label": "USB In Unmute",
                "params": {},
                "sets": {"usb_in_mute": False},
                "help": "Unmutes the computer's audio on the bar's speakers.",
            },
            "toggle_usb_in_mute": {
                "label": "Toggle USB In Mute",
                "params": {},
                "help": "Mutes or unmutes the computer's audio on the bar's speakers.",
            },
            "set_usb_out_level": {
                "label": "Set USB Out Level",
                "params": {
                    "level": {
                        "type": "integer", "required": True, "label": "Level",
                        "min": 0, "max": 100,
                    },
                },
                "sets": {"usb_out_level": "{level}"},
                "help": "Sets the bar's audio to the computer, 0 to 100.",
            },
            "usb_out_level_up": {
                "label": "USB Out Level Up",
                "params": {
                    "step": {
                        "type": "integer", "label": "Step", "default": 5,
                        "min": 1, "max": 100,
                    },
                },
                "help": "Raises the bar's audio to the computer by the step.",
            },
            "usb_out_level_down": {
                "label": "USB Out Level Down",
                "params": {
                    "step": {
                        "type": "integer", "label": "Step", "default": 5,
                        "min": 1, "max": 100,
                    },
                },
                "help": "Lowers the bar's audio to the computer by the step.",
            },
            "usb_out_mute": {
                "label": "USB Out Mute",
                "params": {},
                "sets": {"usb_out_mute": True},
                "help": (
                    "Mutes the bar's audio to the computer. In a call this is the "
                    "call's microphone mute too."
                ),
            },
            "usb_out_unmute": {
                "label": "USB Out Unmute",
                "params": {},
                "sets": {"usb_out_mute": False},
                "help": (
                    "Unmutes the bar's audio to the computer, and the call's "
                    "microphone mute with it."
                ),
            },
            "toggle_usb_out_mute": {
                "label": "Toggle USB Out Mute",
                "params": {},
                "help": (
                    "Mutes or unmutes the bar's audio to the computer, and the "
                    "call's microphone mute with it."
                ),
            },
            "set_analog_level": {
                "label": "Set Analog Input Level",
                "params": {
                    "level": {
                        "type": "number", "required": True, "label": "Level (dB)",
                        "min": -100, "max": 12,
                    },
                },
                "sets": {"analog_level": "{level}"},
                "help": (
                    "Sets the analog input's level in dB, within its min and max "
                    "levels. Heard only while the input source includes analog."
                ),
            },
            "analog_level_up": {
                "label": "Analog Input Level Up",
                "params": {
                    "step": {
                        "type": "number", "label": "Step (dB)", "default": 5,
                        "min": 0.1, "max": 112,
                    },
                },
                "help": "Raises the analog input's level by the step, stopping at its max level.",
            },
            "analog_level_down": {
                "label": "Analog Input Level Down",
                "params": {
                    "step": {
                        "type": "number", "label": "Step (dB)", "default": 5,
                        "min": 0.1, "max": 112,
                    },
                },
                "help": "Lowers the analog input's level by the step, stopping at its min level.",
            },
            "analog_mute": {
                "label": "Analog Input Mute",
                "params": {},
                "sets": {"analog_mute": True},
                "help": "Mutes the analog input.",
            },
            "analog_unmute": {
                "label": "Analog Input Unmute",
                "params": {},
                "sets": {"analog_mute": False},
                "help": "Unmutes the analog input.",
            },
            "toggle_analog_mute": {
                "label": "Toggle Analog Input Mute",
                "params": {},
                "help": "Mutes or unmutes the analog input.",
            },
            "set_mic_als_level": {
                "label": "Set Microphone ALS Level",
                "params": {
                    "level": {
                        "type": "number", "required": True, "label": "Level (dB)",
                        "min": -100, "max": 12,
                    },
                },
                "sets": {"mic_als_level": "{level}"},
                "help": (
                    "Sets the microphones' level on the assistive listening "
                    "output in dB, within its min and max levels. Heard only "
                    "while the ALS source includes the near end."
                ),
            },
            "mic_als_level_up": {
                "label": "Microphone ALS Level Up",
                "params": {
                    "step": {
                        "type": "number", "label": "Step (dB)", "default": 5,
                        "min": 0.1, "max": 112,
                    },
                },
                "help": "Raises the microphones' ALS level by the step, stopping at its max level.",
            },
            "mic_als_level_down": {
                "label": "Microphone ALS Level Down",
                "params": {
                    "step": {
                        "type": "number", "label": "Step (dB)", "default": 5,
                        "min": 0.1, "max": 112,
                    },
                },
                "help": "Lowers the microphones' ALS level by the step, stopping at its min level.",
            },
            "mic_als_mute": {
                "label": "Microphone ALS Mute",
                "params": {},
                "sets": {"mic_als_mute": True},
                "help": "Mutes the microphones on the assistive listening output.",
            },
            "mic_als_unmute": {
                "label": "Microphone ALS Unmute",
                "params": {},
                "sets": {"mic_als_mute": False},
                "help": "Unmutes the microphones on the assistive listening output.",
            },
            "toggle_mic_als_mute": {
                "label": "Toggle Microphone ALS Mute",
                "params": {},
                "help": "Mutes or unmutes the microphones on the assistive listening output.",
            },
            "set_input_source": {
                "label": "Set Input Source",
                "params": {
                    "source": {
                        "type": "enum", "required": True, "label": "Source",
                        "values": [
                            {"value": "usb", "label": "USB"},
                            {"value": "analog", "label": "Analog"},
                            {"value": "usb_analog", "label": "USB and Analog Mixed"},
                        ],
                    },
                },
                "sets": {"input_source": "{source}"},
                "help": "Chooses what the bar plays on its speakers.",
            },
            "set_als_source": {
                "label": "Set ALS Source",
                "params": {
                    "source": {
                        "type": "enum", "required": True, "label": "Source",
                        "values": [
                            {"value": "disabled", "label": "Disabled"},
                            {"value": "far_end", "label": "Far End Only"},
                            {"value": "near_end", "label": "Near End Only"},
                            {"value": "far_and_near_end", "label": "Far and Near End Mixed"},
                        ],
                    },
                },
                "sets": {"als_source": "{source}"},
                "help": "Chooses what the assistive listening output carries.",
            },
            "autoframing_on": {
                "label": "Autoframing On",
                "params": {},
                "sets": {"autoframing": True},
                "help": (
                    "The camera frames the people it sees by itself. Pan, tilt "
                    "and zoom do nothing while it is on."
                ),
            },
            "autoframing_off": {
                "label": "Autoframing Off",
                "params": {},
                "sets": {"autoframing": False},
                "help": "The camera holds its position.",
            },
            "toggle_autoframing": {
                "label": "Toggle Autoframing",
                "params": {},
                "help": "Turns autoframing on or off.",
            },
            "tiling_on": {
                "label": "Tiling On",
                "params": {},
                "sets": {"tiling": True},
                "help": (
                    "The camera shows the people it sees in separate tiles. "
                    "Autoframing goes off; pan, tilt and zoom do nothing while "
                    "it is on."
                ),
            },
            "tiling_off": {
                "label": "Tiling Off",
                "params": {},
                "sets": {"tiling": False},
                "help": "The camera shows one picture.",
            },
            "toggle_tiling": {
                "label": "Toggle Tiling",
                "params": {},
                "help": "Turns tiling on or off.",
            },
            "set_pan": {
                "label": "Set Pan",
                "params": {
                    "pan": {
                        "type": "integer", "required": True, "label": "Pan",
                        "min": -100, "max": 100,
                    },
                },
                "sets": {"pan": "{pan}"},
                "help": (
                    "Pans the camera, -100 full left to 100 full right. No effect "
                    "at zoom 1.0 or with autoframing or tiling on."
                ),
            },
            "pan_left": {
                "label": "Pan Left",
                "params": {
                    "step": {
                        "type": "integer", "label": "Step", "default": 5,
                        "min": 1, "max": 200,
                    },
                },
                "help": "Pans the camera left by the step.",
            },
            "pan_right": {
                "label": "Pan Right",
                "params": {
                    "step": {
                        "type": "integer", "label": "Step", "default": 5,
                        "min": 1, "max": 200,
                    },
                },
                "help": "Pans the camera right by the step.",
            },
            "set_tilt": {
                "label": "Set Tilt",
                "params": {
                    "tilt": {
                        "type": "integer", "required": True, "label": "Tilt",
                        "min": -100, "max": 100,
                    },
                },
                "sets": {"tilt": "{tilt}"},
                "help": (
                    "Tilts the camera, -100 full down to 100 full up. No effect "
                    "at zoom 1.0 or with autoframing or tiling on."
                ),
            },
            "tilt_up": {
                "label": "Tilt Up",
                "params": {
                    "step": {
                        "type": "integer", "label": "Step", "default": 5,
                        "min": 1, "max": 200,
                    },
                },
                "help": "Tilts the camera up by the step.",
            },
            "tilt_down": {
                "label": "Tilt Down",
                "params": {
                    "step": {
                        "type": "integer", "label": "Step", "default": 5,
                        "min": 1, "max": 200,
                    },
                },
                "help": "Tilts the camera down by the step.",
            },
            "set_zoom": {
                "label": "Set Zoom",
                "params": {
                    "zoom": {
                        "type": "number", "required": True, "label": "Zoom",
                        "min": 1, "max": 16,
                        "help": "1.0 is the widest view; the top is the bar's Max Zoom.",
                    },
                },
                "sets": {"zoom": "{zoom}"},
                "help": "Zooms the camera. No effect with autoframing or tiling on.",
            },
            "zoom_in": {
                "label": "Zoom In",
                "params": {
                    "step": {
                        "type": "number", "label": "Step", "default": 0.5,
                        "min": 0.1, "max": 15,
                    },
                },
                "help": "Zooms the camera in by the step.",
            },
            "zoom_out": {
                "label": "Zoom Out",
                "params": {
                    "step": {
                        "type": "number", "label": "Step", "default": 0.5,
                        "min": 0.1, "max": 15,
                    },
                },
                "help": "Zooms the camera out by the step.",
            },
            "recall_camera_preset": {
                "label": "Recall Camera Preset",
                "params": {
                    "preset": {
                        "type": "integer", "required": True, "label": "Preset",
                        "min": 0, "max": 3,
                        "help": (
                            "The bar's three positions are 0 to 2 in Biamp's "
                            "API document. If the bar refuses a number, try 1 "
                            "to 3."
                        ),
                    },
                },
                "help": "Moves the camera to a saved position.",
            },
            "save_camera_preset": {
                "label": "Save Camera Preset",
                "params": {
                    "preset": {
                        "type": "integer", "required": True, "label": "Preset",
                        "min": 0, "max": 3,
                        "help": (
                            "The bar's three positions are 0 to 2 in Biamp's "
                            "API document. If the bar refuses a number, try 1 "
                            "to 3."
                        ),
                    },
                },
                "help": "Saves the camera's current position as the preset, replacing the one stored.",
            },
            "reboot": {
                "label": "Reboot",
                "params": {},
                "help": (
                    "Restarts the bar. Its camera, microphones and speakers drop "
                    "out until it is back."
                ),
            },
        },
    }

    HEALTH_INTERVAL_S = 30.0
    HEALTH_FAULT_MESSAGE = "The bar stopped answering on its Network API session."

    def __init__(
        self, device_id: str, config: dict[str, Any], state: Any, events: Any,
    ) -> None:
        super().__init__(device_id, config, state, events)
        self._rx = ""
        # One request in flight at a time: (command line, future).
        self._request_lock = asyncio.Lock()
        self._inflight: tuple[str, asyncio.Future[Any]] | None = None
        # Subscription name -> state key, for this session.
        self._tokens: dict[str, str] = {}
        # State keys whose subscription the bar refused: read on the poll.
        self._unsubscribed: list[str] = []
        self._bare_reply_logged = False

    # ── Lifecycle ──

    async def _pre_connect(self) -> None:
        if not str(self.config.get("host", "")).strip():
            raise ConnectionError(f"[{self.device_id}] No host configured")
        # The device's own transport, unless simulation or a test put it on
        # tcp (BaseDriver._effective_transport, which 0.36.0 does not have).
        transport = self.config.get("transport") or self.DRIVER_INFO["transport"]
        if transport == "ssh" and not str(
            self.config.get("password") or ""
        ):
            # A login that cannot succeed is never sent.
            raise ConnectionFaultError(
                "Enter the bar's password: its serial number, unless it was "
                "changed in Biamp Camera Controller.",
                code="auth_failed",
            )

    # A raw byte pipe over SSH and over the simulator's TCP: the driver splits
    # lines itself, on whichever line ending the bar uses.
    def _resolve_delimiter(self) -> bytes | None:
        return None

    async def _post_connect(self) -> None:
        # A login proves the SSH server took the password, not that the
        # Network API is serving. Ask it something before reporting connected.
        try:
            serial = await self._request("DEVICE get serialNumber")
        except BiampError:
            serial = None  # an error answer is still the API answering
        except TimeoutError as exc:
            raise ConnectionFaultError(
                "The bar accepted the connection but did not answer a Network "
                "API request. Check that Control Enabled is on under Network "
                "Control in Biamp Camera Controller, and that the firmware is "
                "1.6.0 or later.",
                code="no_response",
            ) from exc
        if serial is not None:
            self.set_state("serial_number", str(serial))

    async def _initial_sync(self) -> None:
        await self._read_identity()
        await self._subscribe_all()

    async def _close_session(self) -> None:
        if self._inflight is not None:
            _line, fut = self._inflight
            if not fut.done():
                fut.cancel()
            self._inflight = None
        self._rx = ""
        self._tokens.clear()
        self._unsubscribed = []

    # ── Line plumbing ──

    async def _request(self, line: str, *, timeout: float = REQUEST_TIMEOUT_S) -> Any:
        """Send one command and await its answer: the value it carries (None
        for a plain +OK). Raises BiampError on an error line, TimeoutError on
        no answer, ConnectionError when the session is down."""
        if self.transport is None or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        async with self._request_lock:
            fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            self._inflight = (line, fut)
            try:
                await self.transport.send((line + LINE_END).encode("utf-8"))
                return await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    f"[{self.device_id}] '{line}' got no answer in {timeout:g}s"
                ) from exc
            finally:
                self._inflight = None

    async def on_data_received(self, data: bytes) -> None:
        self._rx += data.decode("utf-8", errors="replace")
        while True:
            match = re.search(r"\r\n|\r|\n", self._rx)
            if not match:
                break
            line = self._rx[: match.start()].strip()
            self._rx = self._rx[match.end():]
            if line:
                self._route_line(line)

    def _route_line(self, line: str) -> None:
        if line.startswith("!"):
            match = _PUBLISH_RE.match(line)
            if match is None:
                log.debug(f"[{self.device_id}] Unread push line: {line!r}")
                return
            token, raw, ack = match.groups()
            key = self._tokens.get(token)
            if key is not None:
                self._apply(key, _parse_value(raw))
            if ack:
                self._resolve(None)
            return
        if line.startswith("+OK"):
            rest = line[3:].strip()
            if not rest:
                self._resolve(None)
                return
            match = _VALUE_RE.match(rest)
            if match is not None:
                self._resolve(_parse_value(match.group(1)))
                return
            if not self._bare_reply_logged:
                self._bare_reply_logged = True
                log.info(
                    f"[{self.device_id}] The bar answered without a value label: "
                    f"{line!r}")
            self._resolve(_parse_value(rest))
            return
        # The Text Protocol family's error forms: -ERR <reason>, -CANNOT_DELIVER,
        # -GENERAL_FAILURE. A greeting's rule of dashes is not one.
        if _ERROR_RE.match(line):
            self._fail(line[1:].strip())
            return
        # The command echoed back by the terminal, or a greeting.
        log.debug(f"[{self.device_id}] Skipped line: {line!r}")

    def _resolve(self, value: Any) -> None:
        inflight = self._inflight
        if inflight is None:
            return
        fut = inflight[1]
        if not fut.done():
            fut.set_result(value)

    def _fail(self, reason: str) -> None:
        inflight = self._inflight
        if inflight is None:
            log.warning(f"[{self.device_id}] Unclaimed error line: {reason}")
            return
        fut = inflight[1]
        if not fut.done():
            fut.set_exception(BiampError(reason))

    def _apply(self, key: str, value: Any) -> None:
        """A reported value into its state variable, by the variable's kind."""
        kind = _BY_KEY[key][2] if key in _BY_KEY else "string"
        if kind == "bool":
            flag = _to_bool(value)
            if flag is not None:
                self.set_state(key, flag)
        elif kind == "int":
            number = _to_number(value)
            if number is not None:
                self.set_state(key, int(round(number)))
        elif kind == "number":
            number = _to_number(value)
            if number is not None:
                self.set_state(key, number)
        elif kind in ("input_source", "als_source"):
            table = INPUT_SOURCES if kind == "input_source" else ALS_SOURCES
            number = _to_number(value)
            word = table.get(int(number)) if number is not None else None
            if word is not None:
                self.set_state(key, word)
        elif value is not None:
            self.set_state(key, str(value))

    # ── Reads and subscriptions ──

    async def _read_identity(self) -> None:
        for line, key in (
            # Asked again: _post_connect's answer may have been an error.
            ("DEVICE get serialNumber", "serial_number"),
            ("DEVICE get version", "firmware_version"),
            # Listed by Biamp's command calculator, not by the API article.
            ("DEVICE get modelName", "model_name"),
        ):
            try:
                value = await self._request(line)
            except BiampError as exc:
                log.info(f"[{self.device_id}] '{line}' refused: {exc}")
                continue
            if value is not None:
                self.set_state(key, str(value))
        try:
            value = await self._request("Camera get maxZoom")
        except BiampError as exc:
            log.info(f"[{self.device_id}] 'Camera get maxZoom' refused: {exc}")
        else:
            number = _to_number(value)
            if number is not None:
                self.set_state("max_zoom", number)

    async def _subscribe_all(self) -> None:
        rate = int(self.config.get("update_rate_ms") or DEFAULT_UPDATE_RATE_MS)
        self._tokens.clear()
        refused: list[str] = []
        for subject, attr, key, _kind, rated in ATTRIBUTES:
            token = f"{TOKEN_PREFIX}{key}"
            # Registered first: the current value arrives before the +OK.
            self._tokens[token] = key
            line = f"{subject} subscribe {attr} {token}"
            if rated:
                line += f" {rate}"
            try:
                await self._request(line)
            except BiampError as exc:
                self._tokens.pop(token, None)
                refused.append(key)
                log.info(
                    f"[{self.device_id}] Subscription to {subject} {attr} refused "
                    f"({exc}); it is read on the poll instead")
        self._unsubscribed = refused
        for key in refused:
            await self._read_attribute(key)

    async def _read_attribute(self, key: str) -> None:
        subject, attr, _kind = _BY_KEY[key]
        try:
            value = await self._request(f"{subject} get {attr}")
        except BiampError as exc:
            log.debug(f"[{self.device_id}] '{subject} get {attr}' refused: {exc}")
            return
        self._apply(key, value)

    # ── Poll and liveness ──

    async def poll(self) -> None:
        # A timeout or a dropped session raises, and counts toward offline.
        try:
            version = await self._request("DEVICE get version")
        except BiampError as exc:
            log.warning(f"[{self.device_id}] Firmware read refused: {exc}")
        else:
            if version is not None:
                self.set_state("firmware_version", str(version))
        for key in list(self._unsubscribed):
            await self._read_attribute(key)

    async def _liveness_probe(self) -> None:
        try:
            await self._request("DEVICE get version")
        except BiampError:
            return  # an error answer is an answer

    # ── Commands ──

    async def _send(self, line: str, *, sets: tuple[str, ...] = ()) -> Any:
        """Send a command the user asked for; a refusal becomes the sentence
        the device page shows."""
        try:
            value = await self._request(line)
        except BiampError as exc:
            sentence = f"The bar refused '{line}': {exc}."
            self.set_state("last_error", sentence)
            raise CommandRefusedError(sentence) from exc
        # An increment or decrement of a USB level answers the new level.
        if value is not None and sets:
            for key in sets:
                self._apply(key, value)
        # A value whose subscription was refused is read back now, not at the
        # next poll.
        words = line.split()
        if self._unsubscribed and len(words) >= 3:
            target = (words[0], words[2])
            changed = (_KEY_OF.get(target),) + _ALSO_CHANGES.get(target, ())
            for key in changed:
                if key in self._unsubscribed:
                    await self._read_attribute(key)
        return True

    async def set_device_setting(self, key: str, value: Any) -> Any:
        target = SETTINGS.get(key)
        if target is None:
            raise ValueError(f"Unknown device setting: {key}")
        subject, attr = target
        if key == "input_source":
            wire = _source_digit(INPUT_SOURCES, value)
        elif key == "als_source":
            wire = _source_digit(ALS_SOURCES, value)
        elif key in ("backlight_compensation", "flip", "mirror"):
            wire = _wire_bool(value)
        else:
            wire = _wire_float(value)
        await self._send(f"{subject} set {attr} {wire}")
        return True

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None,
    ) -> Any:
        params = params or {}

        # Audio paths: set / up / down level, mute / unmute / toggle.
        for path, subject in AUDIO_PATHS.items():
            usb = subject.startswith("USB")
            level_key = f"{path}_level"
            if command == f"set_{path}_level":
                wire = _wire_int(params["level"]) if usb else _wire_float(params["level"])
                return await self._send(f"{subject} set level {wire}")
            if command in (f"{path}_level_up", f"{path}_level_down"):
                verb = "increment" if command.endswith("_up") else "decrement"
                step = params.get("step", 5)
                wire = _wire_int(step) if usb else _wire_float(step)
                return await self._send(
                    f"{subject} {verb} level {wire}", sets=(level_key,))
            if command == f"{path}_mute":
                return await self._send(f"{subject} set mute true")
            if command == f"{path}_unmute":
                return await self._send(f"{subject} set mute false")
            if command == f"toggle_{path}_mute":
                return await self._send(f"{subject} toggle mute")

        if command == "set_input_source":
            digit = _source_digit(INPUT_SOURCES, params["source"])
            return await self._send(f"InputSource set input {digit}")
        if command == "set_als_source":
            digit = _source_digit(ALS_SOURCES, params["source"])
            return await self._send(f"ALSSource set input {digit}")

        for attr in ("autoframing", "tiling"):
            if command == f"{attr}_on":
                return await self._send(f"Camera set {attr} true")
            if command == f"{attr}_off":
                return await self._send(f"Camera set {attr} false")
            if command == f"toggle_{attr}":
                return await self._send(f"Camera toggle {attr}")

        if command == "set_pan":
            return await self._send(f"Camera set pan {_wire_int(params['pan'])}")
        if command in ("pan_left", "pan_right"):
            # -100 is full left.
            verb = "decrement" if command == "pan_left" else "increment"
            return await self._send(
                f"Camera {verb} pan {_wire_int(params.get('step', 5))}")
        if command == "set_tilt":
            return await self._send(f"Camera set tilt {_wire_int(params['tilt'])}")
        if command in ("tilt_up", "tilt_down"):
            # -100 is full down.
            verb = "increment" if command == "tilt_up" else "decrement"
            return await self._send(
                f"Camera {verb} tilt {_wire_int(params.get('step', 5))}")
        if command == "set_zoom":
            return await self._send(f"Camera set zoom {_wire_float(params['zoom'])}")
        if command in ("zoom_in", "zoom_out"):
            verb = "increment" if command == "zoom_in" else "decrement"
            return await self._send(
                f"Camera {verb} zoom {_wire_float(params.get('step', 0.5))}")
        if command == "recall_camera_preset":
            return await self._send(
                f"Camera set presetRecall {_wire_int(params['preset'])}")
        if command == "save_camera_preset":
            return await self._send(
                f"Camera set presetSave {_wire_int(params['preset'])}")
        if command == "reboot":
            return await self._send("DEVICE reboot")

        log.warning(f"[{self.device_id}] Unknown command: {command}")
        return None
