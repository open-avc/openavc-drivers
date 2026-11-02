"""
OpenAVC Bose Professional Videobar Driver (Videobar REST API).

Controls Bose Professional Videobar VB1 and VB-S USB conferencing bars through
the REST API the bar serves on HTTPS: microphone and loudspeaker mute,
loudspeaker volume, the camera's pan, tilt and zoom and its three presets,
autoframing, Bluetooth pairing, the Bluetooth, Wi-Fi, Ethernet and HDMI
interfaces, a reboot, and the bar's name and location. Reads whether a
computer is connected over USB and in a call, the camera's state, Bluetooth's
state, the network interfaces and, on a VB1, how many people the camera counts
and the GPI mute input.

Push vs poll
------------
Poll only. The REST API Guide v1.5 documents no subscription or callback. The
bar reports changes only as SNMPv3 traps (the SNMP Admin Guide marks mute, the
USB and Bluetooth call states, the camera state and autoframing as
"subscribe"), which OpenAVC does not receive. Mute, volume, the USB and
Bluetooth call states, the camera, autoframing and the people count are read
every ``poll_interval`` (default 3 s, all in one request); identity, location,
the interfaces and the preset positions every 30 s. Every command reads what
it changed back at once.

Why Python
----------
Recalling a camera preset is two requests in order: camera.activePreset is
set, then camera.applyActivePreset moves the camera there (the guide; Bose's
Extron module does the same). A YAML command is one request. Every write is
also read back in the same command (a write's answer carries a code, not the
new value, and Volume Up and Volume Down unmute the loudspeaker), each
property's refusal code is read out of an answer that covers several at once,
and the commands only a VB1 has follow the model the bar reports.

Protocol
--------
``GET /api?query=<id>,<id>`` answers ``{"<id>": {"status": "success",
"value": "<text>"}, ...}``. ``PUT /api`` (a value) and ``POST /api`` (an
action, with an empty value) take a field named ``data`` holding a JSON object
of id and value strings, and answer ``{"<id>": {"status": "success", "code":
"0xe000"}, ...}``. The guide prints the body as ``{"data":"{"3":"39"}"}``,
which is not valid JSON; this driver sends ``data`` as a form field
(application/x-www-form-urlencoded), the encoding a working integration on
firmware 1.5.1 uses. IDs are hexadecimal text (``D6``, ``0F``), the same
numbers as the SNMP guide's OIDs (D6 is .214).

The loudspeaker mute is ID 33, read and set as 1 or 0. The REST guide gives
audio.loudspeakerMute and audio.loudspeakerMuteToggle the same ID, 34, both as
actions; the SNMP Admin Guide lists audio.loudspeakerMute as OID .51 (0x33),
retrieve and update, and the toggle as .52 (0x34).

Authentication and transport
----------------------------
The REST API is off until API access is turned on under Network > API (the
Videobar Configuration app, the Videobar Administration app or the web UI)
with an API username and password. Every request carries them as HTTP Basic.
HTTPS on port 443 with the bar's own certificate.

Source:
    Bose Professional Videobar REST API Guide v1.5 (December 2023):
    https://assets.boseprofessional.com/m/380d978c3e7034f3/original/ug_videobar_rest_api.pdf
    Bose Professional Videobar SNMP Admin Guide v1.5 (the models each property
    applies to, its values, and the loudspeaker mute's ID):
    https://assets.boseprofessional.com/m/5915656503e73eb6/original/ug_videobar_snmp.pdf
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from typing import Any

import httpx

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.utils.logger import get_logger

try:
    from openavc.drivers.base import CommandRefusedError
except ImportError:  # OpenAVC before 0.37.1
    CommandRefusedError = RuntimeError

log = get_logger(__name__)

# A write's answer code (REST API Guide, PUT), as a person reads it.
CODES = {
    "0xe000": "success",
    "0xe001": "no change in value",
    "0xe002": "invalid property",
    "0xe003": "invalid property value",
    "0xe004": "invalid property action",
    "0xe005": "message malformed",
    "0xe006": "access denied",
}
ACCEPTED = {"0xe000", "0xe001"}
INVALID_PROPERTY = "0xe002"
ACCESS_DENIED = "0xe006"

# Each property this driver reads: id -> (state variable, how its text reads).
PROPERTIES = {
    "2": ("mic_mute", "bool"),
    "33": ("speaker_mute", "bool"),
    "3": ("volume", "int"),
    "36": ("usb_connected", "bool"),
    "37": ("usb_in_call", "bool"),
    "60": ("camera_state", "text"),
    "19": ("autoframing", "bool"),
    "6": ("camera_zoom", "int"),
    "7": ("camera_pan", "int"),
    "8": ("camera_tilt", "int"),
    "13": ("active_preset", "preset"),
    "14": ("bluetooth_pairing", "bool"),
    "6B": ("bluetooth_connected", "bool"),
    "C2": ("bluetooth_streaming", "bool"),
    "6C": ("bluetooth_in_call", "bool"),
    "C7": ("gpi_mute", "bool"),
    "DA": ("people_count", "int"),
    "DC": ("people_present", "bool"),
    "D6": ("model", "text"),
    "10": ("serial_number", "text"),
    "16": ("firmware_version", "text"),
    "25": ("device_name", "text"),
    "26": ("room", "text"),
    "27": ("floor", "text"),
    "28": ("building", "text"),
    "DF": ("max_occupancy", "text"),
    "3A": ("bluetooth_enabled", "bool"),
    "3B": ("wifi_enabled", "bool"),
    "67": ("bluetooth_on", "bool"),
    "6A": ("bluetooth_paired_device", "text"),
    "56": ("home_preset_position", "text"),
    "57": ("preset_1_position", "text"),
    "58": ("preset_2_position", "text"),
    "A1": ("wifi_dhcp", "bool"),
    "A2": ("wifi_static_ip", "text"),
    "AC": ("wifi_mac", "text"),
    "B0": ("wifi_state", "text"),
    "38": ("ethernet_enabled", "bool"),
    "C9": ("hdmi_enabled", "bool"),
    "74": ("ethernet_dhcp", "bool"),
    "75": ("ethernet_static_ip", "text"),
    "7F": ("ethernet_state", "text"),
    "80": ("ethernet_mac", "text"),
}

# Read every poll, and every 30 s. The *_VB1 lists are the properties the SNMP
# Admin Guide gives for the VB1 only.
FAST = ["2", "33", "3", "36", "37", "60", "19", "6", "7", "8", "13", "14", "6B", "C2", "6C"]
FAST_VB1 = ["C7", "DA", "DC"]
SLOW = [
    "D6", "10", "16", "25", "26", "27", "28", "DF", "3A", "3B", "67", "6A",
    "56", "57", "58", "A1", "A2", "AC", "B0",
]
SLOW_VB1 = ["38", "C9", "74", "75", "7F", "80"]
IDENTITY = ["D6", "10", "16"]
SLOW_INTERVAL_S = 30.0

CAMERA = ["6", "7", "8"]
AUDIO = ["3", "33"]

# camera.activePreset: 1 is Home, 2 the first preset, 3 the second.
PRESET_NUMBERS = {"home": "1", "preset_1": "2", "preset_2": "3"}
PRESET_NAMES = {number: name for name, number in PRESET_NUMBERS.items()}
PRESET_LABELS = {"home": "Home", "preset_1": "Preset 1", "preset_2": "Preset 2"}
# camera.savePresetHome / savePresetFirst / savePresetSecond, and the
# property that holds each preset's position.
PRESET_SAVE = {"home": "12", "preset_1": "17", "preset_2": "18"}
PRESET_POSITION = {"home": "56", "preset_1": "57", "preset_2": "58"}

# Commands for interfaces only a VB1 has (behavior.hdmiEnabled and
# behavior.ethernetEnabled are VB1 only in the SNMP Admin Guide).
VB1_COMMANDS = ("hdmi_on", "hdmi_off", "ethernet_on", "ethernet_off")

# Device settings: setting key -> property id. The guide allows 0 to 128
# characters for each.
TEXT_SETTINGS = {"room": "26", "floor": "27", "building": "28", "max_occupancy": "DF"}
TEXT_SETTING_MAX = 128


def _norm(pid: Any) -> str:
    """An id as the bar may write it back: hex digits without leading zeros
    ("0F" and "F" are the same property)."""
    text = str(pid).strip().upper()
    try:
        return format(int(text, 16), "X")
    except ValueError:
        return text


PROPERTIES_BY_ID = {_norm(pid): spec for pid, spec in PROPERTIES.items()}


def _entries(payload: Any) -> dict[str, dict]:
    """The per-property entries of an answer, keyed by normalized id."""
    if not isinstance(payload, dict):
        return {}
    return {_norm(k): v for k, v in payload.items() if isinstance(v, dict)}


def _code(entry: dict) -> str:
    return str(entry.get("code", "") or "").strip().lower()


def _succeeded(entry: dict) -> bool:
    return str(entry.get("status", "") or "").strip().lower() == "success"


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _family_of(model: str) -> str:
    """VB1 or VB-S from the model the bar reports; "" when it is neither."""
    if re.search(r"vb\W?1", model, re.IGNORECASE):
        return "VB1"
    if re.search(r"vb\W?s", model, re.IGNORECASE):
        return "VB-S"
    return ""


class VideobarError(Exception):
    """An answer that is not the API's per-property JSON."""

    def __init__(self, message: str, *, http_status: int = 0) -> None:
        super().__init__(message)
        self.http_status = http_status


class BoseVideobarDriver(BaseDriver):
    """Bose Professional Videobar VB1 / VB-S over the Videobar REST API."""

    DRIVER_INFO = {
        "id": "bose_videobar",
        "name": "Bose Professional Videobar",
        "manufacturer": "Bose Professional",
        "category": "video",
        "version": "1.0.0",
        "min_platform_version": "0.37.0",
        "author": "OpenAVC",
        "description": (
            "Controls Bose Professional Videobar VB1 and VB-S conferencing bars "
            "over the bar's REST API: microphone and loudspeaker mute, volume, "
            "camera pan, tilt, zoom and presets, autoframing and Bluetooth. Shows "
            "when a computer is in a call over USB and, on a VB1, how many "
            "people the camera counts."
        ),
        "source_url": "https://assets.boseprofessional.com/m/380d978c3e7034f3/original/ug_videobar_rest_api.pdf",
        "tags": ["bose", "bose-professional", "videobar", "vb1", "vb-s", "video-bar", "conferencing", "camera", "rest"],
        "verified": False,
        "simulated": True,
        "protocols": ["bose_videobar_rest"],
        "ports": [443],
        "transport": "http",
        "discovery": {
            # The IEEE blocks of Bose Corporation, which made the VB1 at its
            # launch, and Bose Professional's own (Transom Post OpCo, MA-M).
            # No identifying probe: neither guide documents an unauthenticated
            # answer, so a Bose MAC is a possible match only.
            "oui": [
                "00:0c:8a", "04:52:c7", "08:df:1f", "28:11:a5", "2c:41:a1", "48:22:1d",
                "4c:87:5d", "60:ab:d2", "68:f2:1f", "78:2b:64", "ac:bf:71", "bc:87:fa",
                "c8:7b:23", "e4:58:bc", "48:5e:0e:c",
            ],
            "manufacturer_alias": ["bose", "bose professional", "bose corporation", "videobar"],
        },
        "compatible_models": [
            {
                "manufacturer": "Bose Professional",
                "models": ["Videobar VB1", "Videobar VB-S"],
                "confidence": "untested",
                "notes": (
                    "REST API Guide v1.5. API access is off until it is turned on "
                    "under Network > API with an API username and password. The "
                    "people count, GPI mute, HDMI output and Ethernet interface "
                    "are on the VB1 only; a VB-S is on the network over Wi-Fi."
                ),
            },
        ],
        "help": {
            "overview": (
                "Controls a Bose Professional Videobar VB1 or VB-S: microphone and "
                "loudspeaker mute, loudspeaker volume, the camera's pan, tilt and "
                "zoom, its Home, Preset 1 and Preset 2 positions, autoframing, "
                "Bluetooth pairing and a reboot. Shows whether a computer is "
                "connected over USB and in a call, the camera's state, Bluetooth "
                "and network status and, on a VB1, how many people the camera "
                "counts and the GPI mute input. Mute and the call states are read "
                "every few seconds, so a press of the bar's own mute button shows "
                "within one poll."
            ),
            "setup": (
                "1. In the Bose Videobar Configuration app (over USB), the "
                "Videobar Administration app or the bar's web page, open Network > "
                "API, turn on API access and set an API username and password.\n"
                "2. Give the bar a fixed IP address or a DHCP reservation. A VB-S "
                "is on the network over Wi-Fi only.\n"
                "3. In OpenAVC, enter the bar's IP address and the API username "
                "and password, and pick the model. Once the bar has connected, "
                "the model it reports is saved in its place.\n"
                "4. Leave Verify SSL Certificate off unless the bar has a "
                "certificate this computer trusts."
            ),
        },
        "default_config": {
            "host": "",
            "port": 443,
            "verify_ssl": False,
            "username": "",
            "password": "",
            "model": "VB1",
            "poll_interval": 3,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "default": 443, "min": 1, "max": 65535, "label": "HTTPS Port"},
            "verify_ssl": {
                "type": "boolean",
                "default": False,
                "label": "Verify SSL Certificate",
                "description": (
                    "The bar ships with its own certificate. Leave this off unless "
                    "the bar has a certificate this computer trusts."
                ),
            },
            "username": {
                "type": "string",
                "default": "",
                "required": True,
                "label": "API Username",
                "description": "The API username set under Network > API on the bar.",
            },
            "password": {
                "type": "string",
                "default": "",
                "required": True,
                "label": "API Password",
                "secret": True,
                "description": "The API password set under Network > API on the bar.",
            },
            "model": {
                "type": "enum",
                "values": ["VB1", "VB-S"],
                "default": "VB1",
                "label": "Model",
                "learned_from": "model_family",
                "description": (
                    "VB1 or VB-S. The people count, GPI mute, HDMI output and "
                    "Ethernet interface are on the VB1 only."
                ),
            },
            "poll_interval": {
                "type": "integer",
                "default": 3,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "How often mute, volume, the call states and the camera are "
                    "read. 0 stops polling."
                ),
            },
        },
        "state_variables": {
            "mic_mute": {
                "type": "boolean",
                "label": "Microphone Mute",
                "help": "True while the bar's microphones are muted.",
                "control": True,
                "cloud_priority": "high",
            },
            "speaker_mute": {
                "type": "boolean",
                "label": "Loudspeaker Mute",
                "help": "True while the bar's loudspeaker is muted. Volume Up and Volume Down unmute it.",
                "control": True,
                "cloud_priority": "high",
            },
            "volume": {
                "type": "integer",
                "label": "Loudspeaker Volume",
                "min": 0,
                "max": 100,
                "step": 1,
                "control": True,
            },
            "usb_connected": {
                "type": "boolean",
                "label": "USB Connected",
                "help": "True while a computer is connected to the bar's USB port.",
            },
            "usb_in_call": {
                "type": "boolean",
                "label": "In a Call (USB)",
                "help": "True while the computer on the USB port is in a call.",
                "cloud_priority": "high",
            },
            "camera_state": {
                "type": "enum",
                "values": ["active", "inactive", "upgrading"],
                "label": "Camera State",
                "help": "active while the camera is streaming video, inactive while it is not, upgrading during a camera firmware update.",
            },
            "autoframing": {
                "type": "boolean",
                "label": "Autoframing",
                "help": "True while the camera's autoframing is on.",
                "control": True,
            },
            "camera_pan": {
                "type": "integer",
                "label": "Camera Pan",
                "min": -10,
                "max": 10,
                "step": 1,
                "control": True,
                "cloud_priority": "low",
            },
            "camera_tilt": {
                "type": "integer",
                "label": "Camera Tilt",
                "min": -10,
                "max": 10,
                "step": 1,
                "control": True,
                "cloud_priority": "low",
            },
            "camera_zoom": {
                "type": "integer",
                "label": "Camera Zoom",
                "min": 1,
                "max": 10,
                "step": 1,
                "control": True,
                "cloud_priority": "low",
            },
            "active_preset": {
                "type": "enum",
                "values": ["home", "preset_1", "preset_2"],
                "label": "Active Preset",
                "help": "The camera preset last selected. The camera starts on Home.",
                "control": True,
            },
            "home_preset_position": {
                "type": "string",
                "label": "Home Preset Position",
                "help": "Pan, tilt and zoom saved in the Home preset, separated by spaces.",
            },
            "preset_1_position": {
                "type": "string",
                "label": "Preset 1 Position",
                "help": "Pan, tilt and zoom saved in Preset 1, separated by spaces.",
            },
            "preset_2_position": {
                "type": "string",
                "label": "Preset 2 Position",
                "help": "Pan, tilt and zoom saved in Preset 2, separated by spaces.",
            },
            "people_count": {
                "type": "integer",
                "label": "People Count",
                "min": 0,
                "max": 99,
                "help": "How many people the camera's autoframing counts. VB1 only.",
            },
            "people_present": {
                "type": "boolean",
                "label": "People Present",
                "help": "True while the camera's autoframing sees anyone. VB1 only.",
                "cloud_priority": "high",
            },
            "gpi_mute": {
                "type": "boolean",
                "label": "GPI Mute",
                "help": "True while the GPI mute input is on. VB1 only.",
            },
            "bluetooth_enabled": {
                "type": "boolean",
                "label": "Bluetooth Enabled",
                "help": "Whether the bar's Bluetooth is turned on in its settings.",
            },
            "bluetooth_on": {
                "type": "boolean",
                "label": "Bluetooth On",
                "help": "True while Bluetooth and Bluetooth Low Energy are running.",
            },
            "bluetooth_pairing": {
                "type": "boolean",
                "label": "Bluetooth Pairing",
                "help": "True while the bar accepts a new Bluetooth pairing. It turns off by itself when the pairing time ends.",
                "control": True,
            },
            "bluetooth_paired_device": {
                "type": "string",
                "label": "Paired Bluetooth Device",
            },
            "bluetooth_connected": {
                "type": "boolean",
                "label": "Bluetooth Connected",
                "help": "True while the paired Bluetooth device is connected.",
            },
            "bluetooth_streaming": {
                "type": "boolean",
                "label": "Bluetooth Streaming",
                "help": "True while audio is streaming over Bluetooth.",
            },
            "bluetooth_in_call": {
                "type": "boolean",
                "label": "In a Call (Bluetooth)",
                "help": "True while the Bluetooth device is in a call.",
                "cloud_priority": "high",
            },
            "wifi_enabled": {
                "type": "boolean",
                "label": "Wi-Fi Enabled",
            },
            "wifi_state": {
                "type": "enum",
                "values": ["idle", "failure", "association", "configuration", "ready", "disconnect", "online"],
                "label": "Wi-Fi State",
            },
            "wifi_dhcp": {
                "type": "boolean",
                "label": "Wi-Fi DHCP",
                "help": "True when Wi-Fi takes its address from DHCP, false when it uses its static address.",
            },
            "wifi_static_ip": {
                "type": "string",
                "label": "Wi-Fi Static IP Address",
                "help": "The address Wi-Fi uses while DHCP is off.",
            },
            "wifi_mac": {
                "type": "string",
                "label": "Wi-Fi MAC Address",
            },
            "ethernet_enabled": {
                "type": "boolean",
                "label": "Ethernet Enabled",
                "help": "VB1 only.",
            },
            "ethernet_state": {
                "type": "enum",
                "values": ["idle", "failure", "association", "configuration", "ready", "disconnect", "online"],
                "label": "Ethernet State",
                "help": "VB1 only.",
            },
            "ethernet_dhcp": {
                "type": "boolean",
                "label": "Ethernet DHCP",
                "help": "True when Ethernet takes its address from DHCP, false when it uses its static address. VB1 only.",
            },
            "ethernet_static_ip": {
                "type": "string",
                "label": "Ethernet Static IP Address",
                "help": "The address Ethernet uses while DHCP is off. VB1 only.",
            },
            "ethernet_mac": {
                "type": "string",
                "label": "Ethernet MAC Address",
                "help": "VB1 only.",
            },
            "hdmi_enabled": {
                "type": "boolean",
                "label": "HDMI Enabled",
                "help": "Whether the bar's HDMI output is turned on. VB1 only.",
            },
            "model": {
                "type": "string",
                "label": "Model",
                "help": "The model the bar reports.",
            },
            "model_family": {
                "type": "enum",
                "values": ["VB1", "VB-S"],
                "label": "Model (VB1 or VB-S)",
                "help": "VB1 or VB-S, from the model the bar reports.",
            },
            "serial_number": {
                "type": "string",
                "label": "Serial Number",
            },
            "firmware_version": {
                "type": "string",
                "label": "Firmware Version",
            },
            "device_name": {
                "type": "string",
                "label": "Device Name",
            },
            "room": {
                "type": "string",
                "label": "Room",
            },
            "floor": {
                "type": "string",
                "label": "Floor",
            },
            "building": {
                "type": "string",
                "label": "Building",
            },
            "max_occupancy": {
                "type": "string",
                "label": "Maximum Occupancy",
            },
            "last_error": {
                "type": "string",
                "label": "Last Error",
                "help": "A value the bar did not answer on the last poll; cleared by the next clean poll.",
            },
        },
        "device_settings": {
            "room": {
                "type": "string",
                "label": "Room",
                "state_key": "room",
                "default": "",
                "setup": False,
                "regex": "^.{0,128}$",
                "help": "The room the bar is in, up to 128 characters.",
            },
            "floor": {
                "type": "string",
                "label": "Floor",
                "state_key": "floor",
                "default": "",
                "setup": False,
                "regex": "^.{0,128}$",
                "help": "The floor the bar is on, up to 128 characters.",
            },
            "building": {
                "type": "string",
                "label": "Building",
                "state_key": "building",
                "default": "",
                "setup": False,
                "regex": "^.{0,128}$",
                "help": "The building the bar is in, up to 128 characters.",
            },
            "max_occupancy": {
                "type": "string",
                "label": "Maximum Occupancy",
                "state_key": "max_occupancy",
                "default": "",
                "setup": False,
                "regex": "^.{0,128}$",
                "help": "The room's maximum occupancy, as text up to 128 characters.",
            },
        },
        "commands": {
            "mic_mute": {
                "label": "Microphone Mute",
                "params": {},
                "sets": {"mic_mute": True},
                "help": "Mutes the bar's microphones.",
            },
            "mic_unmute": {
                "label": "Microphone Unmute",
                "params": {},
                "sets": {"mic_mute": False},
                "help": "Unmutes the bar's microphones.",
            },
            "mic_mute_toggle": {
                "label": "Microphone Mute Toggle",
                "params": {},
                "help": "Mutes the bar's microphones if they are on, unmutes them if they are muted.",
            },
            "speaker_mute": {
                "label": "Loudspeaker Mute",
                "params": {},
                "sets": {"speaker_mute": True},
                "help": "Mutes the bar's loudspeaker.",
            },
            "speaker_unmute": {
                "label": "Loudspeaker Unmute",
                "params": {},
                "sets": {"speaker_mute": False},
                "help": "Unmutes the bar's loudspeaker.",
            },
            "speaker_mute_toggle": {
                "label": "Loudspeaker Mute Toggle",
                "params": {},
                "help": "Mutes the bar's loudspeaker if it is on, unmutes it if it is muted.",
            },
            "set_volume": {
                "label": "Set Volume",
                "params": {
                    "level": {"type": "integer", "required": True, "min": 0, "max": 100, "label": "Volume"},
                },
                "sets": {"volume": "{level}"},
                "help": "Sets the loudspeaker volume, 0 to 100.",
            },
            "volume_up": {
                "label": "Volume Up",
                "params": {},
                "help": "Turns the loudspeaker up one step. Unmutes the loudspeaker if it is muted.",
            },
            "volume_down": {
                "label": "Volume Down",
                "params": {},
                "help": "Turns the loudspeaker down one step. Unmutes the loudspeaker if it is muted.",
            },
            "recall_preset": {
                "label": "Recall Camera Preset",
                "params": {
                    "preset": {
                        "type": "enum",
                        "required": True,
                        "label": "Preset",
                        "values": [
                            {"value": "home", "label": "Home"},
                            {"value": "preset_1", "label": "Preset 1"},
                            {"value": "preset_2", "label": "Preset 2"},
                        ],
                        "default": "home",
                    },
                },
                "sets": {"active_preset": "{preset}"},
                "help": "Moves the camera to a saved preset position.",
            },
            "save_preset": {
                "label": "Save Camera Preset",
                "params": {
                    "preset": {
                        "type": "enum",
                        "required": True,
                        "label": "Preset",
                        "values": [
                            {"value": "home", "label": "Home"},
                            {"value": "preset_1", "label": "Preset 1"},
                            {"value": "preset_2", "label": "Preset 2"},
                        ],
                        "default": "home",
                    },
                },
                "help": "Saves the camera's current pan, tilt and zoom in a preset, replacing what it held.",
            },
            "set_camera_position": {
                "label": "Set Camera Position",
                "params": {
                    "pan": {"type": "integer", "required": True, "min": -10, "max": 10, "label": "Pan"},
                    "tilt": {"type": "integer", "required": True, "min": -10, "max": 10, "label": "Tilt"},
                    "zoom": {"type": "integer", "required": True, "min": 1, "max": 10, "label": "Zoom"},
                },
                "sets": {"camera_pan": "{pan}", "camera_tilt": "{tilt}", "camera_zoom": "{zoom}"},
                "help": "Moves the camera to a pan and a tilt (-10 to 10) and a zoom (1 to 10).",
            },
            "set_camera_pan": {
                "label": "Set Camera Pan",
                "params": {
                    "pan": {"type": "integer", "required": True, "min": -10, "max": 10, "label": "Pan"},
                },
                "sets": {"camera_pan": "{pan}"},
                "help": "Pans the camera to a position from -10 to 10.",
            },
            "set_camera_tilt": {
                "label": "Set Camera Tilt",
                "params": {
                    "tilt": {"type": "integer", "required": True, "min": -10, "max": 10, "label": "Tilt"},
                },
                "sets": {"camera_tilt": "{tilt}"},
                "help": "Tilts the camera to a position from -10 to 10.",
            },
            "set_camera_zoom": {
                "label": "Set Camera Zoom",
                "params": {
                    "zoom": {"type": "integer", "required": True, "min": 1, "max": 10, "label": "Zoom"},
                },
                "sets": {"camera_zoom": "{zoom}"},
                "help": "Zooms the camera to a position from 1 to 10.",
            },
            "camera_pan_left": {
                "label": "Camera Pan Left",
                "params": {},
                "help": "Pans the camera left one step.",
            },
            "camera_pan_right": {
                "label": "Camera Pan Right",
                "params": {},
                "help": "Pans the camera right one step.",
            },
            "camera_tilt_up": {
                "label": "Camera Tilt Up",
                "params": {},
                "help": "Tilts the camera up one step.",
            },
            "camera_tilt_down": {
                "label": "Camera Tilt Down",
                "params": {},
                "help": "Tilts the camera down one step.",
            },
            "camera_zoom_in": {
                "label": "Camera Zoom In",
                "params": {},
                "help": "Zooms the camera in one step.",
            },
            "camera_zoom_out": {
                "label": "Camera Zoom Out",
                "params": {},
                "help": "Zooms the camera out one step.",
            },
            "autoframing_on": {
                "label": "Autoframing On",
                "params": {},
                "sets": {"autoframing": True},
                "help": "Turns on the camera's autoframing.",
            },
            "autoframing_off": {
                "label": "Autoframing Off",
                "params": {},
                "sets": {"autoframing": False},
                "help": "Turns off the camera's autoframing.",
            },
            "autoframing_toggle": {
                "label": "Autoframing Toggle",
                "params": {},
                "help": "Turns autoframing off if it is on, on if it is off.",
            },
            "bluetooth_pairing_on": {
                "label": "Bluetooth Pairing On",
                "params": {},
                "sets": {"bluetooth_pairing": True},
                "help": "Lets a Bluetooth device pair with the bar until the pairing time ends.",
            },
            "bluetooth_pairing_off": {
                "label": "Bluetooth Pairing Off",
                "params": {},
                "sets": {"bluetooth_pairing": False},
                "help": "Stops accepting a new Bluetooth pairing.",
            },
            "bluetooth_pairing_toggle": {
                "label": "Bluetooth Pairing Toggle",
                "params": {},
                "help": "Turns Bluetooth pairing on if it is off, off if it is on.",
            },
            "bluetooth_disconnect": {
                "label": "Bluetooth Disconnect",
                "params": {},
                "sets": {"bluetooth_connected": False},
                "help": "Disconnects the connected Bluetooth device.",
            },
            "bluetooth_on": {
                "label": "Bluetooth On",
                "params": {},
                "sets": {"bluetooth_enabled": True},
                "help": "Turns the bar's Bluetooth on.",
            },
            "bluetooth_off": {
                "label": "Bluetooth Off",
                "params": {},
                "sets": {"bluetooth_enabled": False},
                "help": "Turns the bar's Bluetooth off.",
            },
            "hdmi_on": {
                "label": "HDMI On",
                "params": {},
                "sets": {"hdmi_enabled": True},
                "help": "Turns on the bar's HDMI output. VB1 only.",
            },
            "hdmi_off": {
                "label": "HDMI Off",
                "params": {},
                "sets": {"hdmi_enabled": False},
                "help": "Turns off the bar's HDMI output. VB1 only.",
            },
            "wifi_on": {
                "label": "Wi-Fi On",
                "params": {},
                "sets": {"wifi_enabled": True},
                "help": "Turns on the bar's Wi-Fi.",
            },
            "wifi_off": {
                "label": "Wi-Fi Off",
                "params": {},
                "confirm": (
                    "Turns off the bar's Wi-Fi. If OpenAVC reaches the bar over "
                    "Wi-Fi, it loses the bar until Wi-Fi is turned back on with the "
                    "Videobar Configuration app over USB."
                ),
                "help": "Turns off the bar's Wi-Fi.",
            },
            "ethernet_on": {
                "label": "Ethernet On",
                "params": {},
                "sets": {"ethernet_enabled": True},
                "help": "Turns on the bar's Ethernet port. VB1 only.",
            },
            "ethernet_off": {
                "label": "Ethernet Off",
                "params": {},
                "confirm": (
                    "Turns off the bar's Ethernet port. If OpenAVC reaches the bar "
                    "over Ethernet, it loses the bar until Ethernet is turned back on "
                    "with the Videobar Configuration app over USB."
                ),
                "help": "Turns off the bar's Ethernet port. VB1 only.",
            },
            "set_device_name": {
                "label": "Set Device Name",
                "params": {
                    "name": {
                        "type": "string",
                        "required": True,
                        "label": "Name",
                        "pattern": "^.{1,22}$",
                        "help": "1 to 22 characters.",
                    },
                },
                # The bar restarts to take a new name. Not measured here: an
                # integration on firmware 1.5.1 waits 70 s before polling again.
                "restarts_device_for": 90,
                "help": "Renames the bar. The bar restarts to take the new name.",
            },
            "reboot": {
                "label": "Reboot",
                "params": {},
                # Not measured here: an integration on firmware 1.5.1 waits
                # 70 s after a reboot before polling again.
                "restarts_device_for": 90,
                "help": "Restarts the bar.",
            },
        },
        "quick_actions": [
            "mic_mute_toggle", "speaker_mute_toggle", "volume_up", "volume_down", "autoframing_toggle",
        ],
        "actions": [
            {"id": "mic_mute_toggle", "kind": "command", "icon": "mic-off"},
            {"id": "speaker_mute_toggle", "kind": "command", "icon": "volume-x"},
            {"id": "volume_up", "kind": "command", "icon": "volume-2"},
            {"id": "volume_down", "kind": "command", "icon": "volume-1"},
            {"id": "autoframing_toggle", "kind": "command", "icon": "scan-face"},
        ],
    }

    _client: httpx.AsyncClient | None = None

    def __init__(self, device_id: str, config: dict[str, Any], state: Any, events: Any) -> None:
        super().__init__(device_id, config, state, events)
        self._client = None
        self._lock = asyncio.Lock()
        self._slow_at = 0.0
        # The model family the bar reported; "" until it has.
        self._reported_family = ""
        # Properties this bar answered "invalid property"; not asked again on
        # this connection.
        self._unsupported: set[str] = set()
        self._narrow()

    # ── Config accessors ──

    @property
    def _host(self) -> str:
        return str(self.config.get("host", "") or "").strip()

    @property
    def _port(self) -> int:
        try:
            return int(self.config.get("port") or 443)
        except (TypeError, ValueError):
            return 443

    @property
    def _username(self) -> str:
        return str(self.config.get("username", "") or "")

    @property
    def _password(self) -> str:
        return str(self.config.get("password", "") or "")

    def _model_family(self) -> str:
        """The model the bar reported, else the model picked in its config."""
        if self._reported_family:
            return self._reported_family
        return "VB-S" if str(self.config.get("model", "")).strip().upper() == "VB-S" else "VB1"

    def _is_vb1(self) -> bool:
        return self._model_family() == "VB1"

    def _auth_fault(self, reason: str = "") -> ConnectionFaultError:
        if not self._username or not self._password:
            message = "The bar's REST API needs an API username and password, and one of them is blank."
        elif reason == ACCESS_DENIED:
            message = "The bar refused the API username and password (access denied)."
        else:
            message = "The bar refused the API username and password."
        return ConnectionFaultError(message, code="auth_failed")

    # ── Connection lifecycle ──

    async def _create_transport(self, transport_type: str) -> None:
        if not self._host:
            raise ConnectionFaultError("No IP address is entered.", code="invalid_config")
        if not self._username or not self._password:
            # Never send a sign-in that cannot succeed: the API takes a
            # username and password and has no blank default.
            raise self._auth_fault()
        if not await self._verify_reachable(self._host, self._port):
            raise ConnectionError(f"{self._host}:{self._port} is not responding")
        self._client = httpx.AsyncClient(
            base_url=f"https://{self._host}:{self._port}",
            auth=httpx.BasicAuth(self._username, self._password),
            verify=_as_bool(self.config.get("verify_ssl", False)),
            timeout=httpx.Timeout(8.0, connect=5.0),
            follow_redirects=False,
        )

    async def _post_connect(self) -> None:
        self._unsupported = set()
        self._slow_at = 0.0
        try:
            entries = await self._request("GET", query=IDENTITY)
        except VideobarError as exc:
            if exc.http_status == 403:
                raise self._auth_fault() from exc
            if 300 <= exc.http_status < 400:
                raise ConnectionFaultError(
                    f"The bar redirected the request ({exc}). Check the HTTPS port.",
                    code="invalid_config",
                ) from exc
            if exc.http_status == 404:
                raise ConnectionFaultError(
                    "The address answered, but not with the Videobar REST API (HTTP 404). "
                    "Turn on API access under Network > API on the bar.",
                    code="invalid_config",
                ) from exc
            raise ConnectionError(f"The bar answered the identity read with {exc}") from exc
        codes = [_code(entry) for entry in entries.values() if not _succeeded(entry)]
        if codes and len(codes) == len(entries) and all(code == ACCESS_DENIED for code in codes):
            raise self._auth_fault(ACCESS_DENIED)
        self._apply_entries(IDENTITY, entries)
        log.info(
            f"[{self.device_id}] Connected to Bose {self.get_state('model') or 'Videobar'} "
            f"{self.get_state('serial_number') or ''} at {self._host}:{self._port} "
            f"(firmware {self.get_state('firmware_version') or 'unknown'})"
        )

    async def _initial_sync(self) -> None:
        await self.poll()

    def _link_alive(self) -> bool:
        return self._client is not None

    async def _close_session(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _liveness_probe(self) -> None:
        try:
            await self._request("GET", query=["D6"])
        except VideobarError:
            return  # it answered: the link is up

    # ── HTTP plumbing ──

    async def _request(
        self,
        method: str,
        *,
        query: list[str] | None = None,
        values: dict[str, str] | None = None,
    ) -> dict[str, dict]:
        """One request, one at a time. Returns the answer's per-property
        entries by normalized id. A 401 raises the typed ``auth_failed``; an
        answer that is not the API's JSON raises VideobarError; a dead link
        raises ConnectionError."""
        client = self._client
        if client is None:
            raise ConnectionError("Not connected")
        # The ids go in the URL as the guide writes them, comma-separated.
        url = "/api?query=" + ",".join(query) if query else "/api"
        kwargs: dict[str, Any] = {}
        if values is not None:
            kwargs["data"] = {"data": json.dumps(values, separators=(",", ":"))}
        async with self._lock:
            try:
                resp = await client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                raise ConnectionError(f"{self._host} is not responding: {exc}") from exc
        if resp.status_code == 401:
            raise self._auth_fault()
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        entries = _entries(payload)
        if entries:
            return entries
        if 300 <= resp.status_code < 400:
            where = resp.headers.get("location", "")
            raise VideobarError(f"HTTP {resp.status_code} to {where}".strip(), http_status=resp.status_code)
        if resp.status_code >= 400:
            raise VideobarError(f"HTTP {resp.status_code}", http_status=resp.status_code)
        raise VideobarError("an answer that is not the API's JSON", http_status=resp.status_code)

    # ── Reads ──

    def _apply(self, pid: str, value: Any) -> None:
        spec = PROPERTIES_BY_ID.get(_norm(pid))
        if spec is None:
            return
        key, kind = spec
        text = str(value).strip()
        if kind == "bool":
            if text.lower() not in ("1", "0", "true", "false"):
                return
            self.set_state(key, _as_bool(text))
        elif kind == "int":
            try:
                self.set_state(key, int(text))
            except ValueError:
                log.warning(f"[{self.device_id}] {key}: not a number: {text!r}")
        elif kind == "preset":
            name = PRESET_NAMES.get(text)
            if name is not None:
                self.set_state(key, name)
        else:
            self.set_state(key, text)
        if key == "model":
            self._model_reported(text)

    def _model_reported(self, model: str) -> None:
        family = _family_of(model)
        if not family:
            return
        self.set_state("model_family", family)
        if family != self._reported_family:
            self._reported_family = family
            self._narrow()

    def _apply_entries(self, ids: list[str], entries: dict[str, dict]) -> dict[str, str]:
        """Write each answered property to state. Returns {id: why} for what
        was not answered. An id answered "invalid property" is not on this
        bar: it is not asked again on this connection and not reported."""
        refused: dict[str, str] = {}
        for pid in ids:
            entry = entries.get(_norm(pid))
            if entry is None:
                refused[pid] = "no answer"
                continue
            if _succeeded(entry) and "value" in entry:
                self._apply(pid, entry["value"])
                continue
            code = _code(entry)
            if code == INVALID_PROPERTY:
                if pid not in self._unsupported:
                    log.info(f"[{self.device_id}] This bar has no property {pid}; not asked again")
                self._unsupported.add(pid)
                continue
            refused[pid] = CODES.get(code, code or "refused")
        return refused

    def _wanted(self, ids: list[str], vb1_ids: list[str]) -> list[str]:
        wanted = ids + (vb1_ids if self._is_vb1() else [])
        return [pid for pid in wanted if pid not in self._unsupported]

    async def _read(self, ids: list[str]) -> dict[str, str]:
        ids = [pid for pid in ids if pid not in self._unsupported]
        if not ids:
            return {}
        entries = await self._request("GET", query=ids)
        return self._apply_entries(ids, entries)

    async def _read_back(self, ids: list[str]) -> None:
        """Read what a command changed. What the bar does not answer here is
        left to the next poll."""
        try:
            await self._read(ids)
        except VideobarError as exc:
            log.debug(f"[{self.device_id}] Read-back not answered: {exc}")

    def _label(self, pid: str) -> str:
        spec = PROPERTIES_BY_ID.get(_norm(pid))
        if spec is None:
            return pid
        info = self.DRIVER_INFO["state_variables"].get(spec[0]) or {}
        return str(info.get("label") or spec[0])

    async def poll(self) -> None:
        """Mute, volume, the call states and the camera every poll; the rest
        every 30 s, in the same request. A dead link raises (the platform
        counts it); a value the bar refuses is named in last_error, written
        again by every poll while it holds."""
        if self._client is None:
            return
        now = time.monotonic()
        slow = now - self._slow_at >= SLOW_INTERVAL_S
        ids = self._wanted(FAST, FAST_VB1)
        if slow:
            ids += self._wanted(SLOW, SLOW_VB1)
        try:
            refused = await self._read(ids)
        except VideobarError as exc:
            raise ConnectionError(f"The bar answered the status read with {exc}") from exc
        if slow:
            self._slow_at = now
        if not refused:
            return
        reasons = set(refused.values())
        if len(refused) == len(ids) and len(reasons) == 1:
            message = f"The bar did not answer any value it was asked for ({reasons.pop()})."
        else:
            what = ", ".join(f"{self._label(pid)} ({why})" for pid, why in refused.items())
            message = f"The bar did not answer {what}; those values are not updated."
        self.set_state("last_error", message)

    # ── Model narrowing ──

    def _narrow(self) -> None:
        """Offer the VB1's own commands only on a VB1."""
        base = type(self).DRIVER_INFO
        if self._is_vb1():
            self.DRIVER_INFO = base
            return
        commands = copy.deepcopy(base["commands"])
        for name in VB1_COMMANDS:
            commands.pop(name, None)
        self.DRIVER_INFO = {**base, "commands": commands}

    # ── Commands ──

    async def _write(
        self,
        what: str,
        method: str,
        values: dict[str, str],
        read_back: list[str] | None = None,
    ) -> None:
        """Send a PUT (values) or POST (actions, empty values) and check every
        property's code. Then read back what it changed."""
        try:
            entries = await self._request(method, values=values)
        except VideobarError as exc:
            raise CommandRefusedError(f"The bar refused to {what} ({exc}).") from exc
        for pid in values:
            entry = entries.get(_norm(pid))
            if entry is None:
                raise CommandRefusedError(f"The bar did not answer the request to {what}.")
            code = _code(entry)
            if code in ACCEPTED or (not code and _succeeded(entry)):
                continue
            reason = CODES.get(code, code or "refused")
            raise CommandRefusedError(f"The bar refused to {what}: {reason} ({code}).")
        if read_back:
            await self._read_back(read_back)

    async def _toggle(self, what: str, pid: str, state_key: str) -> None:
        """Set a 1|0 property to the opposite of what the bar reports now."""
        try:
            await self._read([pid])
        except VideobarError as exc:
            raise CommandRefusedError(f"The bar did not report its {what} state, so nothing was sent ({exc}).") from exc
        current = self.get_state(state_key)
        if current is None:
            raise CommandRefusedError(f"The bar did not report its {what} state, so nothing was sent.")
        await self._write(f"turn {what} {'off' if current else 'on'}", "PUT", {pid: "0" if current else "1"}, [pid])

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        params = params or {}

        if command in VB1_COMMANDS and not self._is_vb1():
            raise CommandRefusedError("This command is on the VB1 only.")

        if command == "mic_mute":
            await self._write("mute the microphones", "PUT", {"2": "1"}, ["2"])
        elif command == "mic_unmute":
            await self._write("unmute the microphones", "PUT", {"2": "0"}, ["2"])
        elif command == "mic_mute_toggle":
            await self._write("toggle the microphone mute", "POST", {"15": ""}, ["2"])
        elif command == "speaker_mute":
            await self._write("mute the loudspeaker", "PUT", {"33": "1"}, ["33"])
        elif command == "speaker_unmute":
            await self._write("unmute the loudspeaker", "PUT", {"33": "0"}, ["33"])
        elif command == "speaker_mute_toggle":
            await self._write("toggle the loudspeaker mute", "POST", {"34": ""}, ["33"])
        elif command == "set_volume":
            level = int(params["level"])
            await self._write("set the volume", "PUT", {"3": str(level)}, AUDIO)
        elif command == "volume_up":
            await self._write("turn the volume up", "POST", {"4": ""}, AUDIO)
        elif command == "volume_down":
            await self._write("turn the volume down", "POST", {"5": ""}, AUDIO)
        elif command == "recall_preset":
            preset = str(params.get("preset", "")).strip()
            if preset not in PRESET_NUMBERS:
                raise ValueError(f"Unknown preset: {preset}")
            label = PRESET_LABELS[preset]
            # camera.activePreset picks the preset; camera.applyActivePreset
            # moves the camera to it.
            await self._write(f"select {label}", "PUT", {"13": PRESET_NUMBERS[preset]})
            await self._write(f"move the camera to {label}", "POST", {"0F": ""}, ["13", *CAMERA])
        elif command == "save_preset":
            preset = str(params.get("preset", "")).strip()
            if preset not in PRESET_SAVE:
                raise ValueError(f"Unknown preset: {preset}")
            await self._write(
                f"save {PRESET_LABELS[preset]}", "POST", {PRESET_SAVE[preset]: ""}, [PRESET_POSITION[preset]],
            )
        elif command == "set_camera_position":
            values = {
                "7": str(int(params["pan"])),
                "8": str(int(params["tilt"])),
                "6": str(int(params["zoom"])),
            }
            await self._write("move the camera", "PUT", values, CAMERA)
        elif command == "set_camera_pan":
            await self._write("pan the camera", "PUT", {"7": str(int(params["pan"]))}, CAMERA)
        elif command == "set_camera_tilt":
            await self._write("tilt the camera", "PUT", {"8": str(int(params["tilt"]))}, CAMERA)
        elif command == "set_camera_zoom":
            await self._write("zoom the camera", "PUT", {"6": str(int(params["zoom"]))}, CAMERA)
        elif command == "camera_pan_left":
            await self._write("pan the camera left", "POST", {"0B": ""}, CAMERA)
        elif command == "camera_pan_right":
            await self._write("pan the camera right", "POST", {"0C": ""}, CAMERA)
        elif command == "camera_tilt_up":
            await self._write("tilt the camera up", "POST", {"0D": ""}, CAMERA)
        elif command == "camera_tilt_down":
            await self._write("tilt the camera down", "POST", {"0E": ""}, CAMERA)
        elif command == "camera_zoom_in":
            await self._write("zoom the camera in", "POST", {"9": ""}, CAMERA)
        elif command == "camera_zoom_out":
            await self._write("zoom the camera out", "POST", {"0A": ""}, CAMERA)
        elif command == "autoframing_on":
            await self._write("turn autoframing on", "PUT", {"19": "1"}, ["19"])
        elif command == "autoframing_off":
            await self._write("turn autoframing off", "PUT", {"19": "0"}, ["19", *CAMERA])
        elif command == "autoframing_toggle":
            await self._toggle("autoframing", "19", "autoframing")
        elif command == "bluetooth_pairing_on":
            await self._write("turn Bluetooth pairing on", "PUT", {"14": "1"}, ["14"])
        elif command == "bluetooth_pairing_off":
            await self._write("turn Bluetooth pairing off", "PUT", {"14": "0"}, ["14"])
        elif command == "bluetooth_pairing_toggle":
            await self._write("toggle Bluetooth pairing", "POST", {"C6": ""}, ["14"])
        elif command == "bluetooth_disconnect":
            # The guide lists 1, 2 and 3 for bluetooth.disconnect without
            # saying what each does; 1 disconnects the connected device.
            await self._write("disconnect the Bluetooth device", "POST", {"E4": "1"}, ["6B", "C2", "6C"])
        elif command == "bluetooth_on":
            await self._write("turn Bluetooth on", "PUT", {"3A": "1"}, ["3A", "67"])
        elif command == "bluetooth_off":
            await self._write("turn Bluetooth off", "PUT", {"3A": "0"}, ["3A", "67", "6B"])
        elif command == "hdmi_on":
            await self._write("turn HDMI on", "PUT", {"C9": "1"}, ["C9"])
        elif command == "hdmi_off":
            await self._write("turn HDMI off", "PUT", {"C9": "0"}, ["C9"])
        elif command == "wifi_on":
            await self._write("turn Wi-Fi on", "PUT", {"3B": "1"}, ["3B"])
        elif command == "wifi_off":
            # No read-back: the bar may no longer be reachable.
            await self._write("turn Wi-Fi off", "PUT", {"3B": "0"})
            self._slow_at = 0.0
        elif command == "ethernet_on":
            await self._write("turn Ethernet on", "PUT", {"38": "1"}, ["38"])
        elif command == "ethernet_off":
            # No read-back: the bar may no longer be reachable.
            await self._write("turn Ethernet off", "PUT", {"38": "0"})
            self._slow_at = 0.0
        elif command == "set_device_name":
            name = str(params.get("name", ""))
            if not 1 <= len(name) <= 22:
                raise ValueError("A name is 1 to 22 characters.")
            # No read-back: the bar restarts to take the new name.
            await self._write("rename the bar", "PUT", {"25": name})
            self._slow_at = 0.0
        elif command == "reboot":
            await self._write("reboot", "POST", {"32": ""})
        else:
            raise ValueError(f"Unknown command: {command}")
        return None

    # ── Device settings ──

    async def set_device_setting(self, key: str, value: Any) -> Any:
        pid = TEXT_SETTINGS.get(key)
        if pid is None:
            raise ValueError(f"Unknown device setting: {key}")
        text = "" if value is None else str(value)
        if len(text) > TEXT_SETTING_MAX:
            raise ValueError(f"{self._label(pid)} is at most {TEXT_SETTING_MAX} characters.")
        await self._write(f"set {self._label(pid)}", "PUT", {pid: text}, [pid])
