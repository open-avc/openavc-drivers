"""
OpenAVC tvONE CORIOmaster Driver.

Controls tvONE CORIOmaster video wall processors — CORIOmaster (C3-540),
CORIOmaster mini (C3-510), CORIOmaster micro (C3-503), and CORIOmaster2
(CM2-547 / CM2-547-MK2) — over the CORIOmax command-line API: plain text on
TCP port 10001 (or RS-232 at 115200 8N1).

Protocol sources: "tvONE CORIOmaster Commands", document v411.0.1 (System
API 4.11, firmware M411), and "tvONE CORIOmaster2 Commands", document
v503.0.1 (System API 5.3, firmware G503), both tvONE. The two documents
describe the same CORIOmax CLI surface for everything this driver uses
(login, events, windows, canvases, presets, storyboards, slots, media
playback).

Wire format:
    Commands are single CRLF-terminated lines. A property read is the bare
    property path ("Window1.Input"); a write is "path = value"; a method
    call ends in parentheses ("Preset.PresetList()"). Every request answers
    zero or more reply lines followed by a terminal line: "!Done <echo>" on
    success or "!Failed <echo>" on failure. Sessions start with
    "login(<user>,<password>)", answered by "!Info : User <name> Logged
    In". Property replies use the long-form path even when the request used
    the short S<n>I<n> alias.

Push vs poll: HYBRID. The CORIOmaster documents a real event subscription:
"AddEvents(<category>)" arms a category on the current connection and the
unit then sends "!Event <category>,<event>,<detail…>" lines asynchronously.
The driver subscribes to WINDOW, PRESET, CANVAS, OUTPUT, INPUT, STBD, and
MEDIA_PLAYER on every connect (subscriptions are per-connection) and folds
events into state as they arrive; polling remains as the resync path and
the session keep-alive (accounts have idle timeouts). A unit whose firmware
predates the event mechanism answers !Failed to AddEvents — the driver
logs it once and runs poll-only. The unit accepts ONE controlling
connection at a time (documented constraint).

Why Python (not .avcdriver YAML):
  * Session login must gate the connection and a rejection must classify
    as an authentication fault (the send_login auth shape tracked in the
    roadmap's auth-extension table).
  * Device-enumerated rosters: installed cards come from "Slots" dumps,
    windows and canvases from "Windows"/"Canvases" dumps filtered by their
    Status — none of that is a config-driven YAML roster.
  * Media inputs get extra per-child state (playback status / queue index)
    only when their slot holds a Streaming Media card — a dynamic
    per-child schema.
  * !Event lines interleave with request replies on one connection and
    fan out into window/canvas/port child state; picker lists (presets,
    storyboards, playlists) aggregate across reply lines.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from server.drivers.base import BaseDriver
from server.transport.tcp import TCPTransport
from server.utils.logger import get_logger

log = get_logger(__name__)

REQUEST_TIMEOUT_S = 4.0
LOGIN_TIMEOUT_S = 6.0

# CORIOmaster C3-540 frames have 16 slots (mini 8, micro 5, CM2 16); used
# only as an enumeration bound, not a registered-children cap.
SLOT_MAX = 16

# Event categories armed on every connect. Subscriptions are documented as
# per-communication-channel, so they re-arm on reconnect.
EVENT_CATEGORIES = (
    "WINDOW",
    "PRESET",
    "CANVAS",
    "OUTPUT",
    "INPUT",
    "STBD",
    "MEDIA_PLAYER",
)

# ── Reply-line patterns (long-form paths, per the command doc examples) ──

_RE_SLOT_LIST = re.compile(r"(?i)^slots\.slot(\d+)\s*=\s*(.+?)\s*$")
_RE_CARDTYPE = re.compile(r"(?i)^(?:slots\.)?slot(\d+)\.cardtype\s*=\s*(.+?)\s*$")
_RE_PORT_STUB = re.compile(r"(?i)^(?:slots\.)?slot(\d+)\.(in|out)(\d+)\s*=\s*<")
# One matcher for every scalar port property; dispatch is by property name.
_RE_PORT_PROP = re.compile(
    r"(?i)^(?:slots\.)?slot(\d+)\.(in|out)(\d+)\.(\w+)\s*=\s*(.*?)\s*$"
)
_RE_QUEUE_PROP = re.compile(
    r"(?i)^(?:slots\.)?slot(\d+)\.in(\d+)\.activequeue\.(\w+)\s*=\s*(.*?)\s*$"
)
_RE_WIN_STUB = re.compile(r"(?i)^(?:routing\.)?windows\.window(\d+)\s*=\s*<")
_RE_WIN_PROP = re.compile(
    r"(?i)^(?:routing\.)?(?:windows\.)?window(\d+)\.(\w+)\s*=\s*(.*?)\s*$"
)
_RE_CANVAS_STUB = re.compile(r"(?i)^(?:routing\.)?canvases\.canvas(\d+)\s*=\s*<")
_RE_CANVAS_PROP = re.compile(
    r"(?i)^(?:routing\.)?(?:canvases\.)?canvas(\d+)\.(\w+)\s*=\s*(.*?)\s*$"
)
_RE_STBD_NAME = re.compile(
    r'(?i)^(?:routing\.)?stbds\.stbd(\d+)\.name\s*=\s*["“]?(.*?)["”]?\s*$'
)
_RE_PLAYLIST_STUB = re.compile(r"(?i)^resources\.playlists\.playlist(\d+)\s*=\s*<")
_RE_PLAYLIST_NAME = re.compile(
    r'(?i)^resources\.playlists\.playlist(\d+)\.name\s*=\s*["“]?(.*?)["”]?\s*$'
)
_RE_SYS_STATUS = re.compile(r"(?i)^system\.status\s*=\s*(\S+)")
_RE_API_VER = re.compile(r"(?i)^system\.api_version\s*=\s*(.+?)\s*$")
_RE_UNIT_DESC = re.compile(r'(?i)^system\.unit_description\s*=\s*"?(.*?)"?\s*$')
_RE_STANDBY = re.compile(r"(?i)^system\.standbymode\s*=\s*(on|off)\s*$")
_RE_MODEL_NAME = re.compile(r"(?i)^coriomax\.model_name\s*=\s*(.+?)\s*$")
_RE_MODEL_NUM = re.compile(r"(?i)^coriomax\.model_number\s*=\s*(.+?)\s*$")
_RE_SW_VER = re.compile(r"(?i)^coriomax\.software_version\s*=\s*(.+?)\s*$")
_RE_PRESET_TAKE = re.compile(r"(?i)^(?:routing\.)?preset\.take\s*=\s*(\d+)")
_RE_PRESET_LIST = re.compile(
    r"(?i)^(?:routing\.)?preset\.presetlist\[(\d+)\]\s*=\s*(.*?)\s*$"
)
_RE_LOGIN_OK = re.compile(r"(?i)^!info\s*:\s*user\s+.+\s+logged\s+in")
_RE_TERMINAL = re.compile(r"(?i)^!(done|failed)\b")
_RE_EVENT = re.compile(r"(?i)^!event\s+(.*)$")

# Input/output references inside values and events: long form
# ("Slot4.In1"), or the dotted compact form HDMI sink events use ("s3.o1").
_RE_PORT_REF = re.compile(
    r"(?i)^(?:(?:slots\.)?slot(\d+)\.(in|out)(\d+)|s(\d+)\.?([io])(\d+))$"
)


def _alias(kind: str, slot: int, port: int) -> str:
    """Compact alias id for a port ('s3i1' / 's14o1')."""
    return f"s{slot}{'i' if kind == 'in' else 'o'}{port}"


def _long_form(alias_id: Any) -> str | None:
    """'s3i1' -> 'Slot3.In1'; 's14o2' -> 'Slot14.Out2'. None if malformed."""
    m = re.fullmatch(r"(?i)s(\d+)([io])(\d+)", str(alias_id).strip())
    if not m:
        return None
    word = "In" if m.group(2).lower() == "i" else "Out"
    return f"Slot{int(m.group(1))}.{word}{int(m.group(3))}"


def _port_ref(value: str) -> tuple[str, int, int] | None:
    """Parse a port reference in any documented form -> (kind, slot, port)."""
    m = _RE_PORT_REF.match(value.strip())
    if not m:
        return None
    if m.group(1) is not None:
        return ("in" if m.group(2).lower() == "in" else "out",
                int(m.group(1)), int(m.group(3)))
    return ("in" if m.group(5).lower() == "i" else "out",
            int(m.group(4)), int(m.group(6)))


def _normalize_source(value: str) -> str:
    """Normalize an input reference (or comma list) to compact alias form.

    'Slot3.In1' -> 's3i1'; 'NULL' -> ''. Unrecognized items pass through.
    """
    value = value.strip()
    if not value or value.upper() == "NULL":
        return ""
    items = []
    for item in value.split(","):
        ref = _port_ref(item)
        items.append(_alias(ref[0], ref[1], ref[2]) if ref else item.strip())
    return ",".join(items)


def _win_ref(value: str) -> int | None:
    """'Window3' or '3' -> 3. None if malformed or NULL."""
    m = re.fullmatch(r"(?i)(?:window)?(\d+)", str(value).strip())
    return int(m.group(1)) if m else None


# Base schema for input-port children. Inputs on a Streaming Media card get
# these PLUS the media playback props via a per-child dynamic schema (a
# per-child schema replaces the type-level one, so the base is shared here).
_INPUT_PROPS: dict[str, dict[str, Any]] = {
    "status": {
        "type": "string",
        "label": "Signal Status",
        "help": "The input's reported status (OK when a source is detected).",
        "cloud_priority": "high",
    },
    "resolution": {
        "type": "string",
        "label": "Measured Resolution",
        "cloud_priority": "low",
    },
    "hdmi": {
        "type": "string",
        "label": "HDMI",
        "help": "Found when an HDMI-capable source is detected.",
        "cloud_priority": "low",
    },
    "audio": {
        "type": "string",
        "label": "Audio",
        "help": "Found when the source carries audio.",
        "cloud_priority": "low",
    },
    "name": {"type": "string", "label": "Name", "cloud_priority": "low"},
}

_MEDIA_PROPS: dict[str, dict[str, Any]] = {
    "media_status": {
        "type": "string",
        "label": "Playback",
        "help": "Streaming Media play-queue state (Idle, Playing, Paused, …).",
        "cloud_priority": "high",
    },
    "media_item": {
        "type": "integer",
        "label": "Playing Item",
        "help": "Index of the queue item currently playing.",
        "cloud_priority": "low",
    },
}


class TvoneCoriomasterDriver(BaseDriver):
    """tvONE CORIOmaster family video wall processors (CORIOmax CLI)."""

    DRIVER_INFO = {
        "id": "tvone_coriomaster",
        "name": "tvONE CORIOmaster",
        "manufacturer": "tvONE",
        "category": "video",
        "version": "1.0.0",
        # String/integer-id children registered from device-enumerated
        # rosters with per-child dynamic schemas (media inputs).
        "min_platform_version": "0.19.4",
        "author": "OpenAVC",
        "description": (
            "Controls tvONE CORIOmaster, CORIOmaster mini/micro, and "
            "CORIOmaster2 video wall processors over the CORIOmax "
            "command-line API (TCP 10001). Windows, canvases, and the "
            "installed input/output cards are discovered from the unit and "
            "modeled as child entities — route sources into wall windows, "
            "move and resize windows, recall presets and storyboards, run "
            "canvas audio, and drive the Streaming Media card's playback, "
            "with live updates over the unit's event subscription."
        ),
        "source_url": "https://api.tvone.com/products/c3-series/c3-5xx/tvONE%20CORIOmaster%20Commands_current.pdf",
        "tags": ["video-wall", "processor", "coriomaster", "coriomax", "tvone"],
        "verified": False,
        "simulated": True,
        "protocols": ["coriomax_cli"],
        "ports": [10001],
        "transport": "tcp",
        "discovery": {
            # The CORIOmax CLI requires a login before answering anything
            # documented, so there is no safe active probe; port 10001 plus
            # the vendor string is the available evidence.
            "port_open": [10001],
            "manufacturer_alias": ["tvone", "tv one"],
        },
        "compatible_models": [
            {
                "manufacturer": "tvONE",
                "models": [
                    "CORIOmaster (C3-540)",
                    "CORIOmaster mini (C3-510)",
                    "CORIOmaster micro (C3-503)",
                    "CORIOmaster2 (CM2-547)",
                    "CORIOmaster2 (CM2-547-MK2)",
                ],
                "confidence": "untested",
                "notes": (
                    "All frames speak the same CORIOmax command-line API "
                    "(CORIOmaster document v411.0.1, CORIOmaster2 document "
                    "v503.0.1). Live event updates need firmware with the "
                    "AddEvents mechanism (M411 / G503 documents); older "
                    "firmware falls back to polling automatically. The "
                    "unit accepts ONE controlling connection at a time — "
                    "close CORIOgrapher before connecting OpenAVC. Standby "
                    "mode is supported on the CORIOmaster micro only. "
                    "Factory login is admin / adminpw."
                ),
            },
        ],
        "help": {
            "overview": (
                "CORIOmaster is tvONE's modular video wall processor — "
                "slots take DVI, HDMI, SDI, HDBaseT, and Streaming Media "
                "cards, and outputs are composed into canvases carrying "
                "movable source windows. The driver logs in, discovers the "
                "installed cards, windows, and canvases, and models each as "
                "a child entity: windows carry their source and geometry, "
                "canvases their audio and active storyboard, outputs their "
                "cut-to-black state, inputs their signal status. Wall "
                "presets and storyboards recall from live dropdowns, and "
                "Streaming Media inputs expose playback transport controls. "
                "State updates arrive live through the unit's event "
                "subscription; polling covers older firmware."
            ),
            "setup": (
                "1. Give the unit a static IP (default 192.168.0.10) and "
                "add it here with port 10001.\n"
                "2. Enter the control login — the factory account is "
                "username admin, password adminpw (the password field is "
                "left blank on purpose; type the unit's real password).\n"
                "3. The unit accepts one controller at a time: close any "
                "open CORIOgrapher / CORIOdiscover session first.\n"
                "4. Build walls, windows, and presets in CORIOgrapher "
                "first; this driver operates what commissioning created. "
                "Use Refresh from Device after changing the configuration.\n"
                "5. Keep polling enabled: the session logs out after a few "
                "minutes idle, and polling doubles as the keep-alive."
            ),
        },
        "default_config": {
            "host": "",
            "port": 10001,
            "username": "admin",
            "password": "",
            "poll_interval": 15,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {
                "type": "integer",
                "default": 10001,
                "label": "TCP Port",
                "description": "CORIOmax CLI port — 10001 on every unit.",
            },
            "username": {
                "type": "string",
                "default": "admin",
                "label": "Username",
                "description": "Control account — the factory administrator account is 'admin'.",
            },
            "password": {
                "type": "string",
                "default": "",
                "secret": True,
                "label": "Password",
                "description": "Password for the control account. Factory default is 'adminpw'.",
            },
            "poll_interval": {
                "type": "integer",
                "default": 15,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "Resyncs windows, canvases, ports, and pickers, and "
                    "keeps the login session alive (the unit logs an idle "
                    "session out). Event subscriptions carry changes "
                    "between polls. Leave above 0."
                ),
            },
        },
        "state_variables": {
            "status": {
                "type": "string",
                "label": "System Status",
                "help": "The unit's reported status (System.Status, e.g. Serving).",
            },
            "model": {
                "type": "string",
                "label": "Model",
                "help": "Model name and number reported by the unit.",
            },
            "firmware_version": {"type": "string", "label": "Firmware Version"},
            "api_version": {"type": "string", "label": "API Version"},
            "device_name": {
                "type": "string",
                "label": "Device Name",
                "help": "The unit's name (System.Unit_Description).",
            },
            "active_preset": {
                "type": "integer",
                "label": "Active Preset",
                "help": "Last wall preset taken (Preset.Take).",
                "cloud_priority": "high",
            },
            "standby": {
                "type": "boolean",
                "label": "Standby",
                "help": (
                    "System standby state (System.StandbyMode) — supported "
                    "on the CORIOmaster micro; other models report Off."
                ),
                "cloud_priority": "high",
            },
            "preset_options": {
                "type": "string",
                "label": "Preset Options",
                "help": "JSON list of the wall presets stored on the unit — feeds the Recall Preset picker.",
                "cloud_priority": "low",
            },
            "stbd_options": {
                "type": "string",
                "label": "Storyboard Options",
                "help": "JSON list of the storyboards stored on the unit — feeds the Run Storyboard picker.",
                "cloud_priority": "low",
            },
            "input_options": {
                "type": "string",
                "label": "Input Options",
                "help": "JSON list of the installed input ports — feeds source pickers.",
                "cloud_priority": "low",
            },
            "media_input_options": {
                "type": "string",
                "label": "Media Input Options",
                "help": "JSON list of Streaming Media card inputs — feeds the media transport pickers.",
                "cloud_priority": "low",
            },
            "playlist_options": {
                "type": "string",
                "label": "Playlist Options",
                "help": "JSON list of the playlists stored on the unit — feeds the Load Playlist picker.",
                "cloud_priority": "low",
            },
        },
        "child_entity_types": {
            "window": {
                "label": "Window",
                "label_plural": "Windows",
                "id_format": {"type": "integer", "min": 1},
                "state_variables": {
                    "input": {
                        "type": "string",
                        "label": "Source",
                        "help": "Alias of the input feeding this window (e.g. s4i1).",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "canvas": {"type": "string", "label": "Canvas", "cloud_priority": "low"},
                    "x": {
                        "type": "integer",
                        "label": "X Centre",
                        "min": -8192,
                        "max": 8191,
                        "cloud_priority": "low",
                    },
                    "y": {
                        "type": "integer",
                        "label": "Y Centre",
                        "min": -8192,
                        "max": 8191,
                        "cloud_priority": "low",
                    },
                    "width": {
                        "type": "integer",
                        "label": "Width",
                        "min": 0,
                        "max": 16383,
                        "cloud_priority": "low",
                    },
                    "height": {
                        "type": "integer",
                        "label": "Height",
                        "min": 0,
                        "max": 16383,
                        "cloud_priority": "low",
                    },
                    "zorder": {
                        "type": "integer",
                        "label": "Z-Order",
                        "min": 0,
                        "max": 15,
                        "cloud_priority": "low",
                    },
                    "ftb": {
                        "type": "integer",
                        "label": "Fade to Black",
                        "help": "0 = full brightness, 256 = black.",
                        "min": 0,
                        "max": 256,
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "status": {"type": "string", "label": "Status", "cloud_priority": "low"},
                    "name": {"type": "string", "label": "Name", "cloud_priority": "low"},
                },
                "summary_fields": ["input", "canvas"],
                "label_field": "name",
            },
            "canvas": {
                "label": "Canvas",
                "label_plural": "Canvases",
                "id_format": {"type": "integer", "min": 1},
                "state_variables": {
                    "current_storyboard": {
                        "type": "string",
                        "label": "Current Storyboard",
                        "help": "Storyboard last run on this canvas (e.g. Stbd1); empty when none.",
                        "cloud_priority": "high",
                    },
                    "audio_volume": {
                        "type": "integer",
                        "label": "Audio Volume",
                        "min": 0,
                        "max": 100,
                        "step": 1,
                        "unit": "%",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "audio_mute": {
                        "type": "boolean",
                        "label": "Audio Mute",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "audio_mode": {
                        "type": "string",
                        "label": "Audio Mode",
                        "help": "FromSource (follow audio_source) or FollowWindow.",
                        "cloud_priority": "low",
                    },
                    "audio_source": {
                        "type": "string",
                        "label": "Audio Source",
                        "help": "Input alias feeding canvas audio when audio_mode is FromSource.",
                        "cloud_priority": "low",
                    },
                    "audio_follow_window": {
                        "type": "integer",
                        "label": "Audio Window",
                        "help": "Window whose source feeds canvas audio when audio_mode is FollowWindow.",
                        "cloud_priority": "low",
                    },
                    "window_list": {
                        "type": "string",
                        "label": "Windows",
                        "help": "Comma list of the windows assigned to this canvas.",
                        "cloud_priority": "low",
                    },
                    "name": {"type": "string", "label": "Name", "cloud_priority": "low"},
                },
                "summary_fields": ["current_storyboard", "audio_volume"],
                "label_field": "name",
            },
            "input": {
                "label": "Input",
                "label_plural": "Inputs",
                "id_format": {"type": "string", "max_length": 12},
                # Media-card inputs get _INPUT_PROPS + _MEDIA_PROPS via a
                # per-child schema at registration; hence dynamic.
                "dynamic": True,
                "state_variables": _INPUT_PROPS,
                "summary_fields": ["status"],
                "label_field": "name",
            },
            "output": {
                "label": "Output",
                "label_plural": "Outputs",
                "id_format": {"type": "string", "max_length": 12},
                "state_variables": {
                    "cut_to_black": {
                        "type": "boolean",
                        "label": "Cut to Black",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "source_list": {
                        "type": "string",
                        "label": "Routed Sources",
                        "help": "Aliases of the inputs windowed onto this output (InsList).",
                        "cloud_priority": "low",
                    },
                    "resolution": {"type": "string", "label": "Resolution", "cloud_priority": "low"},
                    "status": {"type": "string", "label": "Status", "cloud_priority": "low"},
                    "name": {"type": "string", "label": "Name", "cloud_priority": "low"},
                },
                "summary_fields": ["cut_to_black", "source_list"],
                "label_field": "name",
            },
        },
        "device_settings": {
            "device_name": {
                "type": "string",
                "label": "Device Name",
                "help": "Name shown for the unit (up to 32 characters).",
                "state_key": "device_name",
                "default": "",
                "setup": False,
            },
        },
        "quick_actions": ["recall_preset", "set_window_input", "refresh"],
        "actions": [
            {"id": "recall_preset", "kind": "command", "icon": "bookmark"},
            {"id": "set_window_input", "kind": "command", "icon": "route"},
            {"id": "take_storyboard", "kind": "command", "icon": "play"},
            {"id": "refresh", "kind": "command", "icon": "refresh-cw"},
            {
                "id": "test_login",
                "kind": "setup",
                "label": "Test Login",
                "icon": "key-round",
                "availability": "always",
            },
        ],
        "commands": {
            "set_window_input": {
                "label": "Set Window Source",
                "params": {
                    "window": {
                        "type": "child_id",
                        "child_type": "window",
                        "required": True,
                        "label": "Window",
                    },
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "required": True,
                        "label": "Input",
                    },
                },
                "help": "Route an input into a wall window (Window<n>.Input).",
            },
            "set_window_position": {
                "label": "Move Window",
                "params": {
                    "window": {
                        "type": "child_id",
                        "child_type": "window",
                        "required": True,
                        "label": "Window",
                    },
                    "x": {
                        "type": "integer",
                        "required": True,
                        "label": "X Centre",
                        "min": -8192,
                        "max": 8191,
                    },
                    "y": {
                        "type": "integer",
                        "required": True,
                        "label": "Y Centre",
                        "min": -8192,
                        "max": 8191,
                    },
                },
                "help": "Move a window's centre on its canvas (14-bit signed coordinates).",
            },
            "set_window_size": {
                "label": "Resize Window",
                "params": {
                    "window": {
                        "type": "child_id",
                        "child_type": "window",
                        "required": True,
                        "label": "Window",
                    },
                    "width": {
                        "type": "integer",
                        "required": True,
                        "label": "Width",
                        "min": 0,
                        "max": 16383,
                    },
                    "height": {
                        "type": "integer",
                        "required": True,
                        "label": "Height",
                        "min": 0,
                        "max": 16383,
                    },
                },
            },
            "set_window_zorder": {
                "label": "Set Window Z-Order",
                "params": {
                    "window": {
                        "type": "child_id",
                        "child_type": "window",
                        "required": True,
                        "label": "Window",
                    },
                    "zorder": {
                        "type": "integer",
                        "required": True,
                        "label": "Z-Order",
                        "min": 0,
                        "max": 15,
                    },
                },
                "help": "Depth within the canvas — higher values stack in front.",
            },
            "set_window_ftb": {
                "label": "Fade Window",
                "params": {
                    "window": {
                        "type": "child_id",
                        "child_type": "window",
                        "required": True,
                        "label": "Window",
                    },
                    "level": {
                        "type": "integer",
                        "required": True,
                        "label": "Fade Level",
                        "min": 0,
                        "max": 256,
                    },
                },
                "help": "Fade a window toward black: 0 = full brightness, 256 = black.",
            },
            "recall_preset": {
                "label": "Recall Preset",
                "params": {
                    "preset": {
                        "type": "integer",
                        "required": True,
                        "label": "Preset",
                        "min": 1,
                        "max": 49,
                        "options_state": "preset_options",
                    },
                },
                "help": "Take a stored wall preset (Preset.Take).",
            },
            "save_preset": {
                "label": "Save Preset",
                "params": {
                    "preset": {
                        "type": "integer",
                        "required": True,
                        "label": "Preset (1-49)",
                        "min": 1,
                        "max": 49,
                    },
                    "name": {
                        "type": "string",
                        "required": False,
                        "label": "Name (optional)",
                        "pattern": "^[A-Za-z0-9_-]{0,20}$",
                    },
                },
                "help": "Save the current wall to a preset slot, optionally naming it (20 alphanumeric characters max, no spaces).",
            },
            "take_storyboard": {
                "label": "Run Storyboard",
                "params": {
                    "storyboard": {
                        "type": "integer",
                        "required": True,
                        "label": "Storyboard",
                        "min": 1,
                        "options_state": "stbd_options",
                    },
                },
                "help": "Execute a stored storyboard animation (Stbds.Stbd<n>.Take).",
            },
            "set_canvas_volume": {
                "label": "Set Canvas Volume",
                "params": {
                    "canvas": {
                        "type": "child_id",
                        "child_type": "canvas",
                        "required": True,
                        "label": "Canvas",
                    },
                    "volume": {
                        "type": "integer",
                        "required": True,
                        "label": "Volume (%)",
                        "min": 0,
                        "max": 100,
                    },
                },
            },
            "canvas_mute_on": {
                "label": "Mute Canvas Audio",
                "params": {
                    "canvas": {
                        "type": "child_id",
                        "child_type": "canvas",
                        "required": True,
                        "label": "Canvas",
                    },
                },
            },
            "canvas_mute_off": {
                "label": "Unmute Canvas Audio",
                "params": {
                    "canvas": {
                        "type": "child_id",
                        "child_type": "canvas",
                        "required": True,
                        "label": "Canvas",
                    },
                },
            },
            "set_canvas_audio_mode": {
                "label": "Set Canvas Audio Mode",
                "params": {
                    "canvas": {
                        "type": "child_id",
                        "child_type": "canvas",
                        "required": True,
                        "label": "Canvas",
                    },
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "label": "Mode",
                        "values": ["FromSource", "FollowWindow"],
                    },
                },
                "help": "FromSource plays the canvas audio source; FollowWindow follows a window's source.",
            },
            "set_canvas_audio_source": {
                "label": "Set Canvas Audio Source",
                "params": {
                    "canvas": {
                        "type": "child_id",
                        "child_type": "canvas",
                        "required": True,
                        "label": "Canvas",
                    },
                    "source": {
                        "type": "string",
                        "required": True,
                        "label": "Input",
                        "options_state": "input_options",
                    },
                },
                "help": "Input whose audio the canvas plays when audio mode is FromSource.",
            },
            "set_canvas_audio_window": {
                "label": "Set Canvas Audio Window",
                "params": {
                    "canvas": {
                        "type": "child_id",
                        "child_type": "canvas",
                        "required": True,
                        "label": "Canvas",
                    },
                    "window": {
                        "type": "child_id",
                        "child_type": "window",
                        "required": True,
                        "label": "Window",
                    },
                },
                "help": "Window whose source audio the canvas plays when audio mode is FollowWindow.",
            },
            "cut_to_black_on": {
                "label": "Cut Output to Black",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                },
                "help": "Blank an output without changing the wall.",
            },
            "cut_to_black_off": {
                "label": "Restore Output from Black",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                },
            },
            "media_play": {
                "label": "Media: Play",
                "params": {
                    "input": {
                        "type": "string",
                        "required": True,
                        "label": "Media Input",
                        "options_state": "media_input_options",
                    },
                },
                "help": "Start / resume the Streaming Media input's play queue.",
            },
            "media_pause": {
                "label": "Media: Pause",
                "params": {
                    "input": {
                        "type": "string",
                        "required": True,
                        "label": "Media Input",
                        "options_state": "media_input_options",
                    },
                },
            },
            "media_stop": {
                "label": "Media: Stop",
                "params": {
                    "input": {
                        "type": "string",
                        "required": True,
                        "label": "Media Input",
                        "options_state": "media_input_options",
                    },
                },
            },
            "media_skip_forward": {
                "label": "Media: Next Item",
                "params": {
                    "input": {
                        "type": "string",
                        "required": True,
                        "label": "Media Input",
                        "options_state": "media_input_options",
                    },
                },
            },
            "media_skip_backward": {
                "label": "Media: Previous Item",
                "params": {
                    "input": {
                        "type": "string",
                        "required": True,
                        "label": "Media Input",
                        "options_state": "media_input_options",
                    },
                },
            },
            "media_load_playlist": {
                "label": "Media: Load Playlist",
                "params": {
                    "input": {
                        "type": "string",
                        "required": True,
                        "label": "Media Input",
                        "options_state": "media_input_options",
                    },
                    "playlist": {
                        "type": "string",
                        "required": True,
                        "label": "Playlist",
                        "options_state": "playlist_options",
                    },
                },
                "help": "Load a stored playlist into the input's play queue (does not auto-play).",
            },
            "media_play_mode": {
                "label": "Media: Play Mode",
                "params": {
                    "input": {
                        "type": "string",
                        "required": True,
                        "label": "Media Input",
                        "options_state": "media_input_options",
                    },
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "label": "Mode",
                        "values": ["Single", "Repeat"],
                    },
                },
                "help": "Single plays the queue once; Repeat loops it.",
            },
            "standby_on": {
                "label": "Enter Standby",
                "params": {},
                "help": (
                    "Put the unit into standby (System.StandbyMode) — "
                    "supported on the CORIOmaster micro; other models "
                    "answer !Failed harmlessly."
                ),
            },
            "standby_off": {
                "label": "Leave Standby",
                "params": {},
            },
            "set_device_name": {
                "label": "Set Device Name",
                "params": {
                    "name": {"type": "string", "required": True, "label": "Name"},
                },
                "help": "Set the unit's name (System.Unit_Description, 32 characters max).",
            },
            "refresh": {
                "label": "Refresh Status",
                "params": {},
                "help": "Re-enumerate cards, windows, and canvases and re-read all state.",
            },
            "raw_command": {
                "label": "Send Raw Command",
                "params": {
                    "command": {"type": "string", "required": True, "label": "Command"},
                },
                "help": (
                    "Escape hatch for anything in the CORIOmaster command "
                    "document, e.g. 'Window1.RotateDeg = 90' or "
                    "'Resources.Configs.Config1'. Returns the reply lines."
                ),
            },
        },
    }

    # ── Lifecycle ──

    def __init__(self, device_id: str, config: dict[str, Any], state, events):
        self._request_lock = asyncio.Lock()
        self._reply_lines: list[str] = []
        self._terminal_waiter: asyncio.Future | None = None
        self._preset_gather: dict[int, str] | None = None
        self._stbd_gather: dict[int, str] | None = None
        self._playlist_gather: dict[int, str] | None = None
        # Registered children: input/output alias ids, window/canvas ints.
        self._known: dict[str, set] = {
            "input": set(), "output": set(), "window": set(), "canvas": set(),
        }
        # Input aliases living on Streaming Media cards (media transport).
        self._media_inputs: set[str] = set()
        self._events_armed = False
        self._poll_cycle = 0
        self._poll_misses = 0
        self._roster_dirty = False
        self._presets_dirty = False
        super().__init__(device_id, config, state, events)

    async def connect(self) -> None:
        host = self.config.get("host", "")
        port = int(self.config.get("port", 10001))

        self.transport = await TCPTransport.create(
            host=host,
            port=port,
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            delimiter=b"\n",
            timeout=5.0,
            name=self.device_id,
        )

        try:
            await self._login()
            await self._read_unit_info()
            await self._enumerate_all()
            await self._arm_events()
        except Exception:
            await self._teardown_transport()
            raise

        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        log.info(f"[{self.device_id}] Connected to CORIOmaster at {host}:{port}")

        self._poll_cycle = 0
        self._poll_misses = 0
        await self.poll()
        poll_interval = int(self.config.get("poll_interval", 15))
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def disconnect(self) -> None:
        await self.stop_polling()
        await self._teardown_transport()
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info(f"[{self.device_id}] Disconnected")

    async def _teardown_transport(self) -> None:
        if self.transport:
            await self.transport.close()
            self.transport = None
        self._fail_terminal_waiter()

    # ── Login / session setup ──

    async def _login(self) -> None:
        """Send login( ) and require the documented confirmation line."""
        username = str(self.config.get("username", "admin")).strip() or "admin"
        password = str(self.config.get("password", ""))
        ok, lines = await self._request(
            f"login({username},{password})", timeout=LOGIN_TIMEOUT_S
        )
        if any(_RE_LOGIN_OK.match(ln) for ln in lines):
            return
        if ok is None:
            raise ConnectionError(
                f"[{self.device_id}] No response to login on the CORIOmax "
                f"CLI — check the unit is reachable and no other "
                f"controller holds the connection"
            )
        raise ConnectionError(
            f"[{self.device_id}] Authentication failed — the unit rejected "
            f"user '{username}'. Factory default is admin / adminpw."
        )

    async def _read_unit_info(self) -> None:
        ok, lines = await self._request("CORIOmax")
        if ok:
            name = num = ""
            for ln in lines:
                m = _RE_MODEL_NAME.match(ln)
                if m:
                    name = m.group(1)
                m = _RE_MODEL_NUM.match(ln)
                if m:
                    num = m.group(1)
            if name or num:
                self.set_state("model", f"{name} {num}".strip())

    async def _arm_events(self) -> None:
        """Subscribe to the event categories the driver folds into state.

        Subscriptions are per-connection, so this runs on every connect.
        Firmware without the event mechanism answers !Failed — noted once,
        and polling carries the load.
        """
        armed = 0
        for category in EVENT_CATEGORIES:
            ok, _lines = await self._request(f"AddEvents({category})")
            if ok:
                armed += 1
        self._events_armed = armed > 0
        if not self._events_armed:
            log.info(
                f"[{self.device_id}] Unit did not accept event "
                f"subscriptions (older firmware?) — running poll-only"
            )

    # ── Serialized request/response ──

    async def _request(
        self, line: str, timeout: float | None = None
    ) -> tuple[bool | None, list[str]]:
        """Send one CLI line and gather its reply.

        Returns (ok, reply_lines): ok is True on '!Done', False on
        '!Failed', None on timeout. Requests are serialized — the CLI
        answers in order on a single connection. !Event lines never count
        as reply lines (they interleave asynchronously).
        """
        if timeout is None:
            timeout = REQUEST_TIMEOUT_S
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        async with self._request_lock:
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._reply_lines = []
            self._terminal_waiter = fut
            try:
                await self.transport.send((line + "\r\n").encode("utf-8"))
                try:
                    ok = await asyncio.wait_for(fut, timeout)
                except asyncio.TimeoutError:
                    return None, list(self._reply_lines)
                return ok, list(self._reply_lines)
            finally:
                if self._terminal_waiter is fut:
                    self._terminal_waiter = None

    def _fail_terminal_waiter(self) -> None:
        waiter = self._terminal_waiter
        if waiter is not None and not waiter.done():
            waiter.set_exception(ConnectionError("connection closed"))
        self._terminal_waiter = None

    # ── Receiving ──

    async def on_data_received(self, data: bytes) -> None:
        line = data.decode("utf-8", errors="replace").strip("\r\n").strip()
        if not line:
            return
        m = _RE_EVENT.match(line)
        if m:
            # Asynchronous push — never part of a pending request's reply.
            self._apply_event(m.group(1))
            return
        self._apply_line(line)
        if self._terminal_waiter is not None:
            self._reply_lines.append(line)
            if self._terminal_waiter.done():
                return
            m = _RE_TERMINAL.match(line)
            if m:
                self._terminal_waiter.set_result(m.group(1).lower() == "done")
            elif _RE_LOGIN_OK.match(line):
                # login( ) answers the documented '!Info : ... Logged In'
                # confirmation with no !Done terminal — treat it as one.
                self._terminal_waiter.set_result(True)

    # ── Event handling ──

    def _apply_event(self, body: str) -> None:
        """Fold one '!Event <body>' push into state.

        Documented shapes (tokens may carry stray spaces — the doc's own
        examples do): 'WINDOW,INPUT,Window1,Slot5.In1',
        'PRESET,TAKE,1', 'CANVAS,PROPERTY_CHANGED,Canvas1,AudioMute,On',
        'CANVAS,STBDCURRENT_CHANGED,Canvas1,Stbd1',
        'OUTPUT,PROPERTY_CHANGED,Slot16.Out1,CutToBlack,On',
        'INPUT,STATUS_GROUP,Slot1.In1,Status,OK',
        'MEDIA_PLAYER,STATUS_UPDATE,Slot4.In1,Playing,3'. Some module
        tables print the same events without the category token — the
        parser keys on the event name when the first token isn't a known
        category.
        """
        tokens = [t.strip() for t in body.split(",")]
        if not tokens:
            return
        category = tokens[0].upper()
        if category in ("WINDOW", "PRESET", "CANVAS", "OUTPUT", "INPUT",
                        "STBD", "MEDIA_PLAYER", "HDMI", "SYSTEM"):
            event, args = (tokens[1].upper() if len(tokens) > 1 else ""), tokens[2:]
        else:
            # Category omitted (module-table shape): first token is the event.
            event, args = category, tokens[1:]

        try:
            self._dispatch_event(event, args)
        except (ValueError, IndexError, KeyError) as exc:
            log.debug(f"[{self.device_id}] Unparsed event '{body}': {exc}")

    def _dispatch_event(self, event: str, args: list[str]) -> None:
        if event == "INPUT" and len(args) >= 2:
            # WINDOW,INPUT,<window>,<input>
            num = _win_ref(args[0])
            if num is None:
                return
            if num not in self._known["window"]:
                self._roster_dirty = True
                return
            self.set_child_state_batch(
                "window", num, {"input": _normalize_source(args[1])}
            )
        elif event in ("TAKE", "COMPLETE") and len(args) >= 1:
            # PRESET,TAKE,<n> / PRESET,COMPLETE,<n>
            self.set_state("active_preset", int(args[0]))
        elif event in ("SAVE", "REMOVE"):
            self._presets_dirty = True
        elif event == "PROPERTY_CHANGED" and len(args) >= 3:
            self._apply_property_event(args[0], args[1], args[2])
        elif event == "STBDCURRENT_CHANGED" and len(args) >= 1:
            num = self._canvas_ref(args[0])
            if num is not None and num in self._known["canvas"]:
                current = args[1] if len(args) > 1 else ""
                if current.upper() == "NULL":
                    current = ""
                self.set_child_state_batch(
                    "canvas", num, {"current_storyboard": current}
                )
        elif event == "STATUS_GROUP" and len(args) >= 3:
            self._apply_status_group(args[0], args[1], args[2])
        elif event == "STATUS_UPDATE" and len(args) >= 2:
            # MEDIA_PLAYER,STATUS_UPDATE,<input>,<state>,<index>
            ref = _port_ref(args[0])
            if not ref or ref[0] != "in":
                return
            alias = _alias("in", ref[1], ref[2])
            if alias not in self._media_inputs:
                return
            updates: dict[str, Any] = {"media_status": args[1]}
            if len(args) > 2 and args[2].isdigit():
                updates["media_item"] = int(args[2])
            self.set_child_state_batch("input", alias, updates)
        # ITEM_STATUS_CHANGED, ISCURRENT_CHANGED, HDMI sink events, and the
        # module/system housekeeping events carry nothing the state model
        # mirrors — ignored.

    @staticmethod
    def _canvas_ref(value: str) -> int | None:
        m = re.fullmatch(r"(?i)(?:canvas)?(\d+)", str(value).strip())
        return int(m.group(1)) if m else None

    def _apply_property_event(self, target: str, prop: str, value: str) -> None:
        """CANVAS/OUTPUT PROPERTY_CHANGED events."""
        num = self._canvas_ref(target) if target.lower().startswith("canvas") else None
        if num is not None:
            if num not in self._known["canvas"]:
                self._roster_dirty = True
                return
            updates = self._canvas_prop_updates(prop, value)
            if updates:
                self.set_child_state_batch("canvas", num, updates)
            return
        ref = _port_ref(target)
        if ref and ref[0] == "out":
            alias = _alias("out", ref[1], ref[2])
            if alias in self._known["output"] and prop.lower() == "cuttoblack":
                self.set_child_state_batch(
                    "output", alias, {"cut_to_black": value.lower() == "on"}
                )
            # AudioMute / AudioEnable on S/PDIF outputs aren't modeled.

    def _apply_status_group(self, target: str, prop: str, value: str) -> None:
        """INPUT/OUTPUT STATUS_GROUP events."""
        ref = _port_ref(target)
        if not ref:
            return
        alias = _alias(ref[0], ref[1], ref[2])
        prop_l = prop.lower()
        if ref[0] == "in" and alias in self._known["input"]:
            key = {
                "status": "status",
                "measured_resolution": "resolution",
                "hdmi": "hdmi",
                "audio": "audio",
            }.get(prop_l)
            if key:
                self.set_child_state_batch("input", alias, {key: value})
        elif ref[0] == "out" and alias in self._known["output"]:
            if prop_l == "status":
                self.set_child_state_batch("output", alias, {"status": value})

    @staticmethod
    def _canvas_prop_updates(prop: str, value: str) -> dict[str, Any]:
        prop_l = prop.lower()
        if prop_l == "audiomute":
            return {"audio_mute": value.lower() == "on"}
        if prop_l == "audiomode":
            return {"audio_mode": value}
        if prop_l == "audiovolume":
            return {"audio_volume": int(value)} if value.lstrip("-").isdigit() else {}
        if prop_l == "audiosource":
            return {"audio_source": _normalize_source(value)}
        if prop_l == "audiofollowwindow":
            num = _win_ref(value)
            return {"audio_follow_window": num} if num is not None else {}
        return {}

    # ── Reply-line state folding ──

    def _apply_line(self, line: str) -> None:
        """Fold one reply line into device / child state.

        A '!Done <echo>' terminal for a property WRITE restates the applied
        value ('!Done Window1.Input = Slot4.In1'), so the prefix is
        stripped and the remainder run through the same property matchers.
        """
        line = re.sub(r"(?i)^!done\s+", "", line)

        m = _RE_QUEUE_PROP.match(line)
        if m:
            alias = _alias("in", int(m.group(1)), int(m.group(2)))
            if alias in self._media_inputs:
                prop, value = m.group(3).lower(), m.group(4)
                if prop == "status":
                    self.set_child_state_batch("input", alias, {"media_status": value})
                elif prop == "currentindex" and value.isdigit():
                    self.set_child_state_batch("input", alias, {"media_item": int(value)})
            return
        m = _RE_PORT_PROP.match(line)
        if m:
            self._apply_port_prop(
                "in" if m.group(2).lower() == "in" else "out",
                int(m.group(1)), int(m.group(3)), m.group(4), m.group(5),
            )
            return
        m = _RE_WIN_PROP.match(line)
        if m:
            num = int(m.group(1))
            if num in self._known["window"]:
                updates = self._window_prop_updates(m.group(2), m.group(3))
                if updates:
                    self.set_child_state_batch("window", num, updates)
            return
        m = _RE_CANVAS_PROP.match(line)
        if m:
            num = int(m.group(1))
            if num in self._known["canvas"]:
                self._apply_canvas_line(num, m.group(2), m.group(3))
            return
        m = _RE_STBD_NAME.match(line)
        if m and self._stbd_gather is not None:
            self._stbd_gather[int(m.group(1))] = m.group(2)
            return
        m = _RE_PLAYLIST_NAME.match(line)
        if m and self._playlist_gather is not None:
            self._playlist_gather[int(m.group(1))] = m.group(2)
            return
        m = _RE_PRESET_TAKE.match(line)
        if m:
            self.set_state("active_preset", int(m.group(1)))
            return
        m = _RE_PRESET_LIST.match(line)
        if m and self._preset_gather is not None:
            # "PresetList[3]=top_and_bottom,Canvas1,1000" — name is field 1.
            name = m.group(2).split(",")[0].strip()
            self._preset_gather[int(m.group(1))] = name
            return
        m = _RE_SYS_STATUS.match(line)
        if m:
            self.set_state("status", m.group(1))
            return
        m = _RE_API_VER.match(line)
        if m:
            self.set_state("api_version", m.group(1))
            return
        m = _RE_UNIT_DESC.match(line)
        if m:
            self.set_state("device_name", m.group(1))
            return
        m = _RE_STANDBY.match(line)
        if m:
            self.set_state("standby", m.group(1).lower() == "on")
            return
        m = _RE_SW_VER.match(line)
        if m:
            self.set_state("firmware_version", m.group(1))
            return

    def _apply_port_prop(
        self, kind: str, slot: int, port: int, prop: str, value: str
    ) -> None:
        alias = _alias(kind, slot, port)
        prop_l = prop.lower()
        if kind == "in":
            if alias not in self._known["input"]:
                return
            key_map = {
                "status": ("status", str),
                "measured_resolution": ("resolution", str),
                "hdmi": ("hdmi", str),
                "audio": ("audio", str),
                "alias": ("name", str),
            }
            entry = key_map.get(prop_l)
            if entry:
                key, _ = entry
                if key == "name" and value.upper() == "NULL":
                    value = alias
                self.set_child_state_batch("input", alias, {key: value})
        else:
            if alias not in self._known["output"]:
                return
            if prop_l == "inslist":
                self.set_child_state_batch(
                    "output", alias, {"source_list": _normalize_source(value)}
                )
            elif prop_l == "cuttoblack":
                self.set_child_state_batch(
                    "output", alias, {"cut_to_black": value.lower() == "on"}
                )
            elif prop_l == "status":
                self.set_child_state_batch("output", alias, {"status": value})
            elif prop_l == "resolution":
                self.set_child_state_batch("output", alias, {"resolution": value})
            elif prop_l == "alias":
                if value.upper() == "NULL":
                    value = alias
                self.set_child_state_batch("output", alias, {"name": value})

    def _window_prop_updates(self, prop: str, value: str) -> dict[str, Any]:
        prop_l = prop.lower()
        if prop_l == "input":
            return {"input": _normalize_source(value)}
        if prop_l == "canvas":
            return {"canvas": "" if value.upper() == "NULL" else value}
        if prop_l == "status":
            return {"status": value}
        if prop_l == "alias":
            return {} if value.upper() == "NULL" else {"name": value}
        if prop_l == "fullname":
            # Fallback label — Alias (parsed after FullName in dumps) wins
            # when set because it overwrites name.
            return {}
        int_map = {
            "canxcentre": "x", "canycentre": "y",
            "canwidth": "width", "canheight": "height",
            "zorder": "zorder", "ftb": "ftb",
        }
        key = int_map.get(prop_l)
        if key and value.lstrip("-").isdigit():
            return {key: int(value)}
        return {}

    def _apply_canvas_line(self, num: int, prop: str, value: str) -> None:
        prop_l = prop.lower()
        if prop_l == "stbdcurrent":
            current = "" if value.upper() == "NULL" else value
            self.set_child_state_batch(
                "canvas", num, {"current_storyboard": current}
            )
            return
        if prop_l == "windowlist":
            wl = "" if value.upper() == "NULL" else value
            self.set_child_state_batch("canvas", num, {"window_list": wl})
            return
        if prop_l == "alias":
            if value.upper() != "NULL":
                self.set_child_state_batch("canvas", num, {"name": value})
            return
        updates = self._canvas_prop_updates(prop, value)
        if updates:
            self.set_child_state_batch("canvas", num, updates)

    # ── Roster enumeration ──

    async def _enumerate_all(self) -> None:
        await self._enumerate_slots()
        await self._enumerate_windows_canvases()

    async def _enumerate_slots(self) -> None:
        """Discover installed cards; register a child per port.

        'Slots' answers one line per slot ('Slots.Slot4 = NO CARD' for the
        empty ones); each installed slot's dump lists Cardtype and its
        In<n>/Out<n> port stubs. Inputs on a Streaming Media card
        additionally get the media playback props (dynamic schema).
        """
        ok, lines = await self._request("Slots")
        if not ok:
            raise ConnectionError(
                f"[{self.device_id}] The unit did not answer the Slots "
                f"enumeration — cannot discover installed cards"
            )
        installed: list[int] = []
        for ln in lines:
            m = _RE_SLOT_LIST.match(ln)
            if m and "NO CARD" not in m.group(2).upper():
                installed.append(int(m.group(1)))

        found: dict[str, set[str]] = {"input": set(), "output": set()}
        media: set[str] = set()
        for slot in installed:
            ok, lines = await self._request(f"Slot{slot}")
            if not ok:
                continue
            is_media = any(
                (m := _RE_CARDTYPE.match(ln)) and "MEDIA" in m.group(2).upper()
                for ln in lines
            )
            for ln in lines:
                m = _RE_PORT_STUB.match(ln)
                if not m:
                    continue
                kind = "in" if m.group(2).lower() == "in" else "out"
                ctype = "input" if kind == "in" else "output"
                alias = _alias(kind, int(m.group(1)), int(m.group(3)))
                found[ctype].add(alias)
                if kind == "in" and is_media:
                    media.add(alias)

        for ctype in ("input", "output"):
            want = found[ctype]
            have = set(self.list_children(ctype))
            for alias in sorted(want - have):
                if ctype == "input" and alias in media:
                    self.register_child(
                        ctype, alias,
                        initial_state={"name": alias},
                        schema={**_INPUT_PROPS, **_MEDIA_PROPS},
                    )
                else:
                    self.register_child(ctype, alias, initial_state={"name": alias})
            for alias in have - want:
                self.deregister_child(ctype, alias)
            self._known[ctype] = want
        self._media_inputs = media

        self.set_state("input_options", json.dumps(
            [{"value": a, "label": a} for a in sorted(found["input"])]
        ))
        self.set_state("media_input_options", json.dumps(
            [{"value": a, "label": a} for a in sorted(media)]
        ))
        log.info(
            f"[{self.device_id}] Enumerated {len(found['input'])} inputs / "
            f"{len(found['output'])} outputs across slots {installed}"
            + (f" ({len(media)} media)" if media else "")
        )

    async def _enumerate_windows_canvases(self) -> None:
        """Register the windows and canvases that are actually in use.

        'Windows' / 'Canvases' list every table slot the frame supports;
        entries whose Status is FREE are unconfigured placeholders and are
        not registered (a C3-540 has 56 window slots — the wall built in
        CORIOgrapher uses a handful). An event referencing an unknown
        window marks the roster dirty and the next poll re-enumerates.
        """
        for ctype, list_cmd, stub_re in (
            ("window", "Windows", _RE_WIN_STUB),
            ("canvas", "Canvases", _RE_CANVAS_STUB),
        ):
            ok, lines = await self._request(list_cmd)
            if not ok:
                continue
            nums = [int(m.group(1)) for ln in lines if (m := stub_re.match(ln))]
            active: set[int] = set()
            for num in nums:
                name = f"{'Window' if ctype == 'window' else 'Canvas'}{num}"
                ok, lines = await self._request(name)
                if not ok:
                    continue
                status = ""
                for ln in lines:
                    m = re.match(
                        rf"(?i)^(?:routing\.)?(?:windows\.|canvases\.)?"
                        rf"{name}\.status\s*=\s*(.+?)\s*$", ln
                    )
                    if m:
                        status = m.group(1).strip().upper()
                        break
                if status and status != "FREE":
                    active.add(num)

            have = set(self.list_children(ctype))
            for num in sorted(active - have):
                label = f"{'Window' if ctype == 'window' else 'Canvas'} {num}"
                self.register_child(ctype, num, initial_state={"name": label})
            for num in have - active:
                self.deregister_child(ctype, num)
            self._known[ctype] = active

        log.info(
            f"[{self.device_id}] Registered {len(self._known['window'])} "
            f"windows / {len(self._known['canvas'])} canvases"
        )

    async def refresh_children(self) -> dict[str, Any]:
        await self._enumerate_all()
        await self._full_read()
        return {
            "windows": len(self._known["window"]),
            "canvases": len(self._known["canvas"]),
            "inputs": len(self._known["input"]),
            "outputs": len(self._known["output"]),
        }

    # ── Polling ──

    async def poll(self) -> None:
        """Resync state; every 6th cycle does the full sweep.

        Steady cycles re-read the windows, canvases, and active preset
        (events carry changes in between; these reads double as the
        session keep-alive). The full sweep adds ports, pickers, system
        info, and roster reconciliation. Two consecutive cycles with no
        answered request raise so the platform flips the device offline —
        a hung controller keeps the TCP socket open.
        """
        answered = 0

        if self._roster_dirty:
            self._roster_dirty = False
            await self._enumerate_windows_canvases()

        full = self._poll_cycle % 6 == 0
        answered += await self._read_windows_canvases()
        ok, _ = await self._request("Preset.Take")
        answered += ok is not None
        if full:
            answered += await self._read_ports()
            answered += await self._read_system()
            await self._refresh_pickers()
        elif self._presets_dirty:
            await self._refresh_preset_list()
        self._poll_cycle += 1

        if answered == 0:
            self._poll_misses += 1
            if self._poll_misses >= 2:
                raise ConnectionError(
                    f"[{self.device_id}] Unit is not responding on the "
                    f"CORIOmax CLI (connection open but silent)"
                )
        else:
            self._poll_misses = 0

    async def _read_windows_canvases(self) -> int:
        answered = 0
        for num in sorted(self._known["window"]):
            ok, _ = await self._request(f"Window{num}")
            answered += ok is not None
        for num in sorted(self._known["canvas"]):
            ok, _ = await self._request(f"Canvas{num}")
            answered += ok is not None
        return answered

    async def _read_ports(self) -> int:
        answered = 0
        for alias in sorted(self._known["input"] | self._known["output"]):
            ok, _ = await self._request(_long_form(alias) or alias)
            answered += ok is not None
        for alias in sorted(self._media_inputs):
            base = _long_form(alias)
            for prop in ("Status", "CurrentIndex"):
                ok, _ = await self._request(f"{base}.ActiveQueue.{prop}")
                answered += ok is not None
        return answered

    async def _read_system(self) -> int:
        answered = 0
        for query in ("System.Status", "System.API_Version",
                      "System.Unit_Description", "System.StandbyMode"):
            ok, _ = await self._request(query)
            answered += ok is not None
        return answered

    async def _full_read(self) -> None:
        await self._read_windows_canvases()
        await self._request("Preset.Take")
        await self._read_ports()
        await self._read_system()
        await self._refresh_pickers()

    # ── Pickers ──

    async def _refresh_pickers(self) -> None:
        await self._refresh_preset_list()
        await self._refresh_storyboards()
        if self._media_inputs:
            await self._refresh_playlists()

    async def _refresh_preset_list(self) -> None:
        self._presets_dirty = False
        self._preset_gather = {}
        try:
            ok, _lines = await self._request("Preset.PresetList()")
            if ok:
                options = [
                    {"value": str(pid), "label": f"{pid}: {name}" if name else str(pid)}
                    for pid, name in sorted(self._preset_gather.items())
                ]
                self.set_state("preset_options", json.dumps(options))
        finally:
            self._preset_gather = None

    async def _refresh_storyboards(self) -> None:
        """Aggregate storyboard names into the Run Storyboard picker.

        'Stbds' dumps every storyboard's properties including its Name
        line; unnamed slots are skipped.
        """
        self._stbd_gather = {}
        try:
            ok, _lines = await self._request("Stbds")
            if ok:
                options = [
                    {"value": str(num), "label": f"{num}: {name}" if name else str(num)}
                    for num, name in sorted(self._stbd_gather.items())
                    if name and name.upper() != "NULL"
                ]
                self.set_state("stbd_options", json.dumps(options))
        finally:
            self._stbd_gather = None

    async def _refresh_playlists(self) -> None:
        """Aggregate playlist names for the Load Playlist picker.

        The Playlists dump lists Playlist<n> stubs; each name is read
        individually. A playlist with no name is documented as empty and
        skipped.
        """
        ok, lines = await self._request("Resources.Playlists")
        if not ok:
            return
        nums = [int(m.group(1)) for ln in lines if (m := _RE_PLAYLIST_STUB.match(ln))]
        self._playlist_gather = {}
        try:
            for num in nums:
                await self._request(f"Resources.Playlists.Playlist{num}.Name")
            options = [
                {"value": f"Resources.Playlists.Playlist{num}", "label": name}
                for num, name in sorted(self._playlist_gather.items())
                if name and name.upper() != "NULL"
            ]
            self.set_state("playlist_options", json.dumps(options))
        finally:
            self._playlist_gather = None

    # ── Commands ──

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}

        if command == "set_window_input":
            src = _long_form(params.get("input"))
            num = _win_ref(params.get("window"))
            if not src or num is None:
                raise ValueError(
                    f"set_window_input: window must be a window number and "
                    f"input an alias like s4i1 (got {params.get('window')!r}, "
                    f"{params.get('input')!r})"
                )
            ok, lines = await self._request(f"Window{num}.Input = {src}")
            self._warn_failed(command, ok, lines)
            return ok

        if command == "set_window_position":
            num = self._require_window(params)
            ok1, lines = await self._request(
                f"Window{num}.CanXCentre = {int(params['x'])}"
            )
            self._warn_failed(command, ok1, lines)
            ok2, lines = await self._request(
                f"Window{num}.CanYCentre = {int(params['y'])}"
            )
            self._warn_failed(command, ok2, lines)
            return bool(ok1) and bool(ok2)

        if command == "set_window_size":
            num = self._require_window(params)
            ok1, lines = await self._request(
                f"Window{num}.CanWidth = {int(params['width'])}"
            )
            self._warn_failed(command, ok1, lines)
            ok2, lines = await self._request(
                f"Window{num}.CanHeight = {int(params['height'])}"
            )
            self._warn_failed(command, ok2, lines)
            return bool(ok1) and bool(ok2)

        if command == "set_window_zorder":
            num = self._require_window(params)
            ok, lines = await self._request(
                f"Window{num}.Zorder = {int(params['zorder'])}"
            )
            self._warn_failed(command, ok, lines)
            return ok

        if command == "set_window_ftb":
            num = self._require_window(params)
            ok, lines = await self._request(
                f"Window{num}.FTB = {int(params['level'])}"
            )
            self._warn_failed(command, ok, lines)
            return ok

        if command == "recall_preset":
            ok, lines = await self._request(f"Preset.Take = {int(params['preset'])}")
            self._warn_failed(command, ok, lines)
            # The wall changed wholesale — re-read windows and canvases.
            await self._read_windows_canvases()
            return ok

        if command == "save_preset":
            pid = int(params["preset"])
            name = str(params.get("name") or "").strip()
            ok, lines = await self._request(f"Preset.Read = {pid}")
            self._warn_failed(command, ok, lines)
            if name:
                ok, lines = await self._request(f"Preset.NameRead = {name}")
                self._warn_failed(command, ok, lines)
            ok, lines = await self._request("Preset.SaveRead()")
            self._warn_failed(command, ok, lines)
            await self._refresh_preset_list()
            return ok

        if command == "take_storyboard":
            num = int(params["storyboard"])
            ok, lines = await self._request(f"Stbds.Stbd{num}.Take()")
            self._warn_failed(command, ok, lines)
            return ok

        if command == "set_canvas_volume":
            num = self._require_canvas(params)
            ok, lines = await self._request(
                f"Canvas{num}.AudioVolume = {int(params['volume'])}"
            )
            self._warn_failed(command, ok, lines)
            return ok

        if command in ("canvas_mute_on", "canvas_mute_off"):
            num = self._require_canvas(params)
            value = "On" if command.endswith("_on") else "Off"
            ok, lines = await self._request(f"Canvas{num}.AudioMute = {value}")
            self._warn_failed(command, ok, lines)
            return ok

        if command == "set_canvas_audio_mode":
            num = self._require_canvas(params)
            ok, lines = await self._request(
                f"Canvas{num}.AudioMode = {params['mode']}"
            )
            self._warn_failed(command, ok, lines)
            return ok

        if command == "set_canvas_audio_source":
            num = self._require_canvas(params)
            src = _long_form(params.get("source"))
            if not src:
                raise ValueError(
                    f"set_canvas_audio_source: source must be an input "
                    f"alias like s4i1 (got {params.get('source')!r})"
                )
            ok, lines = await self._request(f"Canvas{num}.AudioSource = {src}")
            self._warn_failed(command, ok, lines)
            return ok

        if command == "set_canvas_audio_window":
            num = self._require_canvas(params)
            win = _win_ref(params.get("window"))
            if win is None:
                raise ValueError("set_canvas_audio_window: window must be a window number")
            ok, lines = await self._request(
                f"Canvas{num}.AudioFollowWindow = {win}"
            )
            self._warn_failed(command, ok, lines)
            return ok

        if command in ("cut_to_black_on", "cut_to_black_off"):
            dst = _long_form(params.get("output"))
            if not dst:
                raise ValueError(f"{command}: output must be an alias like s14o1")
            value = "On" if command.endswith("_on") else "Off"
            ok, lines = await self._request(f"{dst}.CutToBlack = {value}")
            self._warn_failed(command, ok, lines)
            return ok

        if command in ("media_play", "media_pause", "media_stop",
                       "media_skip_forward", "media_skip_backward"):
            base = self._require_media_input(params)
            method = {
                "media_play": "Play",
                "media_pause": "Pause",
                "media_stop": "Stop",
                "media_skip_forward": "SkipForward",
                "media_skip_backward": "SkipBackward",
            }[command]
            ok, lines = await self._request(f"{base}.ActiveQueue.{method}()")
            self._warn_failed(command, ok, lines)
            return ok

        if command == "media_load_playlist":
            base = self._require_media_input(params)
            playlist = str(params.get("playlist", "")).strip()
            if not playlist.lower().startswith("resources.playlists."):
                playlist = f"Resources.Playlists.{playlist}"
            ok, lines = await self._request(
                f'{base}.ActiveQueue.LoadPlayList("{playlist}")'
            )
            self._warn_failed(command, ok, lines)
            return ok

        if command == "media_play_mode":
            base = self._require_media_input(params)
            ok, lines = await self._request(
                f"{base}.ActiveQueue.PlayMode = {params['mode']}"
            )
            self._warn_failed(command, ok, lines)
            return ok

        if command in ("standby_on", "standby_off"):
            value = "On" if command.endswith("_on") else "Off"
            ok, lines = await self._request(f"System.StandbyMode = {value}")
            self._warn_failed(command, ok, lines)
            await self._request("System.StandbyMode")
            return ok

        if command == "set_device_name":
            name = str(params.get("name", "")).strip()[:32]
            ok, lines = await self._request(f'System.Unit_Description = "{name}"')
            self._warn_failed(command, ok, lines)
            await self._request("System.Unit_Description")
            return ok

        if command == "refresh":
            await self.refresh_children()
            return True

        if command == "raw_command":
            ok, lines = await self._request(str(params.get("command", "")).strip())
            return "\n".join(lines)

        log.warning(f"[{self.device_id}] Unknown command: {command}")
        return None

    @staticmethod
    def _require_window(params: dict[str, Any]) -> int:
        num = _win_ref(params.get("window"))
        if num is None:
            raise ValueError(f"window must be a window number (got {params.get('window')!r})")
        return num

    @staticmethod
    def _require_canvas(params: dict[str, Any]) -> int:
        m = re.fullmatch(r"(?i)(?:canvas)?(\d+)", str(params.get("canvas", "")).strip())
        if not m:
            raise ValueError(f"canvas must be a canvas number (got {params.get('canvas')!r})")
        return int(m.group(1))

    def _require_media_input(self, params: dict[str, Any]) -> str:
        alias = str(params.get("input", "")).strip()
        base = _long_form(alias)
        if not base:
            raise ValueError(
                f"input must be a media input alias like s2i1 (got {alias!r})"
            )
        if self._media_inputs and alias.lower() not in {
            a.lower() for a in self._media_inputs
        }:
            raise ValueError(
                f"{alias} is not a Streaming Media input on this unit"
            )
        return base

    def _warn_failed(self, command: str, ok: bool | None, lines: list[str]) -> None:
        if ok is False:
            log.warning(
                f"[{self.device_id}] {command}: unit answered "
                f"{lines[-1] if lines else '!Failed'}"
            )
        elif ok is None:
            log.warning(f"[{self.device_id}] {command}: no reply from unit")

    # ── Device settings ──

    async def set_device_setting(self, setting: str, value: Any) -> None:
        if setting == "device_name":
            await self.send_command("set_device_name", {"name": str(value)})
            return
        raise ValueError(f"Unknown device setting: {setting}")

    # ── Setup action ──

    async def run_setup_action(
        self, action_id: str, params: dict[str, Any], progress: Any
    ) -> dict[str, Any]:
        if action_id != "test_login":
            raise ValueError(f"Unknown setup action: {action_id}")

        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", 10001))
        username = str(self.config.get("username", "admin")).strip() or "admin"
        password = str(self.config.get("password", ""))
        if not host:
            raise ValueError("No IP address configured")

        await progress(f"Connecting to {host}:{port}…", 20)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=5.0
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise ConnectionError(
                f"Could not reach a CORIOmax CLI on {host}:{port} ({exc})."
            ) from exc

        try:
            await progress("Logging in…", 55)
            writer.write(f"login({username},{password})\r\n".encode())
            await writer.drain()
            deadline = asyncio.get_running_loop().time() + LOGIN_TIMEOUT_S
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise ConnectionError(
                        "No login confirmation from the unit — is another "
                        "controller connected? The CORIOmaster accepts one "
                        "control connection at a time."
                    )
                try:
                    raw = await asyncio.wait_for(reader.readline(), timeout=remaining)
                except asyncio.TimeoutError:
                    continue
                if not raw:
                    raise ConnectionError("The unit closed the connection during login.")
                line = raw.decode("utf-8", errors="replace").strip()
                if _RE_LOGIN_OK.match(line):
                    break
                if _RE_TERMINAL.match(line):
                    raise ConnectionError(
                        f"The unit rejected user '{username}' ({line}). "
                        f"Factory default is admin / adminpw."
                    )
            await progress("Login OK — reading unit info…", 80)
            writer.write(b"CORIOmax.Model_Name\r\n")
            await writer.drain()
            model = ""
            try:
                for _ in range(4):
                    raw = await asyncio.wait_for(reader.readline(), timeout=2.0)
                    line = raw.decode("utf-8", errors="replace").strip()
                    m = _RE_MODEL_NAME.match(line)
                    if m:
                        model = m.group(1)
                    if _RE_TERMINAL.match(line):
                        break
            except asyncio.TimeoutError:
                pass
            return {
                "success": True,
                "message": (
                    f"Logged in to {host} as {username}"
                    + (f" ({model})" if model else "")
                ),
            }
        finally:
            writer.close()
