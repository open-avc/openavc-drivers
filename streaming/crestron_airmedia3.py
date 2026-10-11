"""
OpenAVC Crestron AirMedia Series 3 receiver driver.

Controls the AirMedia Series 3 wireless presentation receivers (AM-3000-WF,
AM-3100-WF, AM-3200, AM-3200-WF, their -I versions and the AM-3200-GV) over
the receiver's CresNext API: JSON objects over HTTPS on port 443, behind the
same cookie sign-in as Crestron's other CresNext devices.

What it reads: who is connected and presenting, the login code, the source on
the display, the HDMI output and (AM-3200) input, the USB conferencing
peripherals and their volume, the paired AirMedia Connect adaptors (one child
each), the occupancy sensor, and the receiver's identity. What it does:
disconnect every user, turn the HDMI output off and on, set and mute the USB
speaker volume, start adaptor pairing, restart the receiver, and change the
presentation settings (login code, system mode, routing mode, Miracast,
Canvas, discovery, the inactivity timeout, wireless conferencing).

Push: the receiver reports changes on a long poll (``GET /Device/Longpoll``
answers with only the properties that changed, or with a timeout, and is asked
again at once). The driver holds one open for the whole session and polls
every object on ``poll_interval`` as well, which reads everything at connect,
catches anything a long poll could not carry, and is how a receiver that
stopped answering is noticed. A change inside a dictionary (connected users,
the adaptor list, the source list, the occupancy sensors) re-reads that object
rather than reconciling a partial dictionary.

Routing: the receiver routes on its own, by its Routing Mode (a device
setting here). The API has no request that puts one chosen source on the
display, so the driver has no route command.

Why Python (not YAML): the session is a cookie sign-in (GET /userlogin.html
for TRACKID, a form POST of the credentials, six HTTPOnly cookies, one of
which changes on every answer) plus an anti-forgery token every POST must
carry; the push channel is a long poll none of the declarative ``push:``
shapes covers; and a nested JSON tree fans out into flat state, children and
re-reads, with the surface narrowed to the model the receiver reports.

Source of truth: "API for AirMedia Series 3 Receivers"
(https://sdkcon78221.crestron.com/sdk/AM3K-API/index.htm, current as of
firmware 3.0800.0157) and the AirMedia Series 3 Product Manual, Doc. 9020AC
(crestron.com/docs/9020) for the model feature table and first sign-in.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.utils.logger import get_logger

log = get_logger(__name__)


# ── Paths (the API Reference topics) ──

_SIGN_IN = "/userlogin.html"
_DEVICE_INFO = "/Device/DeviceInfo"
_AIRMEDIA = "/Device/AirMedia"
_CLIENT_DATA = "/Device/AirMedia/ClientData"
_CONFERENCING = "/Device/AirMedia/WirelessConferencing"
_ADAPTORS = "/Device/AirMediaTx3"
_SOURCES = "/Device/SourceSelectionConfiguration"
_AVIO = "/Device/AvioV2"
_OCCUPANCY = "/Device/App/Config/OccupancySensor"
_LONGPOLL = "/Device/Longpoll"

# A sign-in or a request refused. The Authentication topic answers invalid
# credentials and a timed-out session with 403 Forbidden; 401 is read the same.
# A DM-NVX-D30 on the same web server (firmware 7.0.5057) answered a request
# without a live session with a redirect to the sign-in page instead (301,
# Location /userlogin.html), which _session_refused reads as the same refusal.
_REFUSED = (401, 403)

# The Results object's StatusId: below 0 is an error; 3 is "the property is
# unsupported", which applied nothing either.
_UNSUPPORTED = 3

# How long one long poll may stay open before the driver asks again. The API
# does not say how long the receiver holds one; a receiver that answers its
# own timeout sooner is asked again at once.
_LONGPOLL_HOLD_S = 120.0

# A dictionary that changes often (an adaptor's signal strength) is read again
# at most this often.
_REREAD_GAP_S = 1.0

# ── Models (Product Manual 9020AC, the feature table on pages 7-9) ──

# DeviceInfo.Model names the model; an -I (international) unit has the same
# features as its US model, and the AM-3200-GV reports itself as an AM-3200
# (manual, Configuration).
_FAMILIES = ("AM-3000-WF", "AM-3100-WF", "AM-3200", "AM-3200-WF")
_HAS_CONFERENCING = frozenset({"AM-3100-WF", "AM-3200", "AM-3200-WF"})
_HAS_HDMI_INPUT = frozenset({"AM-3200", "AM-3200-WF"})
_HAS_ADAPTORS = frozenset({"AM-3000-WF", "AM-3100-WF", "AM-3200-WF"})

_CONFERENCING_COMMANDS = frozenset({
    "set_peripheral_volume", "mute_peripheral", "unmute_peripheral",
})
_CONFERENCING_SETTINGS = frozenset({"conferencing_enabled"})
_CONFERENCING_STATES = frozenset({
    "conferencing_enabled", "conferencing_status", "peripheral_volume",
    "peripheral_mute", "peripheral_volume_supported", "peripherals_blocked",
    "peripherals_blocked_reason", "mic_detected", "mic_in_use", "mic_model",
    "speaker_detected", "speaker_in_use", "speaker_model", "camera_detected",
    "camera_in_use", "camera_model",
})
_HDMI_INPUT_STATES = frozenset({"hdmi_input_sync", "hdmi_input_resolution"})
_ADAPTOR_COMMANDS = frozenset({"start_adaptor_pairing"})
_ADAPTOR_STATES = frozenset({"adaptor_pairing_status", "paired_adaptors"})

_SIGN_IN_REFUSED = "The AirMedia refused the username and password."


def _session_refused(resp: httpx.Response) -> bool:
    """A refused session: 401 or 403, or a redirect to the sign-in page."""
    if resp.status_code in _REFUSED:
        return True
    return resp.is_redirect and "userlogin" in resp.headers.get("location", "").lower()


def _model_family(reported: Any) -> str:
    """The model family a reported DeviceInfo.Model belongs to, or ""."""
    model = str(reported or "").strip().upper()
    if model.endswith("-I"):
        model = model[:-2]
    if model == "AM-3200-GV":
        model = "AM-3200"
    return model if model in _FAMILIES else ""


def _first_key(mapping: Any) -> str | None:
    if isinstance(mapping, dict) and mapping:
        return sorted(mapping)[0]
    return None


def _resolution(port: dict[str, Any]) -> str | None:
    """A port's resolution as the receiver states it, else built from its
    horizontal, vertical and frame-rate fields. None when the port said
    nothing about it."""
    current = port.get("CurrentResolution")
    if isinstance(current, str) and current.strip():
        return current.strip()
    h, v = port.get("HorizontalResolution"), port.get("VerticalResolution")
    if h is None and v is None:
        return None
    if not h or not v:
        return "No Signal"
    fps = port.get("FramesPerSecond")
    return f"{h}x{v}@{fps}" if fps else f"{h}x{v}"


class CrestronAirMedia3Driver(BaseDriver):
    """Crestron AirMedia Series 3 receiver (CresNext API over HTTPS)."""

    DRIVER_INFO = {
        "id": "crestron_airmedia3",
        "name": "Crestron AirMedia Series 3",
        "manufacturer": "Crestron",
        "category": "streaming",
        "version": "1.0.1",
        "author": "OpenAVC",
        "min_platform_version": "0.37.0",
        "description": (
            "Crestron AirMedia Series 3 wireless presentation receivers over "
            "the receiver's API: who is connected and presenting, the login "
            "code, the source on the display, the HDMI output, the USB "
            "conferencing peripherals and their volume, the paired AirMedia "
            "Connect adaptors, and the presentation settings. Changes arrive "
            "as the receiver reports them."
        ),
        "source_url": "https://sdkcon78221.crestron.com/sdk/AM3K-API/index.htm",
        "tags": [
            "crestron", "airmedia", "wireless-presentation", "byod",
            "wireless-conferencing", "rest-api",
        ],
        "verified": False,
        "simulated": True,
        "web_ui": True,
        "protocols": ["cresnext"],
        "ports": [443],
        "transport": "http",
        "help": {
            "overview": (
                "Controls a Crestron AirMedia Series 3 receiver: see who is "
                "connected and presenting, show the login code on a panel, "
                "disconnect everyone at the end of a meeting, turn the HDMI "
                "output off and on, set and mute the USB speaker used for "
                "wireless conferencing, and follow the paired AirMedia Connect "
                "adaptors. The receiver chooses what is on the display by its "
                "Routing Mode."
            ),
            "setup": (
                "1. Sign in to the receiver's web page once (https:// and its "
                "IP address, shown on the display). A new receiver asks for a "
                "new administrator username and password there.\n"
                "2. Enter that username and password here.\n"
                "3. Leave Verify TLS Certificate off unless a trusted "
                "certificate is installed on the receiver."
            ),
            "connection": (
                "Check the receiver's IP address on its welcome screen, and "
                "that the username and password are an administrator account "
                "on the receiver."
            ),
        },
        "discovery": {
            # Crestron's own IEEE blocks (MA-L, IEEE registry). The API
            # documents no identifying answer to ask before sign-in, so these
            # hints are all a scan has.
            "oui": ["c4:42:68", "00:10:7f"],
            "manufacturer_alias": ["crestron", "crestron electronics"],
        },
        "compatible_models": [
            {
                "manufacturer": "Crestron",
                "models": [
                    "AM-3000-WF", "AM-3000-WF-I", "AM-3100-WF", "AM-3100-WF-I",
                    "AM-3200", "AM-3200-WF", "AM-3200-WF-I", "AM-3200-GV",
                ],
                "confidence": "untested",
                "notes": (
                    "Built from the API for AirMedia Series 3 Receivers "
                    "(firmware 3.0800.0157) and tested against the simulator. "
                    "The commands and readings a model lacks are left off: no "
                    "wireless conferencing on the AM-3000-WF, no HDMI input on "
                    "the AM-3000-WF and AM-3100-WF, no AirMedia Connect "
                    "adaptors on the AM-3200 and AM-3200-GV."
                ),
            },
        ],
        "default_config": {
            "host": "",
            "port": 443,
            "verify_ssl": False,
            "username": "admin",
            "password": "",
            "model": "",
            "poll_interval": 30,
        },
        "config_schema": {
            "host": {
                "type": "string", "required": True, "label": "IP Address",
                "help": "The receiver's IP address, shown on its welcome screen.",
            },
            "port": {
                "type": "integer", "default": 443, "label": "Port",
                "min": 1, "max": 65535, "advanced": True,
            },
            "verify_ssl": {
                "type": "boolean", "default": False,
                "label": "Verify TLS Certificate", "advanced": True,
                "help": (
                    "The receiver ships with a self-signed certificate. Turn "
                    "this on only after installing a trusted one."
                ),
            },
            "username": {
                "type": "string", "default": "admin", "label": "Username",
                "help": "An administrator account on the receiver.",
            },
            "password": {
                "type": "string", "default": "", "label": "Password",
                "secret": True,
            },
            "model": {
                "type": "enum", "default": "", "label": "Model",
                "learned_from": "model_family",
                "values": [
                    {"value": "", "label": "Detect automatically"},
                    {"value": "AM-3000-WF", "label": "AM-3000-WF or AM-3000-WF-I"},
                    {"value": "AM-3100-WF", "label": "AM-3100-WF or AM-3100-WF-I"},
                    {"value": "AM-3200", "label": "AM-3200 or AM-3200-GV"},
                    {"value": "AM-3200-WF", "label": "AM-3200-WF or AM-3200-WF-I"},
                ],
                "help": (
                    "Which commands and readings the device offers before the "
                    "receiver is reached (and in simulation). The model the "
                    "receiver reports replaces it."
                ),
            },
            "poll_interval": {
                "type": "integer", "default": 30, "min": 5, "max": 600,
                "label": "Poll Interval (sec)",
                "help": (
                    "How often every reading is asked for again. Changes "
                    "arrive as they happen between polls."
                ),
            },
        },
        "state_variables": {
            # ── Identity ──
            "model": {"type": "string", "label": "Model"},
            "model_family": {
                "type": "string", "label": "Model Family",
                "help": "The model without its -I suffix; an AM-3200-GV reads AM-3200.",
            },
            "serial_number": {"type": "string", "label": "Serial Number"},
            "firmware": {"type": "string", "label": "Firmware Version"},
            "device_name": {"type": "string", "label": "Device Name"},
            "mac_address": {"type": "string", "label": "MAC Address"},
            "reboot_reason": {
                "type": "string", "label": "Last Restart Reason",
                "help": "poweron, watchdog, manual, firmware or unknown.",
            },
            # ── Presentation ──
            "presentation_status": {
                "type": "enum", "values": ["Idle", "Active", "Presenting"],
                "label": "Presentation Status",
                "help": (
                    "Idle: nobody connected. Active: users connected, nobody "
                    "presenting. Presenting: at least one user presenting."
                ),
            },
            "connected_users": {"type": "integer", "label": "Connected Users", "min": 0},
            "presenting_users": {"type": "integer", "label": "Presenting Users", "min": 0},
            "wifi_users_connected": {
                "type": "boolean", "label": "Users on the Receiver's Wi-Fi",
            },
            "active_sources": {
                "type": "string", "label": "Sources on the Display",
                "help": "The names of the sources showing now, separated by commas; empty when none.",
            },
            "login_code": {
                "type": "string", "label": "Login Code",
                "help": (
                    "The code a user enters to connect, as the receiver reports "
                    "it when the login code mode is Fixed."
                ),
            },
            "login_code_mode": {
                "type": "enum", "values": ["Disabled", "Random", "Random8", "Fixed"],
                "label": "Login Code Mode",
            },
            "active_login_code_mode": {
                "type": "enum", "values": ["Disabled", "Random", "Random8", "Fixed"],
                "label": "Login Code Mode in Use",
                "help": "Follows a new login code mode once every user has disconnected.",
            },
            "fixed_login_code": {"type": "string", "label": "Fixed Login Code"},
            "show_login_code": {"type": "boolean", "label": "Show Login Code"},
            "system_mode": {
                "type": "enum",
                "values": [
                    "OptimizedForVideoQuality", "OptimizedForMultiplePresentations",
                    "Custom",
                ],
                "label": "System Mode",
            },
            "routing_mode": {
                "type": "enum",
                "values": ["Auto", "AirMediaOnly", "AutoWithPriority", "Wired"],
                "label": "Routing Mode",
            },
            "miracast_enabled": {"type": "boolean", "label": "Miracast"},
            "canvas_enabled": {"type": "boolean", "label": "AirMedia Canvas"},
            "airmedia_discovery": {"type": "boolean", "label": "AirMedia Discovery"},
            "inactivity_timeout": {
                "type": "integer", "label": "Inactivity Timeout",
                "min": 1, "max": 60, "unit": "min",
            },
            # ── Video ──
            "display_connected": {"type": "boolean", "label": "Display Connected"},
            "output_resolution": {
                "type": "string", "label": "Output Resolution", "cloud_priority": "low",
            },
            "output_disabled": {
                "type": "boolean", "label": "HDMI Output Off", "control": True,
            },
            "hdmi_input_sync": {"type": "boolean", "label": "HDMI Input Signal"},
            "hdmi_input_resolution": {
                "type": "string", "label": "HDMI Input Resolution", "cloud_priority": "low",
            },
            # ── Wireless conferencing ──
            "conferencing_enabled": {"type": "boolean", "label": "Wireless Conferencing"},
            "conferencing_status": {
                "type": "enum", "values": ["In Use", "Available", "Unavailable"],
                "label": "Conferencing Status",
            },
            "peripheral_volume": {
                "type": "integer", "label": "USB Speaker Volume",
                "min": 0, "max": 100, "unit": "%", "control": True,
            },
            "peripheral_mute": {
                "type": "boolean", "label": "USB Speaker Mute", "control": True,
            },
            "peripheral_volume_supported": {
                "type": "boolean", "label": "USB Speaker Volume Adjustable",
            },
            "peripherals_blocked": {"type": "boolean", "label": "Peripherals Blocked"},
            "peripherals_blocked_reason": {
                "type": "string", "label": "Peripherals Blocked Because",
            },
            "mic_detected": {"type": "boolean", "label": "Microphone Detected"},
            "mic_in_use": {"type": "boolean", "label": "Microphone In Use"},
            "mic_model": {"type": "string", "label": "Microphone Model"},
            "speaker_detected": {"type": "boolean", "label": "Speaker Detected"},
            "speaker_in_use": {"type": "boolean", "label": "Speaker In Use"},
            "speaker_model": {"type": "string", "label": "Speaker Model"},
            "camera_detected": {"type": "boolean", "label": "Camera Detected"},
            "camera_in_use": {"type": "boolean", "label": "Camera In Use"},
            "camera_model": {"type": "string", "label": "Camera Model"},
            # ── AirMedia Connect adaptors ──
            "adaptor_pairing_status": {
                "type": "enum", "values": ["Not_Started", "In_Progress", "Max_Reached"],
                "label": "Adaptor Pairing",
            },
            "paired_adaptors": {"type": "integer", "label": "Paired Adaptors", "min": 0},
            # ── Occupancy ──
            "occupancy": {
                "type": "string", "label": "Occupancy",
                "help": (
                    "What the first enabled occupancy sensor connected to the "
                    "receiver reports; empty when none is set up."
                ),
            },
            "last_error": {"type": "string", "label": "Last Error"},
        },
        "child_entity_types": {
            "adaptor": {
                "label": "AirMedia Connect Adaptor",
                "label_plural": "AirMedia Connect Adaptors",
                "id_format": {"type": "string", "max_length": 64},
                "state_variables": {
                    "label": {"type": "string", "label": "Name"},
                    "model": {"type": "string", "label": "Model"},
                    "serial_number": {"type": "string", "label": "Serial Number"},
                    "firmware": {"type": "string", "label": "Firmware Version"},
                    "mac_address": {"type": "string", "label": "MAC Address"},
                    "connection_status": {
                        "type": "string", "label": "Connection",
                        "help": (
                            "Online, Online_Sharing, Online_Conferencing, "
                            "Online_Sharing_Conferencing, Online_Updating, "
                            "Offline or Unknown."
                        ),
                    },
                    "screen_share_status": {
                        "type": "enum", "values": ["Unknown", "Stopped", "Paused", "Playing"],
                        "label": "Screen Share",
                    },
                    "source_connection_status": {
                        "type": "string", "label": "Laptop Connection",
                        "help": "ReadyToShare, UnabledToShare or NotConnected.",
                    },
                    "signal_strength": {
                        "type": "integer", "label": "Signal Strength",
                        "min": 0, "max": 100, "unit": "%", "cloud_priority": "low",
                    },
                },
                "summary_fields": [
                    "label", "connection_status", "screen_share_status", "signal_strength",
                ],
                "label_field": "label",
            },
        },
        "device_settings": {
            "login_code_mode": {
                "type": "enum", "label": "Login Code Mode",
                "help": "Whether users enter a code to connect, and which kind.",
                "values": [
                    {"value": "Disabled", "label": "No code"},
                    {"value": "Random", "label": "Random 4-digit code"},
                    {"value": "Random8", "label": "Random 8-digit code"},
                    {"value": "Fixed", "label": "Fixed code"},
                ],
                "state_key": "login_code_mode", "default": "Random", "setup": False,
            },
            "fixed_login_code": {
                "type": "string", "label": "Fixed Login Code",
                "help": (
                    "The eight-digit code users enter when the login code mode "
                    "is Fixed. A new code is used once every user has disconnected."
                ),
                "regex": "^[0-9]{8}$",
                "state_key": "fixed_login_code", "default": "", "setup": False,
            },
            "show_login_code": {
                "type": "boolean", "label": "Show Login Code",
                "help": "Shows the login code on the display.",
                "state_key": "show_login_code", "default": True, "setup": False,
            },
            "system_mode": {
                "type": "enum", "label": "System Mode",
                "help": (
                    "Optimized for Video Quality presents one source at a time; "
                    "Optimized for Multiple Presentations shows several at "
                    "once. Both fix Canvas and the presentation limit; Custom "
                    "leaves every setting free."
                ),
                "values": [
                    {"value": "OptimizedForVideoQuality", "label": "Optimized for Video Quality"},
                    {"value": "OptimizedForMultiplePresentations",
                     "label": "Optimized for Multiple Presentations"},
                    {"value": "Custom", "label": "Custom"},
                ],
                "state_key": "system_mode", "default": "OptimizedForMultiplePresentations",
                "setup": False,
            },
            "routing_mode": {
                "type": "enum", "label": "Routing Mode",
                "help": "Which sources the receiver puts on the display by itself when they connect.",
                "values": [
                    {"value": "Auto", "label": "Every source"},
                    {"value": "AirMediaOnly", "label": "AirMedia sources only"},
                    {"value": "AutoWithPriority",
                     "label": "Every source, back to the next by priority when one stops"},
                    {"value": "Wired", "label": "Every source except AirMedia"},
                ],
                "state_key": "routing_mode", "default": "Auto", "setup": False,
            },
            "miracast_enabled": {
                "type": "boolean", "label": "Miracast",
                "help": "Lets Windows laptops present with Miracast.",
                "state_key": "miracast_enabled", "default": True, "setup": False,
            },
            "canvas_enabled": {
                "type": "boolean", "label": "AirMedia Canvas",
                "help": (
                    "Shows up to four sources on the display at once. Set by "
                    "the system mode unless it is Custom."
                ),
                "state_key": "canvas_enabled", "default": True, "setup": False,
            },
            "airmedia_discovery": {
                "type": "boolean", "label": "AirMedia Discovery",
                "help": (
                    "Lets the AirMedia app find the receiver on the network. "
                    "Off, users type its IP address or host name."
                ),
                "state_key": "airmedia_discovery", "default": True, "setup": False,
            },
            "inactivity_timeout": {
                "type": "integer", "label": "Inactivity Timeout (min)",
                "help": "Disconnects a user who is connected but not presenting after this many minutes.",
                "min": 1, "max": 60,
                "state_key": "inactivity_timeout", "default": 45, "setup": False,
            },
            "conferencing_enabled": {
                "type": "boolean", "label": "Wireless Conferencing",
                "help": "Offers the USB camera, microphone and speaker to a presenting laptop's meeting.",
                "state_key": "conferencing_enabled", "default": True, "setup": False,
            },
        },
        "commands": {
            "disconnect_all_users": {
                "label": "Disconnect All Users",
                "help": "Disconnects every AirMedia user; what they were presenting leaves the display.",
            },
            "disable_output": {
                "label": "HDMI Output Off",
                "help": "Turns off the receiver's HDMI output; the display shows nothing from it.",
                "sets": {"output_disabled": True},
            },
            "enable_output": {
                "label": "HDMI Output On",
                "help": "Turns the receiver's HDMI output back on.",
                "sets": {"output_disabled": False},
            },
            "set_peripheral_volume": {
                "label": "Set USB Speaker Volume",
                "help": "Sets the volume of the USB speaker used for wireless conferencing.",
                "params": {
                    "level": {
                        "type": "integer", "required": True, "label": "Volume",
                        "min": 0, "max": 100, "unit": "%",
                    },
                },
                "sets": {"peripheral_volume": "{level}"},
            },
            "mute_peripheral": {
                "label": "Mute USB Speaker",
                "help": "Mutes the USB speaker used for wireless conferencing.",
                "sets": {"peripheral_mute": True},
            },
            "unmute_peripheral": {
                "label": "Unmute USB Speaker",
                "help": "Unmutes the USB speaker used for wireless conferencing.",
                "sets": {"peripheral_mute": False},
            },
            "start_adaptor_pairing": {
                "label": "Pair an AirMedia Connect Adaptor",
                "help": (
                    "Puts the receiver into pairing for an AirMedia Connect "
                    "adaptor; Adaptor Pairing reads In_Progress until it ends."
                ),
            },
            "reboot": {
                "label": "Restart Receiver",
                "help": "Restarts the receiver. Presentations end and it is offline until it starts again.",
                "confirm": "Restart the AirMedia receiver? Presentations end and it is offline until it starts again.",
            },
        },
        "actions": [
            {"id": "disconnect_all_users", "kind": "command", "icon": "user-x"},
            {"id": "disable_output", "kind": "command", "icon": "monitor-off"},
            {"id": "enable_output", "kind": "command", "icon": "monitor"},
            {"id": "mute_peripheral", "kind": "command", "icon": "volume-x"},
            {"id": "unmute_peripheral", "kind": "command", "icon": "volume-2"},
        ],
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._client: httpx.AsyncClient | None = None
        self._base_url = ""
        # The anti-forgery token the sign-in answer carries; every POST sends
        # it back (a POST without it ends the session).
        self._xsrf = ""
        # One sign-in again at a time; the count tells a request whose session
        # was refused whether someone has already signed in again since.
        self._sign_in_lock = asyncio.Lock()
        self._sign_ins = 0
        # Set once the receiver refuses a sign-in on this connection, so
        # nothing sends the refused password again.
        self._refused = ""
        self._longpoll_task: asyncio.Task | None = None
        self._reread_tasks: dict[str, asyncio.Task] = {}
        self._reread_wanted: set[str] = set()
        self._reread_at: dict[str, float] = {}
        # Objects this account may not read, named in last_error.
        self._denied: set[str] = set()
        # The AvioV2 keys of the HDMI output and input ports last read, so a
        # partial update and a write address the same port.
        self._output_keys: tuple[str, str] = ("Output1", "Port1")
        self._input_keys: tuple[str, str] | None = None
        self._family = ""
        picked = _model_family(self.config.get("model", ""))
        if picked:
            self._family = picked
            self._narrow()

    # ── Model narrowing ──

    def _narrow(self) -> None:
        """Offer only what this model has. An unidentified model keeps the
        full declaration."""
        base = type(self).DRIVER_INFO
        family = self._family
        if family not in _FAMILIES:
            self.DRIVER_INFO = base
            return
        drop_commands: set[str] = set()
        drop_settings: set[str] = set()
        drop_states: set[str] = set()
        drop_children: set[str] = set()
        if family not in _HAS_CONFERENCING:
            drop_commands |= _CONFERENCING_COMMANDS
            drop_settings |= _CONFERENCING_SETTINGS
            drop_states |= _CONFERENCING_STATES
        if family not in _HAS_HDMI_INPUT:
            drop_states |= _HDMI_INPUT_STATES
        if family not in _HAS_ADAPTORS:
            drop_commands |= _ADAPTOR_COMMANDS
            drop_states |= _ADAPTOR_STATES
            drop_children.add("adaptor")
            if self._has_adaptors():
                for local_id in list(self.list_children("adaptor")):
                    self.deregister_child("adaptor", local_id)
        self.DRIVER_INFO = {
            **base,
            "commands": {
                k: v for k, v in base["commands"].items() if k not in drop_commands
            },
            "device_settings": {
                k: v for k, v in base["device_settings"].items() if k not in drop_settings
            },
            "state_variables": {
                k: v for k, v in base["state_variables"].items() if k not in drop_states
            },
            "child_entity_types": {
                k: v for k, v in base["child_entity_types"].items()
                if k not in drop_children
            },
            "actions": [
                a for a in base["actions"]
                if a.get("command", a["id"]) not in drop_commands
            ],
        }
        for prop in drop_states:
            self.delete_state(prop)
        log.info("[%s] Presenting the %s surface", self.device_id, family)

    def _set(self, updates: dict[str, Any]) -> None:
        """Write the readings this model declares; drop the rest."""
        declared = self.DRIVER_INFO.get("state_variables", {})
        kept = {k: v for k, v in updates.items() if k in declared}
        if kept:
            self.set_states(kept)

    def _has_adaptors(self) -> bool:
        return "adaptor" in self.DRIVER_INFO.get("child_entity_types", {})

    # ── Connection lifecycle ──

    def _host(self) -> str:
        return str(self.config.get("host", "") or "").strip()

    async def _pre_connect(self) -> None:
        host = self._host()
        if not host:
            raise ConnectionFaultError(
                "Enter the AirMedia receiver's IP address.", code="invalid_config",
            )
        port = int(self.config.get("port", 443) or 443)
        self._base_url = f"https://{host}:{port}"

    async def _create_transport(self, transport_type: str) -> None:
        """Driver-owned session: one httpx client for requests and the long
        poll. ``self.transport`` stays None; _link_alive()/_close_session()
        report and retire the client instead. Redirects are not followed: a
        redirect to the sign-in page is how a CresNext unit can refuse a
        session, and followed, it arrives as a 200 page with no JSON."""
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            verify=bool(self.config.get("verify_ssl", False)),
            timeout=10.0,
            follow_redirects=False,
            headers={"Referer": self._base_url},
        )
        self._xsrf = ""
        self._refused = ""
        self._denied = set()

    async def _post_connect(self) -> None:
        host = self._host()
        try:
            await self._sign_in()
            dev = await self._get(_DEVICE_INFO, core=True)
        except ConnectionFaultError:
            raise
        except httpx.ConnectError as exc:
            text = str(exc)
            if "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text:
                raise ConnectionFaultError(
                    "The receiver's TLS certificate is not trusted. Turn off "
                    "Verify TLS Certificate for this device, or install a "
                    "trusted certificate on the receiver.",
                    code="tls_cert_untrusted",
                ) from exc
            raise ConnectionError(f"Could not reach the AirMedia at {host}: {exc}") from exc
        except httpx.TransportError as exc:
            raise ConnectionError(f"Could not reach the AirMedia at {host}: {exc}") from exc
        if not dev or not isinstance(dev.get("DeviceInfo"), dict):
            raise ConnectionError(
                f"The device at {host} did not answer with its device information."
            )
        self._apply_device(dev, partial=False)
        log.info(
            "[%s] Connected to %s at %s", self.device_id,
            self.get_state("model") or "an AirMedia receiver", host,
        )

    async def _initial_sync(self) -> None:
        try:
            await self.poll()
        except httpx.TransportError as exc:
            raise ConnectionError(f"Could not reach the AirMedia at {self._host()}: {exc}") from exc
        self._longpoll_task = asyncio.create_task(self._longpoll_loop())

    def _link_alive(self) -> bool:
        return self._client is not None

    async def disconnect(self) -> None:
        # End the session on the receiver while the client is still open;
        # harmless if the receiver is already gone.
        if self._client is not None:
            try:
                await self._client.get("/logout")
            except Exception:
                pass
        await super().disconnect()

    async def _close_session(self) -> None:
        tasks = [self._longpoll_task, *self._reread_tasks.values()]
        self._longpoll_task = None
        self._reread_tasks = {}
        self._reread_wanted = set()
        for task in tasks:
            if task is None or task is asyncio.current_task():
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        client, self._client = self._client, None
        self._xsrf = ""
        self._refused = ""
        if client is not None:
            await client.aclose()

    # ── Sign-in ──

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise ConnectionError("Not connected")
        return self._client

    async def _sign_in(self) -> None:
        """The Authentication topic's session: GET the sign-in page for its
        TRACKID cookie, POST the credentials URL-encoded, keep the cookies
        (the client's jar) and the CREST-XSRF-TOKEN header."""
        client = self._require_client()
        username = str(self.config.get("username", "admin") or "")
        password = str(self.config.get("password", "") or "")

        # A receiver nobody has signed in to yet asks for a new administrator
        # account (manual, Sign in to the Receiver). Read that from the
        # redirect rather than following it.
        landing = await client.get(_SIGN_IN, follow_redirects=False)
        where = (landing.headers.get("location", "") + " " + str(landing.url)).lower()
        if "createuser" in where:
            raise ConnectionFaultError(
                "This receiver has no administrator account yet. Open its web "
                "page, create the account, then enter it here.",
                code="auth_failed",
            )
        # Never send a sign-in that cannot succeed.
        if not password:
            raise ConnectionFaultError(
                "Enter the password of an administrator account on the receiver.",
                code="auth_failed",
            )

        resp = await client.post(
            _SIGN_IN,
            content=urlencode({"login": username, "passwd": password}),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": self._base_url,
                "Referer": f"{self._base_url}{_SIGN_IN}",
            },
            follow_redirects=False,
        )
        # A redirect back to the sign-in page is a refusal too, so a refused
        # password is never counted as accepted and sent again.
        if _session_refused(resp):
            raise ConnectionFaultError(self._sign_in_refusal(resp.text), code="auth_failed")
        # 200 OK per the topic; its sample client expects a redirect to "/".
        if resp.status_code not in (200, 302, 303):
            raise ConnectionError(
                f"The AirMedia answered the sign-in with HTTP {resp.status_code}."
            )
        token = resp.headers.get("CREST-XSRF-TOKEN", "")
        if token:
            self.redact_in_log(token)
        self._xsrf = token
        self._sign_ins += 1

    @staticmethod
    def _sign_in_refusal(body: str) -> str:
        """The receiver's own sentence when it has blocked this address, else
        the credentials."""
        for line in re.sub(r"<[^>]*>", "\n", body or "").splitlines():
            line = " ".join(line.split()).rstrip(".")
            if "blocked" in line.lower() and len(line) <= 200:
                return f"The AirMedia refused the sign-in: {line}."
        return _SIGN_IN_REFUSED

    async def _sign_in_again(self, sign_ins_seen: int) -> None:
        """Sign in again after a refused session, once for every request that
        saw the refusal. A refused sign-in is ``auth_failed``, and nothing
        sends the password again on this connection."""
        async with self._sign_in_lock:
            if self._refused:
                raise ConnectionFaultError(self._refused, code="auth_failed")
            if self._sign_ins != sign_ins_seen:
                return  # another request has already signed in again
            try:
                await self._sign_in()
            except ConnectionFaultError as exc:
                self._refused = str(exc)
                raise

    def _lose_auth(self, message: str) -> None:
        """A refusal met by a background task: drop the connection typed."""
        if getattr(self, "_connected", False):
            self._force_disconnect("auth_failed", message)

    # ── Requests ──

    async def _get(self, path: str, *, core: bool = False) -> dict[str, Any] | None:
        """GET one object; the ``Device`` tree of the answer, or None.

        Transport errors propagate (the poll contract). A refused session
        signs in again once. Refused again straight after: on the core read
        that is ``auth_failed``; on any other object the account may not read
        it, which last_error names while it holds."""
        client = self._require_client()
        sign_ins = self._sign_ins
        resp = await client.get(path)
        if _session_refused(resp):
            await self._sign_in_again(sign_ins)
            resp = await client.get(path)
            if _session_refused(resp):
                if core:
                    message = "The AirMedia refused the account after signing in again."
                    self._stash_fault("auth_failed", message)
                    raise ConnectionFaultError(message, code="auth_failed")
                self._denied.add(path)
                return None
        self._denied.discard(path)
        if not resp.is_success:
            log.warning("[%s] GET %s -> HTTP %s", self.device_id, path, resp.status_code)
            return None
        try:
            body = resp.json()
        except ValueError:
            log.warning("[%s] GET %s answered something other than JSON", self.device_id, path)
            return None
        dev = body.get("Device") if isinstance(body, dict) else None
        return dev if isinstance(dev, dict) else None

    async def _post(self, obj: dict[str, Any], what: str) -> None:
        """POST a partial object to /Device (Make API Calls) and raise when
        the receiver refused any of it. A refused session signs in again
        once and sends it again."""
        client = self._require_client()
        body = {"Device": obj}
        for attempt in (1, 2):
            sign_ins = self._sign_ins
            headers = {"Content-Type": "application/json"}
            if self._xsrf:
                headers["X-CREST-XSRF-TOKEN"] = self._xsrf
            resp = await client.post("/Device", json=body, headers=headers)
            if _session_refused(resp):
                if attempt == 1:
                    await self._sign_in_again(sign_ins)
                    continue
                raise RuntimeError(
                    f"The AirMedia refused {what}: this account may not change it."
                )
            break
        if not resp.is_success:
            raise RuntimeError(f"The AirMedia answered {what} with HTTP {resp.status_code}.")
        try:
            actions = resp.json().get("Actions", [])
        except (ValueError, AttributeError):
            return
        for action in actions if isinstance(actions, list) else []:
            for result in action.get("Results", []) or []:
                status = result.get("StatusId", 0)
                if not isinstance(status, int):
                    continue
                info = str(result.get("StatusInfo", "") or "").strip()
                if status < 0 or status == _UNSUPPORTED:
                    raise RuntimeError(
                        f"The AirMedia refused {what}: {info or 'error ' + str(status)}."
                    )
                if status > 0:
                    log.info("[%s] %s: %s", self.device_id, what, info or f"status {status}")

    async def _read(self, path: str) -> None:
        dev = await self._get(path)
        if dev:
            self._apply_device(dev, partial=False)

    # ── Polling ──

    def _reads(self) -> list[str]:
        paths = [_SOURCES, _AVIO, _OCCUPANCY]
        if self._has_adaptors():
            paths.append(_ADAPTORS)
        return paths

    async def poll(self) -> None:
        """Read every object. The AirMedia object comes first: it is the read
        that proves the session. Transport errors propagate."""
        if self._client is None:
            return
        dev = await self._get(_AIRMEDIA, core=True)
        if dev:
            self._apply_device(dev, partial=False)
        for path in self._reads():
            await self._read(path)
        if self._denied:
            names = ", ".join(sorted(p.rsplit("/", 1)[-1] for p in self._denied))
            self.set_state("last_error", f"This account may not read: {names}.")

    # ── Push: the long poll ──

    async def _longpoll_loop(self) -> None:
        """Hold one long poll open at a time and apply each answer. Failures
        back off and never take the device down; polling notices a receiver
        that stopped answering."""
        failures = 0
        warned = False
        while self._client is not None:
            client = self._client
            sign_ins = self._sign_ins
            started = time.monotonic()
            try:
                resp = await client.get(
                    _LONGPOLL,
                    timeout=httpx.Timeout(10.0, read=_LONGPOLL_HOLD_S),
                )
            except asyncio.CancelledError:
                raise
            except httpx.ReadTimeout:
                continue
            except Exception as exc:
                if self._client is None:
                    return
                failures += 1
                msg = f"[{self.device_id}] Long poll failed ({str(exc) or type(exc).__name__}); retrying"
                if warned:
                    log.debug(msg)
                else:
                    log.warning(msg)
                    warned = True
                await asyncio.sleep(min(2.0 * failures, 30.0))
                continue
            if _session_refused(resp):
                try:
                    await self._sign_in_again(sign_ins)
                except ConnectionFaultError as exc:
                    self._lose_auth(str(exc))
                    return
                except Exception:
                    await asyncio.sleep(min(2.0 * (failures + 1), 30.0))
                    continue
                # A change made while no session was live may never be
                # reported on the new one: read everything once.
                for path in (_AIRMEDIA, *self._reads()):
                    self._reread(path)
                continue
            if not resp.is_success:
                failures += 1
                await asyncio.sleep(min(2.0 * failures, 30.0))
                continue
            failures = 0
            warned = False
            try:
                body = resp.json()
            except ValueError:
                body = None
            dev = body.get("Device") if isinstance(body, dict) else None
            if isinstance(dev, dict) and dev:
                self._apply_device(dev, partial=True)
            elif time.monotonic() - started < 1.0:
                # A timeout answered at once: do not ask in a tight loop.
                await asyncio.sleep(1.0)

    def _reread(self, path: str) -> None:
        """Read an object again in the background: once for a burst of
        changes, at most once every _REREAD_GAP_S, and once more when a change
        arrives while a read is under way."""
        self._reread_wanted.add(path)
        task = self._reread_tasks.get(path)
        if task is None or task.done():
            self._reread_tasks[path] = asyncio.create_task(self._reread_loop(path))

    async def _reread_loop(self, path: str) -> None:
        while path in self._reread_wanted and self._client is not None:
            wait = self._reread_at.get(path, 0.0) + _REREAD_GAP_S - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._reread_wanted.discard(path)
            self._reread_at[path] = time.monotonic()
            try:
                await self._read(path)
            except asyncio.CancelledError:
                raise
            except ConnectionFaultError as exc:
                self._lose_auth(str(exc))
                return
            except Exception as exc:
                log.debug("[%s] Reading %s again failed: %s", self.device_id, path, exc)

    # ── Applying the Device tree ──

    def _apply_device(self, dev: dict[str, Any], *, partial: bool) -> None:
        """Apply a Device tree: a full object read or a long poll's changes.
        Only the properties present are written. In a partial tree a
        dictionary (users, adaptors, sources, sensors) is read again whole."""
        if isinstance(dev.get("DeviceInfo"), dict):
            self._apply_identity(dev["DeviceInfo"])
        if isinstance(dev.get("AirMedia"), dict):
            self._apply_airmedia(dev["AirMedia"], partial)
        if isinstance(dev.get("SourceSelectionConfiguration"), dict):
            self._apply_sources(dev["SourceSelectionConfiguration"], partial)
        if isinstance(dev.get("AvioV2"), dict):
            self._apply_avio(dev["AvioV2"])
        if isinstance(dev.get("AirMediaTx3"), dict) and self._has_adaptors():
            self._apply_adaptors(dev["AirMediaTx3"], partial)
        app = dev.get("App")
        config = app.get("Config") if isinstance(app, dict) else None
        if isinstance(config, dict) and isinstance(config.get("OccupancySensor"), dict):
            self._apply_occupancy(config["OccupancySensor"], partial)

    def _apply_identity(self, info: dict[str, Any]) -> None:
        updates: dict[str, Any] = {}
        for key, prop in (
            ("Model", "model"), ("SerialNumber", "serial_number"),
            ("DeviceVersion", "firmware"), ("Name", "device_name"),
            ("MacAddress", "mac_address"), ("RebootReason", "reboot_reason"),
        ):
            if key in info and info[key] is not None:
                updates[prop] = str(info[key]).strip()
        if "Model" in info:
            family = _model_family(info.get("Model"))
            updates["model_family"] = family
            if family and family != self._family:
                self._family = family
                self._narrow()
            elif not family:
                log.info(
                    "[%s] The receiver reports model %r; offering every command",
                    self.device_id, info.get("Model"),
                )
        self._set(updates)

    def _apply_airmedia(self, am: dict[str, Any], partial: bool) -> None:
        updates: dict[str, Any] = {}
        for key, prop, cast in (
            ("LoginCodeMode", "login_code_mode", str),
            ("ActiveLoginCodeMode", "active_login_code_mode", str),
            # The API Reference's property table spells it this way.
            ("AcitveLoginCodeMode", "active_login_code_mode", str),
            ("ActiveLoginCode", "login_code", str),
            ("LoginCode", "fixed_login_code", str),
            ("ShowLoginCode", "show_login_code", bool),
            ("SystemMode", "system_mode", str),
            ("IsCanvasEnabled", "canvas_enabled", bool),
            ("IsDiscoveryEnabled", "airmedia_discovery", bool),
            ("InactivityTimeout", "inactivity_timeout", int),
        ):
            if key in am and am[key] is not None:
                updates[prop] = cast(am[key])
        miracast = am.get("Miracast")
        if isinstance(miracast, dict) and "IsEnabled" in miracast:
            updates["miracast_enabled"] = bool(miracast["IsEnabled"])

        clients = am.get("ClientData")
        if isinstance(clients, dict):
            if "Status" in clients:
                updates["presentation_status"] = str(clients["Status"])
            if "TotalUsers" in clients:
                updates["connected_users"] = int(clients["TotalUsers"] or 0)
            if "IsAnyWifiUserConnected" in clients:
                updates["wifi_users_connected"] = bool(clients["IsAnyWifiUserConnected"])
            if "ConnectedClients" in clients:
                if partial:
                    self._reread(_CLIENT_DATA)
                else:
                    connected = clients.get("ConnectedClients")
                    entries = connected.values() if isinstance(connected, dict) else []
                    updates["presenting_users"] = sum(
                        1 for c in entries if isinstance(c, dict) and c.get("IsClientActive")
                    )

        conferencing = am.get("WirelessConferencing")
        if isinstance(conferencing, dict):
            cfg = conferencing.get("Configuration")
            if isinstance(cfg, dict):
                for key, prop, cast in (
                    ("IsEnabled", "conferencing_enabled", bool),
                    ("PeripheralVolume", "peripheral_volume", int),
                    ("PeripheralMute", "peripheral_mute", bool),
                    ("IsPeripheralVolumeSupported", "peripheral_volume_supported", bool),
                ):
                    if key in cfg and cfg[key] is not None:
                        updates[prop] = cast(cfg[key])
            status = conferencing.get("Status")
            if isinstance(status, dict):
                for key, prop, cast in (
                    ("ConferencingStatus", "conferencing_status", str),
                    ("IsPeripheralBlocked", "peripherals_blocked", bool),
                    ("PeripheralBlockedReason", "peripherals_blocked_reason", str),
                    ("IsMicDetected", "mic_detected", bool),
                    ("IsMicInUse", "mic_in_use", bool),
                    ("MicModel", "mic_model", str),
                    ("IsSpeakerDetected", "speaker_detected", bool),
                    ("IsSpeakerInUse", "speaker_in_use", bool),
                    ("SpeakerModel", "speaker_model", str),
                    ("IsCameraDetected", "camera_detected", bool),
                    ("IsCameraInUse", "camera_in_use", bool),
                    ("CameraModel", "camera_model", str),
                ):
                    if key in status and status[key] is not None:
                        updates[prop] = cast(status[key])
        self._set(updates)

    def _apply_sources(self, ssc: dict[str, Any], partial: bool) -> None:
        updates: dict[str, Any] = {}
        if ssc.get("RoutingMode") is not None:
            updates["routing_mode"] = str(ssc["RoutingMode"])
        if "SourcesV2" in ssc:
            if partial:
                self._reread(_SOURCES)
            else:
                updates["active_sources"] = ", ".join(self._active_source_names(ssc["SourcesV2"]))
        self._set(updates)

    @staticmethod
    def _active_source_names(groups: Any) -> list[str]:
        """The names of every source marked active, in the receiver's order,
        sub-inputs (an adaptor under its group) included."""
        names: list[str] = []

        def visit(sources: Any) -> None:
            if not isinstance(sources, dict):
                return
            for key in sorted(sources):
                source = sources[key]
                if not isinstance(source, dict):
                    continue
                if source.get("IsActive"):
                    name = str(source.get("Name") or source.get("Id") or key).strip()
                    if name and name not in names:
                        names.append(name)
                # The table spells it SourceSubinputs, the example SourceSubInputs.
                visit(source.get("SourceSubInputs") or source.get("SourceSubinputs"))

        if isinstance(groups, dict):
            for group_key in sorted(groups):
                group = groups[group_key]
                if isinstance(group, dict):
                    visit(group.get("Sources"))
        return names

    def _apply_avio(self, avio: dict[str, Any]) -> None:
        updates: dict[str, Any] = {}
        outputs = avio.get("Outputs")
        if isinstance(outputs, dict) and outputs:
            out_key = self._output_keys[0] if self._output_keys[0] in outputs else _first_key(outputs)
            info = outputs[out_key].get("OutputInfo") if isinstance(outputs[out_key], dict) else None
            ports = info.get("Ports") if isinstance(info, dict) else None
            if isinstance(ports, dict) and ports:
                port_key = self._output_keys[1] if self._output_keys[1] in ports else _first_key(ports)
                port = ports[port_key]
                if isinstance(port, dict):
                    self._output_keys = (out_key, port_key)
                    if "IsSinkConnected" in port:
                        updates["display_connected"] = bool(port["IsSinkConnected"])
                    resolution = _resolution(port)
                    if resolution is not None:
                        updates["output_resolution"] = resolution
                    digital = port.get("Digital")
                    if isinstance(digital, dict) and "IsOutputDisabled" in digital:
                        updates["output_disabled"] = bool(digital["IsOutputDisabled"])
        inputs = avio.get("Inputs")
        if isinstance(inputs, dict) and inputs:
            port = self._hdmi_input_port(inputs)
            if port is not None:
                if "IsSyncDetected" in port:
                    updates["hdmi_input_sync"] = bool(port["IsSyncDetected"])
                resolution = _resolution(port)
                if resolution is not None:
                    updates["hdmi_input_resolution"] = resolution
        self._set(updates)

    def _hdmi_input_port(self, inputs: dict[str, Any]) -> dict[str, Any] | None:
        """The HDMI input's port: the one whose PortType says Hdmi, else the
        one found before (a partial update names no PortType)."""
        for in_key in sorted(inputs):
            info = inputs[in_key].get("InputInfo") if isinstance(inputs[in_key], dict) else None
            ports = info.get("Ports") if isinstance(info, dict) else None
            if not isinstance(ports, dict):
                continue
            for port_key in sorted(ports):
                port = ports[port_key]
                if not isinstance(port, dict):
                    continue
                if str(port.get("PortType", "")).lower() == "hdmi":
                    self._input_keys = (in_key, port_key)
                    return port
                if self._input_keys == (in_key, port_key):
                    return port
        return None

    def _apply_adaptors(self, tx3: dict[str, Any], partial: bool) -> None:
        if tx3.get("PairingStatus") is not None:
            self._set({"adaptor_pairing_status": str(tx3["PairingStatus"])})
        roster = tx3.get("Tx3DevicesMap")
        if roster is None and "Tx3DevicesMap" not in tx3:
            return
        known = set(self.list_children("adaptor"))
        if partial:
            # Changes to adaptors already listed apply at once; the list is
            # read again as well, since the API does not say how a long poll
            # reports an adaptor removed.
            if isinstance(roster, dict):
                for key, entry in roster.items():
                    if key in known and isinstance(entry, dict):
                        fields = self._adaptor_fields(entry)
                        if fields:
                            self.set_child_state_batch("adaptor", key, fields)
            self._reread(_ADAPTORS)
            return
        entries = roster if isinstance(roster, dict) else {}
        wanted = {key for key, entry in entries.items() if isinstance(entry, dict)}
        for stale in known - wanted:
            self.deregister_child("adaptor", stale)
        for key in sorted(wanted):
            fields = self._adaptor_fields(entries[key])
            if key in known:
                self.set_child_state_batch("adaptor", key, fields)
            else:
                self.register_child("adaptor", key, initial_state=fields)
        self._set({"paired_adaptors": len(wanted)})

    @staticmethod
    def _adaptor_fields(entry: dict[str, Any]) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        for key, prop in (
            ("FriendlyName", "label"), ("Model", "model"),
            ("SerialNumber", "serial_number"), ("FirmwareVersion", "firmware"),
            ("MacAddress", "mac_address"), ("ConnectionStatus", "connection_status"),
            ("ScreenShareStatus", "screen_share_status"),
            ("SourceConnectionStatus", "source_connection_status"),
        ):
            if entry.get(key) is not None:
                fields[prop] = str(entry[key]).strip()
        if entry.get("SignalStrengthPercentage") is not None:
            fields["signal_strength"] = int(entry["SignalStrengthPercentage"])
        if "label" not in fields and entry.get("Name"):
            fields["label"] = str(entry["Name"]).strip()
        status = fields.get("connection_status")
        if status is not None:
            fields["online"] = status.startswith("Online")
        return fields

    def _apply_occupancy(self, occupancy: dict[str, Any], partial: bool) -> None:
        if "Sensors" not in occupancy:
            return
        if partial:
            self._reread(_OCCUPANCY)
            return
        sensors = occupancy.get("Sensors")
        status = ""
        if isinstance(sensors, dict):
            for key in sorted(sensors):
                sensor = sensors[key]
                if isinstance(sensor, dict) and sensor.get("IsEnabled"):
                    status = str(sensor.get("Status") or "").strip()
                    break
        self._set({"occupancy": status})

    # ── Commands ──

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        self._require_client()
        if (command not in self.DRIVER_INFO.get("commands", {})
                and command in type(self).DRIVER_INFO["commands"]):
            label = type(self).DRIVER_INFO["commands"][command]["label"]
            raise RuntimeError(f"The {self._family} has no {label}.")

        match command:
            case "disconnect_all_users":
                await self._post(
                    {"AirMedia": {"DisconnectAllUsers": True}}, "Disconnect All Users",
                )
                await self._read(_CLIENT_DATA)
            case "disable_output" | "enable_output":
                out_key, port_key = self._output_keys
                await self._post(
                    {"AvioV2": {"Outputs": {out_key: {"OutputInfo": {"Ports": {
                        port_key: {"Digital": {"IsOutputDisabled": command == "disable_output"}},
                    }}}}}},
                    "the HDMI output",
                )
                await self._read(_AVIO)
            case "set_peripheral_volume":
                level = max(0, min(100, int(params.get("level", 0))))
                await self._post(
                    {"AirMedia": {"WirelessConferencing": {"Configuration": {
                        "PeripheralVolume": level,
                    }}}},
                    "the USB speaker volume",
                )
                await self._read(_CONFERENCING)
            case "mute_peripheral" | "unmute_peripheral":
                await self._post(
                    {"AirMedia": {"WirelessConferencing": {"Configuration": {
                        "PeripheralMute": command == "mute_peripheral",
                    }}}},
                    "the USB speaker mute",
                )
                await self._read(_CONFERENCING)
            case "start_adaptor_pairing":
                await self._post({"AirMediaTx3": {"PairCmd": True}}, "adaptor pairing")
                await self._read(_ADAPTORS)
            case "reboot":
                await self._post({"DeviceOperations": {"Reboot": True}}, "the restart")
            case _:
                log.warning("[%s] Unknown command: %s", self.device_id, command)
                return None
        log.debug("[%s] Sent command: %s %s", self.device_id, command, params)
        return None

    # ── Device settings ──

    async def set_device_setting(self, key: str, value: Any) -> Any:
        self._require_client()
        if key not in self.DRIVER_INFO.get("device_settings", {}):
            raise ValueError(f"Unknown device setting: {key}")
        label = self.DRIVER_INFO["device_settings"][key]["label"]
        match key:
            case "login_code_mode":
                obj, path = {"AirMedia": {"LoginCodeMode": str(value)}}, _AIRMEDIA
            case "fixed_login_code":
                code = str(value).strip()
                if not re.fullmatch(r"[0-9]{8}", code):
                    raise ValueError("The fixed login code is eight digits.")
                obj, path = {"AirMedia": {"LoginCode": code}}, _AIRMEDIA
            case "show_login_code":
                obj, path = {"AirMedia": {"ShowLoginCode": bool(value)}}, _AIRMEDIA
            case "system_mode":
                obj, path = {"AirMedia": {"SystemMode": str(value)}}, _AIRMEDIA
            case "routing_mode":
                obj, path = {"SourceSelectionConfiguration": {"RoutingMode": str(value)}}, _SOURCES
            case "miracast_enabled":
                obj, path = {"AirMedia": {"Miracast": {"IsEnabled": bool(value)}}}, _AIRMEDIA
            case "canvas_enabled":
                obj, path = {"AirMedia": {"IsCanvasEnabled": bool(value)}}, _AIRMEDIA
            case "airmedia_discovery":
                obj, path = {"AirMedia": {"IsDiscoveryEnabled": bool(value)}}, _AIRMEDIA
            case "inactivity_timeout":
                minutes = int(value)
                if not 1 <= minutes <= 60:
                    raise ValueError("The inactivity timeout is 1 to 60 minutes.")
                obj, path = {"AirMedia": {"InactivityTimeout": minutes}}, _AIRMEDIA
            case "conferencing_enabled":
                obj, path = (
                    {"AirMedia": {"WirelessConferencing": {"Configuration": {
                        "IsEnabled": bool(value),
                    }}}},
                    _CONFERENCING,
                )
            case _:
                raise ValueError(f"Unknown device setting: {key}")
        await self._post(obj, label)
        await self._read(path)

    async def refresh_children(self) -> dict[str, Any]:
        if self._has_adaptors() and self._client is not None:
            await self._read(_ADAPTORS)
        return {"adaptors": len(self.list_children("adaptor")) if self._has_adaptors() else 0}
