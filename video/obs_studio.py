"""
OpenAVC OBS Studio Driver.

Controls OBS Studio through obs-websocket, the WebSocket server built into
OBS since version 28: program and preview scene switching, studio mode and
the transition (kind, duration, T-bar), streaming, recording (start, stop,
pause, split, chapter markers), the virtual camera, the replay buffer, every
input's mute, volume, balance, sync offset and monitoring, media source
playback, text source contents, browser source refresh, scene item
visibility, source filters, any other output OBS exposes (NDI, Decklink,
aux outputs), scene collections, profiles, hotkeys, projectors and
screenshots. Live state covers all of it plus OBS's own statistics (CPU,
memory, disk, frame rate, dropped frames).

Why Python
----------
obs-websocket is JSON-RPC over a persistent WebSocket: a Hello / Identify
handshake with a SHA-256 challenge, requests correlated to responses by a
request id, request batches, and an event stream on the same socket. OBS
enumerates its own scenes, inputs, scene items, filters and outputs, and
they change while a show runs, so the rosters are child entities built at
connect and kept current from the events. None of that fits the declarative
``.avcdriver`` request/response model or the four ``push:`` shapes, so this
driver owns a ``websockets`` connection.

Push vs poll
------------
Hybrid. obs-websocket pushes an event for every change the driver cares
about (scene switches, stream, record, virtual camera and replay buffer
state, input mute, volume, balance, sync offset, monitoring, active and
showing state, media playback, scene item visibility, filter state, scene,
input and filter creation, removal and renames, transition changes, studio
mode, scene collection and profile changes), so a change made in OBS lands
in state at once. Polling (``poll_interval``, default 5 s) covers what is not
evented: the OBS statistics, stream and record timecodes and byte counts,
media source cursors and the plain output list. Input level meters are a
high-volume event (every 50 ms) and are subscribed only when
``enable_meters`` is on.

Authentication
--------------
OBS sends a salt and a challenge in its Hello; the driver answers with
base64(sha256(base64(sha256(password + salt)) + challenge)). A rejected
password closes the socket with code 4009, which the driver reports as a
typed ``auth_failed`` fault so the platform waits for a new password instead
of retrying. When OBS requires a password and none is configured, the
driver refuses before sending anything.

Sources (all public, from the OBS Project):
  obs-websocket 5.x protocol reference (the source of record)
      https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md
  The same reference as machine-readable JSON
      https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.json
  OBS Studio's WebSocket server settings (Tools menu)
      https://obsproject.com/kb/remote-control-guide
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re
from typing import Any

import websockets

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.utils.logger import get_logger

log = get_logger(__name__)

RPC_VERSION = 1
SUBPROTOCOL = "obswebsocket.json"

# WebSocketOpCode
OP_HELLO = 0
OP_IDENTIFY = 1
OP_IDENTIFIED = 2
OP_REIDENTIFY = 3
OP_EVENT = 5
OP_REQUEST = 6
OP_REQUEST_RESPONSE = 7
OP_REQUEST_BATCH = 8
OP_REQUEST_BATCH_RESPONSE = 9

# WebSocketCloseCode
CLOSE_AUTHENTICATION_FAILED = 4009
CLOSE_UNSUPPORTED_RPC_VERSION = 4010
CLOSE_SESSION_INVALIDATED = 4011

# EventSubscription bits
SUB_GENERAL = 1 << 0
SUB_CONFIG = 1 << 1
SUB_SCENES = 1 << 2
SUB_INPUTS = 1 << 3
SUB_TRANSITIONS = 1 << 4
SUB_FILTERS = 1 << 5
SUB_OUTPUTS = 1 << 6
SUB_SCENE_ITEMS = 1 << 7
SUB_MEDIA_INPUTS = 1 << 8
SUB_UI = 1 << 10
SUB_INPUT_VOLUME_METERS = 1 << 16
SUB_INPUT_ACTIVE_STATE = 1 << 17
SUB_INPUT_SHOW_STATE = 1 << 18

BASE_SUBSCRIPTIONS = (
    SUB_GENERAL | SUB_CONFIG | SUB_SCENES | SUB_INPUTS | SUB_TRANSITIONS
    | SUB_FILTERS | SUB_OUTPUTS | SUB_SCENE_ITEMS | SUB_MEDIA_INPUTS | SUB_UI
    | SUB_INPUT_ACTIVE_STATE | SUB_INPUT_SHOW_STATE
)

# RequestStatus codes the driver reads by meaning.
STATUS_SUCCESS = 100
STATUS_NOT_READY = 207
STATUS_INVALID_RESOURCE_STATE = 604
STATUS_RESOURCE_NOT_FOUND = 600

# What a RequestStatus code means when OBS sends no comment with it.
STATUS_TEXT = {
    203: "the request had no type", 204: "OBS does not know this request",
    205: "OBS reported a general error", 207: "OBS is not ready",
    300: "a required field was missing", 301: "the request data was missing",
    400: "a field was invalid", 401: "a field had the wrong type",
    402: "a value was out of range", 403: "a required field was empty",
    500: "the output is already active", 501: "the output is not active",
    502: "the output is already paused", 503: "the output is not paused",
    504: "the output is disabled in OBS", 505: "studio mode is active",
    506: "studio mode is not active", 600: "OBS has no such resource",
    601: "the resource already exists", 602: "the resource is the wrong type",
    603: "OBS has not enough of that resource", 604: "the resource is not in a state that allows this",
    605: "the input kind is invalid", 606: "the resource cannot be configured",
    607: "the filter kind is invalid", 700: "OBS could not create the resource",
    701: "OBS could not perform the action", 702: "OBS could not process the request",
    703: "OBS cannot act right now",
}

REQUEST_TIMEOUT_S = 10.0
# SetCurrentSceneCollection blocks until the collection has loaded.
LONG_REQUEST_TIMEOUT_S = 40.0
HELLO_TIMEOUT_S = 8.0
# OBS acknowledges PauseRecord at once and pauses at the next keyframe (the
# keyframe interval, 2 s by default). When the recording cannot be paused at
# all it still acknowledges and nothing happens, so the driver waits for the
# paused event and reports a pause that never lands.
PAUSE_CONFIRM_S = 4.0

# obs-websocket's dB floor: SetInputVolume accepts -100 to 26 dB, and a
# fader at zero (multiplier 0) is reported as null (negative infinity).
VOLUME_DB_MIN = -100.0
VOLUME_DB_MAX = 26.0
METER_FLOOR_DB = -100.0
METER_MIN_INTERVAL_S = 0.5

OUTPUT_STATES = ("unknown", "starting", "started", "stopping", "stopped",
                 "reconnecting", "reconnected", "paused", "resumed")
TRANSIENT_OUTPUT_STATES = ("starting", "stopping", "reconnecting")
# OBS still refuses a Start while an output is stopping (and reports the
# STOPPING event with outputActive false), so "active" follows the state the
# event names, not that flag: anything short of stopped is still running.
INACTIVE_OUTPUT_STATES = ("stopped", "unknown")


def _output_active(data: dict[str, Any], state: str) -> bool:
    if state == "unknown":
        return _bool(data.get("outputActive"))
    return state not in INACTIVE_OUTPUT_STATES
MEDIA_STATES = ("none", "playing", "opening", "buffering", "paused", "stopped",
                "ended", "error")
MONITOR_TYPES = {
    "OBS_MONITORING_TYPE_NONE": "none",
    "OBS_MONITORING_TYPE_MONITOR_ONLY": "monitor_only",
    "OBS_MONITORING_TYPE_MONITOR_AND_OUTPUT": "monitor_and_output",
}
MONITOR_TYPES_WIRE = {v: k for k, v in MONITOR_TYPES.items()}
MEDIA_ACTIONS = ("play", "pause", "stop", "restart", "next", "previous")
VIDEO_MIX_TYPES = {
    "program": "OBS_WEBSOCKET_VIDEO_MIX_TYPE_PROGRAM",
    "preview": "OBS_WEBSOCKET_VIDEO_MIX_TYPE_PREVIEW",
    "multiview": "OBS_WEBSOCKET_VIDEO_MIX_TYPE_MULTIVIEW",
}
SOURCE_TYPES = {
    "OBS_SOURCE_TYPE_INPUT": "input",
    "OBS_SOURCE_TYPE_SCENE": "scene",
}

# Input kinds (unversioned) whose settings carry a ``text`` field.
TEXT_INPUT_KINDS = ("text_gdiplus", "text_ft2_source")
# Input kinds that answer the media requests (play, pause, cursor).
MEDIA_INPUT_KINDS = ("ffmpeg_source", "vlc_source", "slideshow")
BROWSER_INPUT_KINDS = ("browser_source",)

_CHILD_ID_RE = re.compile(r"[^A-Za-z0-9_-]")
_CHILD_ID_MAX = 96


def _sanitize_id(raw: str) -> str:
    """An OBS name as a child local id: ``[A-Za-z0-9_-]`` only."""
    text = _CHILD_ID_RE.sub("_", str(raw)).strip("_") or "_"
    return text[:_CHILD_ID_MAX]


def _output_state(wire: Any) -> str:
    text = str(wire or "")
    if text.startswith("OBS_WEBSOCKET_OUTPUT_"):
        text = text[len("OBS_WEBSOCKET_OUTPUT_"):]
    text = text.lower()
    return text if text in OUTPUT_STATES else "unknown"


def _media_state(wire: Any) -> str:
    text = str(wire or "")
    if text.startswith("OBS_MEDIA_STATE_"):
        text = text[len("OBS_MEDIA_STATE_"):]
    text = text.lower()
    return text if text in MEDIA_STATES else "none"


def _db(value: Any) -> float:
    """A volume in dB as OBS reports it; null (silence) and anything below
    the floor read as the floor."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return VOLUME_DB_MIN
    if math.isnan(number) or number < VOLUME_DB_MIN:
        return VOLUME_DB_MIN
    return round(number, 1)


def _mul_to_db(mul: Any) -> float:
    try:
        number = float(mul)
    except (TypeError, ValueError):
        return METER_FLOOR_DB
    if number <= 0:
        return METER_FLOOR_DB
    return max(METER_FLOOR_DB, round(20.0 * math.log10(number), 1))


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def auth_string(password: str, salt: str, challenge: str) -> str:
    """The Identify ``authentication`` string for a Hello challenge."""
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()).decode("ascii")
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode("utf-8")).digest()).decode("ascii")


class ObsRequestError(Exception):
    """obs-websocket answered a request with ``result: false``."""

    def __init__(self, request_type: str, code: int, comment: str = ""):
        super().__init__(comment or STATUS_TEXT.get(code, f"{request_type} failed with code {code}"))
        self.request_type = request_type
        self.code = code
        self.comment = comment

    @property
    def text(self) -> str:
        """The comment OBS sent, else the meaning of the code."""
        return self.comment or STATUS_TEXT.get(self.code, f"code {self.code}")


class _Roster:
    """Name <-> child id bookkeeping for one child type. OBS names are the
    protocol's keys; the ids are readable forms of them that never collide."""

    def __init__(self) -> None:
        self.id_by_name: dict[str, str] = {}
        self.name_by_id: dict[str, str] = {}

    def assign(self, name: str) -> str:
        if name in self.id_by_name:
            return self.id_by_name[name]
        base = _sanitize_id(name)
        candidate = base
        n = 2
        while candidate in self.name_by_id:
            suffix = f"_{n}"
            candidate = base[:_CHILD_ID_MAX - len(suffix)] + suffix
            n += 1
        self.id_by_name[name] = candidate
        self.name_by_id[candidate] = name
        return candidate

    def drop(self, name: str) -> str | None:
        local = self.id_by_name.pop(name, None)
        if local is not None:
            self.name_by_id.pop(local, None)
        return local

    def clear(self) -> None:
        self.id_by_name.clear()
        self.name_by_id.clear()


class ObsStudioDriver(BaseDriver):
    """OBS Studio over obs-websocket 5."""

    DRIVER_INFO = {
        "id": "obs_studio",
        "name": "OBS Studio",
        "manufacturer": "OBS Project",
        "category": "video",
        "version": "1.0.0",
        # The connection lifecycle hooks this driver overrides landed in
        # 0.24.0; the sibling drivers that own their session declare 0.25.0.
        "min_platform_version": "0.25.0",
        "author": "OpenAVC",
        "description": (
            "Controls OBS Studio through its built-in WebSocket server: switch "
            "program and preview scenes, run the studio-mode transition, start "
            "and stop streaming, recording, the virtual camera and the replay "
            "buffer, mute and set the level of any audio input, play media "
            "sources, change text sources, show and hide scene items, switch "
            "filters, scene collections and profiles, and watch OBS's CPU, "
            "frame rate and dropped frames."
        ),
        "source_url": "https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md",
        "tags": ["video-production", "streaming", "recording", "switcher", "software",
                 "lecture-capture", "obs-websocket"],
        "verified": False,
        "simulated": True,
        "transport": "tcp",
        "ports": [4455],
        "protocols": ["obs-websocket"],
        "compatible_models": [
            {
                "manufacturer": "OBS Project",
                "models": ["OBS Studio"],
                "confidence": "full",
                "notes": (
                    "OBS Studio 28 or later on Windows, macOS or Linux (obs-websocket 5 "
                    "is built in). OBS 27 and earlier need the obs-websocket 5 plugin "
                    "installed separately; the 4.x plugin speaks a different protocol "
                    "and is not supported."
                ),
            },
        ],
        "help": {
            "overview": (
                "OBS Studio is free video production software used for lecture "
                "capture, live streaming and recording. This driver talks to the "
                "WebSocket server built into OBS.\n\n"
                "OpenAVC switches the program and preview scene, runs the transition "
                "in studio mode, starts and stops the stream, the recording (with "
                "pause, file split and chapter markers), the virtual camera and the "
                "replay buffer, sets each audio input's mute, level, balance, sync "
                "offset and monitoring, plays and pauses media sources, rewrites text "
                "sources, refreshes browser sources, shows and hides scene items, "
                "turns filters on and off, starts any other output OBS has (NDI, "
                "Decklink), switches scene collections and profiles, triggers "
                "hotkeys, opens projectors and saves screenshots.\n\n"
                "Every scene, input, scene item, filter and output is a child entity "
                "with live state, and changes made in OBS itself show up at once."
            ),
            "setup": (
                "1. In OBS open Tools > WebSocket Server Settings, tick Enable "
                "WebSocket server and click Apply. The default port is 4455.\n"
                "2. If Enable Authentication is ticked (the OBS default), click Show "
                "Connect Info and copy the Server Password into the Password field "
                "below.\n"
                "3. Enter the address of the computer running OBS: 127.0.0.1 when OBS "
                "runs on the same computer as OpenAVC, otherwise its IP address on "
                "the network. For another computer, allow TCP port 4455 through its "
                "firewall.\n"
                "4. Scenes, inputs and outputs are read from OBS when the device "
                "connects and follow OBS as they change; use Refresh from Device "
                "after a large rebuild in OBS if anything is missing."
            ),
            "connection": (
                "Check that the WebSocket server is enabled in OBS (Tools > WebSocket "
                "Server Settings), that the port matches, and that the password is "
                "the one shown under Show Connect Info."
            ),
        },
        "default_config": {
            "host": "",
            "port": 4455,
            "password": "",
            "poll_interval": 5,
            "enable_meters": False,
        },
        "config_schema": {
            "host": {
                "type": "string", "required": True, "label": "IP Address",
                "help": "The computer running OBS. 127.0.0.1 when it is this computer.",
            },
            "port": {
                "type": "integer", "default": 4455, "min": 1, "max": 65535, "label": "Port",
                "help": "The WebSocket server port in OBS (Tools > WebSocket Server Settings). Default 4455.",
            },
            "password": {
                "type": "string", "default": "", "label": "Password", "secret": True,
                "help": "The Server Password from OBS's WebSocket Server Settings. Leave blank when Enable Authentication is off in OBS.",
            },
            "poll_interval": {
                "type": "integer", "default": 5, "min": 0, "label": "Poll Interval (sec)",
                "help": "How often statistics, stream and record timecodes and media positions are read. Everything else arrives the moment it changes. 0 disables polling.",
            },
            "enable_meters": {
                "type": "boolean", "default": False, "label": "Audio Level Meters", "advanced": True,
                "help": "Subscribe to OBS's audio level meters and publish each input's peak level (updated up to twice a second). Off by default: it is a high-volume event stream.",
            },
        },
        "state_variables": {
            "obs_version": {"type": "string", "label": "OBS Version"},
            "websocket_version": {"type": "string", "label": "obs-websocket Version"},
            "rpc_version": {"type": "integer", "label": "RPC Version"},
            "platform": {"type": "string", "label": "Platform"},
            "platform_description": {"type": "string", "label": "Platform Description"},
            "studio_mode": {"type": "boolean", "label": "Studio Mode", "control": True, "cloud_priority": "high"},
            "program_scene": {"type": "string", "label": "Program Scene", "control": True, "cloud_priority": "high"},
            "preview_scene": {"type": "string", "label": "Preview Scene", "control": True,
                              "help": "Blank unless studio mode is on."},
            "scene_options": {"type": "string", "label": "Scenes",
                              "help": "JSON list of the scene names, top to bottom as in OBS; feeds the scene pickers."},
            "scene_count": {"type": "integer", "label": "Scene Count"},
            "input_count": {"type": "integer", "label": "Input Count"},
            "transition": {"type": "string", "label": "Transition", "control": True},
            "transition_kind": {"type": "string", "label": "Transition Kind"},
            "transition_duration_ms": {"type": "integer", "label": "Transition Duration (ms)", "min": 0, "max": 20000, "unit": "ms", "control": True},
            "transition_fixed": {"type": "boolean", "label": "Transition Has Fixed Duration"},
            "transition_in_progress": {"type": "boolean", "label": "Transition In Progress", "cloud_priority": "high"},
            "transition_options": {"type": "string", "label": "Transitions",
                                   "help": "JSON list of the transition names; feeds the Set Transition picker."},
            "streaming": {"type": "boolean", "label": "Streaming", "control": True, "cloud_priority": "high"},
            "stream_state": {"type": "enum", "values": ["unknown", "starting", "started", "stopping", "stopped", "reconnecting", "reconnected", "paused", "resumed"], "label": "Stream State", "cloud_priority": "high"},
            "stream_reconnecting": {"type": "boolean", "label": "Stream Reconnecting", "cloud_priority": "high"},
            "stream_timecode": {"type": "string", "label": "Stream Timecode", "cloud_priority": "low"},
            "stream_duration_ms": {"type": "integer", "label": "Stream Duration (ms)", "unit": "ms", "cloud_priority": "low"},
            "stream_congestion": {"type": "number", "label": "Stream Congestion", "min": 0, "max": 1, "cloud_priority": "low"},
            "stream_bytes": {"type": "integer", "label": "Stream Bytes Sent", "cloud_priority": "low"},
            "stream_skipped_frames": {"type": "integer", "label": "Stream Skipped Frames", "cloud_priority": "low"},
            "stream_total_frames": {"type": "integer", "label": "Stream Total Frames", "cloud_priority": "low"},
            "recording": {"type": "boolean", "label": "Recording", "control": True, "cloud_priority": "high"},
            "record_state": {"type": "enum", "values": ["unknown", "starting", "started", "stopping", "stopped", "reconnecting", "reconnected", "paused", "resumed"], "label": "Record State", "cloud_priority": "high"},
            "record_paused": {"type": "boolean", "label": "Recording Paused", "control": True, "cloud_priority": "high"},
            "record_timecode": {"type": "string", "label": "Record Timecode", "cloud_priority": "low"},
            "record_duration_ms": {"type": "integer", "label": "Record Duration (ms)", "unit": "ms", "cloud_priority": "low"},
            "record_bytes": {"type": "integer", "label": "Record Bytes Written", "cloud_priority": "low"},
            "record_file": {"type": "string", "label": "Recording File",
                            "help": "The file being written, or the last one finished."},
            "record_directory": {"type": "string", "label": "Recording Folder"},
            "virtual_camera": {"type": "boolean", "label": "Virtual Camera", "control": True, "cloud_priority": "high"},
            "replay_buffer": {"type": "boolean", "label": "Replay Buffer", "control": True, "cloud_priority": "high"},
            "last_replay_file": {"type": "string", "label": "Last Saved Replay"},
            "scene_collection": {"type": "string", "label": "Scene Collection", "control": True},
            "scene_collection_options": {"type": "string", "label": "Scene Collections",
                                         "help": "JSON list; feeds the Switch Scene Collection picker."},
            "profile": {"type": "string", "label": "Profile", "control": True},
            "profile_options": {"type": "string", "label": "Profiles",
                                "help": "JSON list; feeds the Switch Profile picker."},
            "source_options": {"type": "string", "label": "Sources",
                               "help": "JSON list of every input and scene name; feeds the source pickers."},
            "hotkey_options": {"type": "string", "label": "Hotkeys", "cloud_priority": "low",
                               "help": "JSON list of OBS's hotkey names; feeds the Trigger Hotkey picker."},
            "monitor_options": {"type": "string", "label": "Monitors",
                                "help": "JSON list of the displays attached to the OBS computer; feeds the projector pickers."},
            "cpu_percent": {"type": "number", "label": "CPU Usage (%)", "min": 0, "max": 100, "unit": "%", "cloud_priority": "low"},
            "memory_mb": {"type": "number", "label": "Memory Used (MB)", "unit": "MB", "cloud_priority": "low"},
            "disk_free_mb": {"type": "number", "label": "Recording Disk Free (MB)", "unit": "MB", "cloud_priority": "low"},
            "active_fps": {"type": "number", "label": "Rendered Frame Rate", "unit": "fps", "cloud_priority": "low"},
            "frame_render_ms": {"type": "number", "label": "Frame Render Time (ms)", "unit": "ms", "cloud_priority": "low"},
            "render_skipped_frames": {"type": "integer", "label": "Render Skipped Frames", "cloud_priority": "low"},
            "render_total_frames": {"type": "integer", "label": "Render Total Frames", "cloud_priority": "low"},
            "output_skipped_frames": {"type": "integer", "label": "Encoder Skipped Frames", "cloud_priority": "low"},
            "output_total_frames": {"type": "integer", "label": "Encoder Total Frames", "cloud_priority": "low"},
            "base_resolution": {"type": "string", "label": "Canvas Resolution"},
            "output_resolution": {"type": "string", "label": "Output Resolution"},
            "fps": {"type": "number", "label": "Configured Frame Rate", "unit": "fps"},
            "last_error": {"type": "string", "label": "Last Error"},
        },
        "child_entity_types": {
            "scene": {
                "label": "Scene", "label_plural": "Scenes",
                "id_format": {"type": "string"},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "program": {"type": "boolean", "label": "On Program", "cloud_priority": "high"},
                    "preview": {"type": "boolean", "label": "On Preview"},
                    "index": {"type": "integer", "label": "Position",
                              "help": "0 is the bottom of the scene list in OBS."},
                    "item_count": {"type": "integer", "label": "Scene Items"},
                },
                "summary_fields": ["name", "program", "preview"],
                "label_field": "name",
            },
            "input": {
                "label": "Input", "label_plural": "Inputs",
                "id_format": {"type": "string"},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "kind": {"type": "string", "label": "Kind"},
                    "has_audio": {"type": "boolean", "label": "Has Audio"},
                    "muted": {"type": "boolean", "label": "Muted", "control": True, "cloud_priority": "high"},
                    "volume_db": {"type": "number", "label": "Volume (dB)", "min": -100, "max": 26,
                                  "step": 0.5, "unit": "dB", "control": True},
                    "volume_mul": {"type": "number", "label": "Volume (multiplier)", "min": 0, "max": 20},
                    "balance": {"type": "number", "label": "Balance", "min": 0, "max": 1, "step": 0.01, "control": True,
                                "help": "0 is fully left, 0.5 centre, 1 fully right."},
                    "sync_offset_ms": {"type": "integer", "label": "Sync Offset (ms)", "min": -950, "max": 20000, "unit": "ms", "control": True},
                    "monitor_type": {"type": "enum", "values": ["none", "monitor_only", "monitor_and_output"],
                                     "label": "Audio Monitoring", "control": True},
                    "active": {"type": "boolean", "label": "On Program", "cloud_priority": "high",
                               "help": "True while the input is part of what the program output shows."},
                    "showing": {"type": "boolean", "label": "Showing",
                                "help": "True while the input is shown in the preview, a projector or a dialog."},
                    "is_media": {"type": "boolean", "label": "Media Source"},
                    "media_state": {"type": "enum", "values": ["none", "playing", "opening", "buffering", "paused", "stopped", "ended", "error"], "label": "Media State", "cloud_priority": "high"},
                    "media_duration_ms": {"type": "integer", "label": "Media Duration (ms)", "unit": "ms"},
                    "media_cursor_ms": {"type": "integer", "label": "Media Position (ms)", "unit": "ms", "cloud_priority": "low"},
                    "is_text": {"type": "boolean", "label": "Text Source"},
                    "text": {"type": "string", "label": "Text", "control": True},
                    "level_db": {"type": "number", "label": "Peak Level (dB)", "min": -100, "max": 0, "unit": "dB", "cloud_priority": "low",
                                 "help": "Only updates while Audio Level Meters is on in the device settings."},
                },
                "summary_fields": ["name", "kind", "muted", "volume_db"],
                "label_field": "name",
            },
            "scene_item": {
                "label": "Scene Item", "label_plural": "Scene Items",
                "id_format": {"type": "string"},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "scene": {"type": "string", "label": "Scene"},
                    "source": {"type": "string", "label": "Source"},
                    "source_type": {"type": "enum", "values": ["input", "scene", "group"], "label": "Source Type"},
                    "item_id": {"type": "integer", "label": "Scene Item ID"},
                    "enabled": {"type": "boolean", "label": "Visible", "control": True, "cloud_priority": "high"},
                    "locked": {"type": "boolean", "label": "Locked"},
                    "index": {"type": "integer", "label": "Position"},
                },
                "summary_fields": ["scene", "source", "enabled"],
                "label_field": "name",
            },
            "filter": {
                "label": "Filter", "label_plural": "Filters",
                "id_format": {"type": "string"},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "source": {"type": "string", "label": "Source"},
                    "filter_name": {"type": "string", "label": "Filter"},
                    "kind": {"type": "string", "label": "Kind"},
                    "enabled": {"type": "boolean", "label": "Enabled", "control": True, "cloud_priority": "high"},
                    "index": {"type": "integer", "label": "Position"},
                },
                "summary_fields": ["source", "filter_name", "enabled"],
                "label_field": "name",
            },
            "output": {
                "label": "Output", "label_plural": "Outputs",
                "id_format": {"type": "string"},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "kind": {"type": "string", "label": "Kind"},
                    "active": {"type": "boolean", "label": "Active", "control": True, "cloud_priority": "high"},
                    "reconnecting": {"type": "boolean", "label": "Reconnecting"},
                    "width": {"type": "integer", "label": "Width"},
                    "height": {"type": "integer", "label": "Height"},
                },
                "summary_fields": ["name", "kind", "active"],
                "label_field": "name",
            },
        },
        "device_settings": {
            "studio_mode": {
                "type": "boolean", "label": "Studio Mode",
                "state_key": "studio_mode", "default": False, "setup": False,
                "help": "With studio mode on, scene changes go to the preview and the Transition command takes them to program.",
            },
            "transition_duration_ms": {
                "type": "integer", "label": "Transition Duration (ms)", "min": 0, "max": 20000,
                "state_key": "transition_duration_ms", "default": 300, "setup": False,
                "help": "The current transition's duration. Ignored by transitions with a fixed duration (Cut).",
            },
        },
        "commands": {
            # ── Scenes and transitions ──
            "set_program_scene": {
                "label": "Switch Program Scene",
                "params": {"scene": {"type": "child_id", "child_type": "scene", "required": True, "label": "Scene"}},
                "help": "Put a scene on program. In studio mode this cuts straight to program without a transition; use Switch Preview Scene and Transition instead.",
            },
            "set_preview_scene": {
                "label": "Switch Preview Scene",
                "params": {"scene": {"type": "child_id", "child_type": "scene", "required": True, "label": "Scene"}},
                "help": "Studio mode only.",
            },
            "transition": {"label": "Transition", "params": {},
                           "help": "Take the preview scene to program with the current transition. Studio mode only."},
            "set_studio_mode": {
                "label": "Set Studio Mode",
                "params": {"enabled": {"type": "boolean", "required": True, "label": "Enabled"}},
            },
            "studio_mode_on": {"label": "Studio Mode On", "params": {}},
            "studio_mode_off": {"label": "Studio Mode Off", "params": {}},
            "set_transition": {
                "label": "Set Transition",
                "params": {"name": {"type": "string", "required": True, "label": "Transition", "options_state": "transition_options"}},
            },
            "set_transition_duration": {
                "label": "Set Transition Duration",
                "params": {"duration_ms": {"type": "integer", "required": True, "label": "Duration (ms)", "min": 0, "max": 20000, "unit": "ms"}},
            },
            "set_tbar_position": {
                "label": "Set T-Bar Position",
                "params": {
                    "position": {"type": "number", "required": True, "label": "Position", "min": 0, "max": 1, "decimals": 3,
                                 "help": "0 is the preview side, 1 completes the transition."},
                    "release": {"type": "boolean", "label": "Release",
                                "help": "Yes (the default) lets go of the T-bar after the move; No holds it for a following move."},
                },
                "help": "Studio mode only.",
            },
            # ── Stream ──
            "start_stream": {"label": "Start Streaming", "params": {}},
            "stop_stream": {"label": "Stop Streaming", "params": {}},
            "toggle_stream": {"label": "Toggle Streaming", "params": {}},
            "send_caption": {
                "label": "Send Stream Caption",
                "params": {"text": {"type": "string", "required": True, "label": "Caption Text", "trim": False}},
                "help": "Send CEA-608 caption text on the stream.",
            },
            # ── Record ──
            "start_record": {"label": "Start Recording", "params": {}},
            "stop_record": {"label": "Stop Recording", "params": {}},
            "toggle_record": {"label": "Toggle Recording", "params": {}},
            "pause_record": {"label": "Pause Recording", "params": {}},
            "resume_record": {"label": "Resume Recording", "params": {}},
            "toggle_record_pause": {"label": "Toggle Recording Pause", "params": {}},
            "split_record_file": {"label": "Split Recording File", "params": {},
                                  "help": "Close the current recording file and continue in a new one. OBS 30.2 or later, with Automatic File Splitting enabled under Settings > Output > Recording."},
            "create_record_chapter": {
                "label": "Add Chapter Marker",
                "params": {"name": {"type": "string", "label": "Chapter Name"}},
                "help": "Add a chapter marker to the recording. OBS 30.2 or later, Hybrid MP4 recordings only.",
            },
            # ── Virtual camera, replay buffer, other outputs ──
            "start_virtual_camera": {"label": "Start Virtual Camera", "params": {}},
            "stop_virtual_camera": {"label": "Stop Virtual Camera", "params": {}},
            "toggle_virtual_camera": {"label": "Toggle Virtual Camera", "params": {}},
            "start_replay_buffer": {"label": "Start Replay Buffer", "params": {}},
            "stop_replay_buffer": {"label": "Stop Replay Buffer", "params": {}},
            "toggle_replay_buffer": {"label": "Toggle Replay Buffer", "params": {}},
            "save_replay_buffer": {"label": "Save Replay", "params": {},
                                   "help": "Write the replay buffer's contents to a file. The replay buffer must be running."},
            "start_output": {"label": "Start Output", "params": {"output": {"type": "child_id", "child_type": "output", "required": True, "label": "Output"}}},
            "stop_output": {"label": "Stop Output", "params": {"output": {"type": "child_id", "child_type": "output", "required": True, "label": "Output"}}},
            "toggle_output": {"label": "Toggle Output", "params": {"output": {"type": "child_id", "child_type": "output", "required": True, "label": "Output"}}},
            # ── Inputs: audio ──
            "mute_input": {"label": "Mute Input", "params": {"input": {"type": "child_id", "child_type": "input", "required": True, "label": "Input"}}},
            "unmute_input": {"label": "Unmute Input", "params": {"input": {"type": "child_id", "child_type": "input", "required": True, "label": "Input"}}},
            "toggle_input_mute": {"label": "Toggle Input Mute", "params": {"input": {"type": "child_id", "child_type": "input", "required": True, "label": "Input"}}},
            "set_input_volume": {
                "label": "Set Input Volume",
                "params": {
                    "input": {"type": "child_id", "child_type": "input", "required": True, "label": "Input"},
                    "level_db": {"type": "number", "required": True, "label": "Level (dB)", "min": -100, "max": 26, "decimals": 1, "unit": "dB"},
                },
            },
            "adjust_input_volume": {
                "label": "Adjust Input Volume",
                "params": {
                    "input": {"type": "child_id", "child_type": "input", "required": True, "label": "Input"},
                    "delta_db": {"type": "number", "required": True, "label": "Change (dB)", "min": -60, "max": 60, "decimals": 1, "unit": "dB",
                                 "help": "Added to the current level; the result stays within -100 and +26 dB."},
                },
            },
            "set_input_balance": {
                "label": "Set Input Balance",
                "params": {
                    "input": {"type": "child_id", "child_type": "input", "required": True, "label": "Input"},
                    "balance": {"type": "number", "required": True, "label": "Balance", "min": 0, "max": 1, "decimals": 2,
                                "help": "0 is fully left, 0.5 centre, 1 fully right."},
                },
            },
            "set_input_sync_offset": {
                "label": "Set Input Sync Offset",
                "params": {
                    "input": {"type": "child_id", "child_type": "input", "required": True, "label": "Input"},
                    "offset_ms": {"type": "integer", "required": True, "label": "Offset (ms)", "min": -950, "max": 20000, "unit": "ms"},
                },
            },
            "set_input_monitoring": {
                "label": "Set Input Audio Monitoring",
                "params": {
                    "input": {"type": "child_id", "child_type": "input", "required": True, "label": "Input"},
                    "monitor_type": {"type": "enum", "required": True, "label": "Monitoring",
                                     "values": [{"value": "none", "label": "Off"},
                                                {"value": "monitor_only", "label": "Monitor Only"},
                                                {"value": "monitor_and_output", "label": "Monitor and Output"}]},
                },
            },
            # ── Inputs: media, text, browser ──
            "media_play": {"label": "Play Media", "params": {"input": {"type": "child_id", "child_type": "input", "required": True, "label": "Media Input"}}},
            "media_pause": {"label": "Pause Media", "params": {"input": {"type": "child_id", "child_type": "input", "required": True, "label": "Media Input"}}},
            "media_stop": {"label": "Stop Media", "params": {"input": {"type": "child_id", "child_type": "input", "required": True, "label": "Media Input"}}},
            "media_restart": {"label": "Restart Media", "params": {"input": {"type": "child_id", "child_type": "input", "required": True, "label": "Media Input"}}},
            "media_next": {"label": "Next Media Item", "params": {"input": {"type": "child_id", "child_type": "input", "required": True, "label": "Media Input"}},
                           "help": "Playlist sources (VLC, image slide show)."},
            "media_previous": {"label": "Previous Media Item", "params": {"input": {"type": "child_id", "child_type": "input", "required": True, "label": "Media Input"}},
                               "help": "Playlist sources (VLC, image slide show)."},
            "set_media_position": {
                "label": "Set Media Position",
                "params": {
                    "input": {"type": "child_id", "child_type": "input", "required": True, "label": "Media Input"},
                    "position_ms": {"type": "integer", "required": True, "label": "Position (ms)", "min": 0, "unit": "ms"},
                },
            },
            "offset_media_position": {
                "label": "Skip Media",
                "params": {
                    "input": {"type": "child_id", "child_type": "input", "required": True, "label": "Media Input"},
                    "offset_ms": {"type": "integer", "required": True, "label": "Skip (ms)", "min": -3600000, "max": 3600000, "unit": "ms",
                                  "help": "Negative skips back."},
                },
            },
            "set_text": {
                "label": "Set Text",
                "params": {
                    "input": {"type": "child_id", "child_type": "input", "required": True, "label": "Text Input"},
                    "text": {"type": "string", "required": True, "label": "Text", "trim": False},
                },
                "help": "Replace the contents of a text source (lower thirds, titles, timers).",
            },
            "refresh_browser_source": {
                "label": "Refresh Browser Source",
                "params": {"input": {"type": "child_id", "child_type": "input", "required": True, "label": "Browser Input"}},
                "help": "Reload the page without cache, as the Refresh button in the source's properties does.",
            },
            # ── Scene items ──
            "show_scene_item": {"label": "Show Scene Item", "params": {"item": {"type": "child_id", "child_type": "scene_item", "required": True, "label": "Scene Item"}}},
            "hide_scene_item": {"label": "Hide Scene Item", "params": {"item": {"type": "child_id", "child_type": "scene_item", "required": True, "label": "Scene Item"}}},
            "toggle_scene_item": {"label": "Toggle Scene Item", "params": {"item": {"type": "child_id", "child_type": "scene_item", "required": True, "label": "Scene Item"}}},
            "set_source_visible": {
                "label": "Show / Hide Source in Scene",
                "params": {
                    "scene": {"type": "child_id", "child_type": "scene", "required": True, "label": "Scene"},
                    "source": {"type": "string", "required": True, "label": "Source", "options_state": "source_options"},
                    "visible": {"type": "boolean", "required": True, "label": "Visible"},
                },
                "help": "Find the source in the scene by name and set its visibility. Use the Scene Item commands when the item is already known.",
            },
            # ── Filters ──
            "enable_filter": {"label": "Enable Filter", "params": {"filter": {"type": "child_id", "child_type": "filter", "required": True, "label": "Filter"}}},
            "disable_filter": {"label": "Disable Filter", "params": {"filter": {"type": "child_id", "child_type": "filter", "required": True, "label": "Filter"}}},
            "toggle_filter": {"label": "Toggle Filter", "params": {"filter": {"type": "child_id", "child_type": "filter", "required": True, "label": "Filter"}}},
            # ── Collections, profiles, hotkeys, UI ──
            "set_scene_collection": {
                "label": "Switch Scene Collection",
                "params": {"name": {"type": "string", "required": True, "label": "Scene Collection", "options_state": "scene_collection_options"}},
                "help": "Loads a different set of scenes. Every scene, input and scene item is re-read once OBS has switched.",
            },
            "set_profile": {
                "label": "Switch Profile",
                "params": {"name": {"type": "string", "required": True, "label": "Profile", "options_state": "profile_options"}},
                "help": "Cannot be changed while streaming or recording.",
            },
            "trigger_hotkey": {
                "label": "Trigger Hotkey",
                "params": {"name": {"type": "string", "required": True, "label": "Hotkey", "options_state": "hotkey_options"}},
                "help": "Run an OBS hotkey by its internal name (for functions no other command covers).",
            },
            "trigger_key_sequence": {
                "label": "Press Key Combination",
                "params": {
                    "key": {"type": "string", "required": True, "label": "Key", "pattern": r"OBS_KEY_[A-Z0-9_]+",
                            "help": "An OBS key id such as OBS_KEY_F5 or OBS_KEY_A."},
                    "shift": {"type": "boolean", "label": "Shift"},
                    "control": {"type": "boolean", "label": "Control"},
                    "alt": {"type": "boolean", "label": "Alt"},
                    "command": {"type": "boolean", "label": "Command (Mac)"},
                },
                "help": "Trigger whatever OBS has bound to this key combination.",
            },
            "open_projector": {
                "label": "Open Projector",
                "params": {
                    "mix": {"type": "enum", "required": True, "label": "Show",
                            "values": [{"value": "program", "label": "Program"}, {"value": "preview", "label": "Preview"},
                                       {"value": "multiview", "label": "Multiview"}]},
                    "monitor": {"type": "string", "label": "Monitor", "options_state": "monitor_options",
                                "help": "A display attached to the OBS computer, or Windowed."},
                },
                "help": "Open a full-screen projector of the program, preview or multiview on one of the OBS computer's displays.",
            },
            "open_source_projector": {
                "label": "Open Source Projector",
                "params": {
                    "source": {"type": "string", "required": True, "label": "Source", "options_state": "source_options"},
                    "monitor": {"type": "string", "label": "Monitor", "options_state": "monitor_options"},
                },
            },
            "save_screenshot": {
                "label": "Save Screenshot",
                "params": {
                    "source": {"type": "string", "required": True, "label": "Source", "options_state": "source_options"},
                    "file_path": {"type": "string", "required": True, "label": "File Path",
                                  "help": "A path on the OBS computer, ending in the image format, e.g. C:\\captures\\program.png."},
                    "width": {"type": "integer", "label": "Width", "min": 8, "max": 4096},
                    "height": {"type": "integer", "label": "Height", "min": 8, "max": 4096},
                },
            },
            "broadcast_custom_event": {
                "label": "Broadcast Custom Event",
                "params": {"data": {"type": "string", "required": True, "label": "Event Data (JSON)", "trim": False,
                                    "help": "A JSON object delivered to every other obs-websocket client (scripts, stream decks)."}},
            },
        },
        "quick_actions": ["start_stream", "stop_stream", "start_record", "stop_record", "transition"],
        "actions": [
            {"id": "start_stream", "kind": "command", "icon": "radio"},
            {"id": "stop_stream", "kind": "command", "icon": "square",
             "confirm": "Stop streaming?"},
            {"id": "start_record", "kind": "command", "icon": "circle"},
            {"id": "stop_record", "kind": "command", "icon": "square"},
            {"id": "transition", "kind": "command", "icon": "arrow-right-left",
             "visible_when": {"key": "device.$id.studio_mode", "operator": "truthy"}},
            {"id": "start_virtual_camera", "kind": "command", "icon": "video"},
            {"id": "stop_virtual_camera", "kind": "command", "icon": "video-off"},
            {"id": "save_replay_buffer", "kind": "command", "icon": "save",
             "visible_when": {"key": "device.$id.replay_buffer", "operator": "truthy"}},
        ],
        "discovery": {
            # obs-websocket answers a WebSocket upgrade on 4455 with the 101
            # and then, before the client says anything, its Hello frame,
            # which names obs-websocket and carries both version numbers. The
            # probe sends a minimal upgrade request and reads until the Hello
            # arrives; a fixed key is a valid key.
            "tcp_probe": {
                "port": 4455,
                "send_ascii": (
                    "GET / HTTP/1.1\r\nHost: openavc\r\nUpgrade: websocket\r\n"
                    "Connection: Upgrade\r\nSec-WebSocket-Key: b3BlbmF2Yy1kaXNjb3Zlcg==\r\n"
                    "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: obswebsocket.json\r\n\r\n"
                ),
                "expect_regex": r'"obsWebSocketVersion"\s*:\s*"',
                "timeout_ms": 3000,
                "extract": {
                    "firmware": {"regex": r'"obsStudioVersion"\s*:\s*"([^"]+)"', "group": 1},
                },
                "extract_manufacturer": "OBS Project",
            },
            "port_open": [4455],
            "manufacturer_alias": ["OBS", "OBS Studio", "OBS Project", "obs-websocket"],
        },
    }

    # Liveness watchdog: a WebSocket that stays open while OBS hangs answers
    # nothing; GetVersion proves the request path still works.
    HEALTH_INTERVAL_S = 30.0
    HEALTH_TIMEOUT_S = 8.0
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = "Connected, but OBS stopped answering requests."

    def __init__(self, device_id: str, config: dict[str, Any], state, events):
        super().__init__(device_id, config, state, events)
        self._ws: Any = None
        self._reader_task: asyncio.Task | None = None
        # Events are handled by a worker of their own, in arrival order: a
        # handler that asks OBS a follow-up question needs the reader free to
        # deliver the answer.
        self._event_task: asyncio.Task | None = None
        self._event_queue: asyncio.Queue | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self._sync_lock = asyncio.Lock()
        self._req_id = 0
        self._closing = False
        self._collection_changing = False
        self._identified = False
        # Rosters: OBS names behind each child local id.
        self._scenes = _Roster()
        self._inputs = _Roster()
        self._outputs = _Roster()
        # scene_item local id -> (scene name, scene item id); filter local id
        # -> (source name, filter name).
        self._items: dict[str, tuple[str, int]] = {}
        self._item_ids: dict[tuple[str, int], str] = {}
        self._filters: dict[str, tuple[str, str]] = {}
        self._filter_ids: dict[tuple[str, str], str] = {}
        # Per input: unversioned kind, and the last meter write time.
        self._input_kinds: dict[str, str] = {}
        self._meter_last: dict[str, float] = {}
        self._studio_mode = False
        self._paused_event = asyncio.Event()

    # ── Connection lifecycle ──────────────────────────────────────────────

    def _host(self) -> str:
        return str(self.config.get("host", "")).strip()

    def _port(self) -> int:
        return _int(self.config.get("port", 4455), 4455)

    def _password(self) -> str:
        return str(self.config.get("password", "") or "")

    def _subscriptions(self) -> int:
        mask = BASE_SUBSCRIPTIONS
        if _bool(self.config.get("enable_meters", False)):
            mask |= SUB_INPUT_VOLUME_METERS
        return mask

    async def _pre_connect(self) -> None:
        self._closing = False
        self._identified = False
        self._collection_changing = False
        if not self._host():
            raise ConnectionFaultError(
                "No IP address is set for the OBS computer.", code="invalid_config")

    async def _create_transport(self, transport_type: str) -> None:
        url = f"ws://{self._host()}:{self._port()}"
        self._ws = await websockets.connect(
            url,
            subprotocols=[SUBPROTOCOL],
            open_timeout=6.0,
            max_size=None,
            ping_interval=20,
            ping_timeout=10,
        )

    async def _post_connect(self) -> None:
        # The handshake reads the socket directly; the reader loop starts
        # only once Identified has arrived, so nothing races the id-correlated
        # dispatch. A raise here aborts the connect and the platform closes
        # the socket through _close_session.
        await self._identify()
        self._event_queue = asyncio.Queue()
        self._event_task = asyncio.create_task(self._event_worker())
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _initial_sync(self) -> None:
        try:
            await self._full_sync()
        except ObsRequestError as exc:
            if exc.code == STATUS_NOT_READY:
                # OBS answers 207 while it is starting up or shutting down;
                # a connection failure, so the platform tries again.
                raise ConnectionError("OBS is not ready yet (starting up or shutting down)") from exc
            raise

    def _link_alive(self) -> bool:
        return self._ws is not None and _ws_is_open(self._ws)

    async def disconnect(self) -> None:
        self._closing = True
        await super().disconnect()

    def _handle_transport_disconnect(self) -> None:
        self._fail_pending()
        self._ws = None
        self._identified = False
        super()._handle_transport_disconnect()

    async def _close_session(self) -> None:
        self._fail_pending()
        self._identified = False
        for attr in ("_reader_task", "_event_task"):
            task = getattr(self, attr)
            setattr(self, attr, None)
            if task and task is not asyncio.current_task():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._event_queue = None
        sock = self._ws
        self._ws = None
        if sock is not None:
            try:
                await sock.close()
            except Exception:
                pass

    def _fail_pending(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    async def _liveness_probe(self) -> None:
        await self._request("GetVersion", timeout=self.HEALTH_TIMEOUT_S)

    # ── Handshake ─────────────────────────────────────────────────────────

    async def _recv_json(self, timeout: float) -> dict[str, Any]:
        raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ConnectionError("OBS sent a frame that is not JSON") from exc
        if not isinstance(msg, dict):
            raise ConnectionError("OBS sent a frame that is not an object")
        return msg

    async def _identify(self) -> None:
        try:
            hello = await self._recv_json(HELLO_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise ConnectionError(
                "OBS did not send its Hello. The port answers but is not obs-websocket 5 "
                "(OBS 28 or later, or the obs-websocket 5 plugin)."
            ) from exc
        except websockets.exceptions.ConnectionClosed as exc:
            raise self._closed_fault(exc, "before its Hello") from exc
        if hello.get("op") != OP_HELLO:
            raise ConnectionError(f"OBS opened with op {hello.get('op')!r} instead of Hello")
        data = hello.get("d") or {}
        identify: dict[str, Any] = {
            "rpcVersion": RPC_VERSION,
            "eventSubscriptions": self._subscriptions(),
        }
        auth = data.get("authentication")
        if isinstance(auth, dict):
            password = self._password()
            if not password:
                raise ConnectionFaultError(
                    "OBS requires a password (Tools > WebSocket Server Settings > "
                    "Show Connect Info) and none is set.",
                    code="auth_failed",
                )
            identify["authentication"] = auth_string(
                password, str(auth.get("salt", "")), str(auth.get("challenge", "")))
        await self._ws.send(json.dumps({"op": OP_IDENTIFY, "d": identify}))
        try:
            identified = await self._recv_json(HELLO_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise ConnectionError("OBS did not answer the Identify message") from exc
        except websockets.exceptions.ConnectionClosed as exc:
            raise self._closed_fault(exc, "after Identify") from exc
        if identified.get("op") != OP_IDENTIFIED:
            raise ConnectionError(
                f"OBS answered Identify with op {identified.get('op')!r} instead of Identified")
        self._identified = True
        self.set_states({
            "obs_version": str(data.get("obsStudioVersion", "")),
            "websocket_version": str(data.get("obsWebSocketVersion", "")),
            "rpc_version": _int((identified.get("d") or {}).get("negotiatedRpcVersion"),
                                _int(data.get("rpcVersion"), RPC_VERSION)),
        })

    def _closed_fault(self, exc: Exception, when: str) -> Exception:
        """A close during the handshake, read by its close code."""
        code = _close_code(exc)
        if code == CLOSE_AUTHENTICATION_FAILED:
            return ConnectionFaultError(
                "OBS rejected the password. Copy the Server Password from Tools > "
                "WebSocket Server Settings > Show Connect Info.",
                code="auth_failed",
            )
        if code == CLOSE_UNSUPPORTED_RPC_VERSION:
            return ConnectionError(
                f"OBS does not support obs-websocket RPC version {RPC_VERSION}")
        return ConnectionError(f"OBS closed the connection {when} (code {code})")

    # ── Reader loop, requests ─────────────────────────────────────────────

    async def _read_loop(self) -> None:
        ws = self._ws
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(msg, dict):
                    continue
                op = msg.get("op")
                data = msg.get("d") or {}
                if op in (OP_REQUEST_RESPONSE, OP_REQUEST_BATCH_RESPONSE):
                    fut = self._pending.pop(str(data.get("requestId", "")), None)
                    if fut is not None and not fut.done():
                        fut.set_result(data)
                elif op == OP_EVENT and self._event_queue is not None:
                    self._event_queue.put_nowait(
                        (str(data.get("eventType", "")), data.get("eventData") or {}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = _close_code(exc)
            if code == CLOSE_SESSION_INVALIDATED:
                log.warning(f"[{self.device_id}] OBS ended the session (kicked)")
                self._stash_fault("transport_disconnected", "OBS ended the session.")
            else:
                log.debug(f"[{self.device_id}] Reader loop ended: {exc}")
                self._stash_fault("transport_disconnected", "The connection to OBS dropped.")
        finally:
            if not self._closing:
                self._handle_transport_disconnect()

    async def _event_worker(self) -> None:
        queue = self._event_queue
        if queue is None:
            return
        while True:
            event, data = await queue.get()
            try:
                await self._handle_event(event, data)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug(f"[{self.device_id}] Event handler for {event} failed", exc_info=True)

    def _next_id(self) -> str:
        self._req_id += 1
        return f"openavc-{self._req_id}"

    async def _send(self, payload: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None or not self._identified:
            raise ConnectionError(f"[{self.device_id}] Not connected to OBS")
        async with self._send_lock:
            await ws.send(json.dumps(payload))

    async def _request(
        self, request_type: str, data: dict[str, Any] | None = None, *,
        timeout: float = REQUEST_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Send one request and return its ``responseData``; a failed
        request raises ObsRequestError."""
        rid = self._next_id()
        body: dict[str, Any] = {"requestType": request_type, "requestId": rid}
        if data:
            body["requestData"] = data
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"op": OP_REQUEST, "d": body})
            reply = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"[{self.device_id}] OBS did not answer {request_type}")
        except asyncio.CancelledError:
            raise ConnectionError(f"[{self.device_id}] Connection to OBS dropped during {request_type}")
        finally:
            self._pending.pop(rid, None)
        return _unwrap(reply)

    async def _batch(
        self, requests: list[tuple[str, dict[str, Any] | None]], *,
        timeout: float = REQUEST_TIMEOUT_S,
    ) -> list[dict[str, Any] | ObsRequestError]:
        """Send a RequestBatch and return one entry per request, in order: the
        ``responseData`` or the ObsRequestError that request produced."""
        if not requests:
            return []
        rid = self._next_id()
        body = {
            "requestId": rid,
            "haltOnFailure": False,
            "requests": [
                {"requestType": rtype, "requestId": f"{rid}-{index}",
                 **({"requestData": rdata} if rdata else {})}
                for index, (rtype, rdata) in enumerate(requests)
            ],
        }
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"op": OP_REQUEST_BATCH, "d": body})
            reply = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"[{self.device_id}] OBS did not answer a request batch")
        except asyncio.CancelledError:
            raise ConnectionError(f"[{self.device_id}] Connection to OBS dropped during a request batch")
        finally:
            self._pending.pop(rid, None)
        results = reply.get("results") or []
        out: list[dict[str, Any] | ObsRequestError] = []
        for index in range(len(requests)):
            result = results[index] if index < len(results) else {}
            try:
                out.append(_unwrap(result))
            except ObsRequestError as exc:
                out.append(exc)
        return out

    # ── Full sync (connect, refresh, scene collection change) ─────────────

    async def refresh_children(self) -> Any:
        await self._full_sync()
        return {
            "scenes": len(self._scenes.name_by_id),
            "inputs": len(self._inputs.name_by_id),
            "scene_items": len(self._items),
            "filters": len(self._filters),
            "outputs": len(self._outputs.name_by_id),
        }

    async def _full_sync(self) -> None:
        async with self._sync_lock:
            await self._read_version_and_config()
            await self._read_transitions()
            await self._read_scenes()
            await self._read_inputs()
            await self._read_scene_items()
            await self._read_filters()
            await self._read_outputs()
            await self._read_output_status()
            await self._read_hotkeys_and_monitors()
            await self._read_stats()

    async def _read_version_and_config(self) -> None:
        results = await self._batch([
            ("GetVersion", None),
            ("GetVideoSettings", None),
            ("GetRecordDirectory", None),
            ("GetStudioModeEnabled", None),
            ("GetSceneCollectionList", None),
            ("GetProfileList", None),
        ])
        version, video, record_dir, studio, collections, profiles = results
        updates: dict[str, Any] = {}
        if isinstance(version, dict):
            updates.update({
                "obs_version": str(version.get("obsVersion", "")),
                "websocket_version": str(version.get("obsWebSocketVersion", "")),
                "rpc_version": _int(version.get("rpcVersion"), RPC_VERSION),
                "platform": str(version.get("platform", "")),
                "platform_description": str(version.get("platformDescription", "")),
            })
        if isinstance(video, dict):
            num = _num(video.get("fpsNumerator"), 0.0)
            den = _num(video.get("fpsDenominator"), 1.0) or 1.0
            updates.update({
                "base_resolution": f"{_int(video.get('baseWidth'))}x{_int(video.get('baseHeight'))}",
                "output_resolution": f"{_int(video.get('outputWidth'))}x{_int(video.get('outputHeight'))}",
                "fps": round(num / den, 3),
            })
        if isinstance(record_dir, dict):
            updates["record_directory"] = str(record_dir.get("recordDirectory", ""))
        if isinstance(studio, dict):
            self._studio_mode = _bool(studio.get("studioModeEnabled"))
            updates["studio_mode"] = self._studio_mode
        if isinstance(collections, dict):
            updates["scene_collection"] = str(collections.get("currentSceneCollectionName", ""))
            updates["scene_collection_options"] = json.dumps(
                [str(n) for n in collections.get("sceneCollections") or []])
        if isinstance(profiles, dict):
            updates["profile"] = str(profiles.get("currentProfileName", ""))
            updates["profile_options"] = json.dumps([str(n) for n in profiles.get("profiles") or []])
        if updates:
            self.set_states(updates)

    async def _read_transitions(self) -> None:
        results = await self._batch([
            ("GetSceneTransitionList", None),
            ("GetCurrentSceneTransition", None),
        ])
        listing, current = results
        updates: dict[str, Any] = {}
        if isinstance(listing, dict):
            names = [str(t.get("transitionName", "")) for t in listing.get("transitions") or []
                     if isinstance(t, dict)]
            updates["transition_options"] = json.dumps(names)
        if isinstance(current, dict):
            self._apply_current_transition(current, updates)
        if updates:
            self.set_states(updates)

    def _apply_current_transition(self, current: dict[str, Any], updates: dict[str, Any]) -> None:
        updates["transition"] = str(current.get("transitionName") or "")
        updates["transition_kind"] = str(current.get("transitionKind") or "")
        fixed = _bool(current.get("transitionFixed"))
        updates["transition_fixed"] = fixed
        duration = current.get("transitionDuration")
        if duration is not None:
            updates["transition_duration_ms"] = _int(duration)

    async def _read_scenes(self) -> None:
        listing = await self._request("GetSceneList")
        scenes = [s for s in listing.get("scenes") or [] if isinstance(s, dict)]
        # sceneIndex 0 is the bottom of OBS's list; present top first.
        scenes.sort(key=lambda s: -_int(s.get("sceneIndex")))
        program = listing.get("currentProgramSceneName") or ""
        preview = listing.get("currentPreviewSceneName") or ""
        if not self._studio_mode:
            preview = ""
        wanted: dict[str, dict[str, Any]] = {}
        for scene in scenes:
            name = str(scene.get("sceneName", ""))
            if not name:
                continue
            wanted[name] = scene
        for name in list(self._scenes.id_by_name):
            if name not in wanted:
                local = self._scenes.drop(name)
                if local is not None:
                    self.deregister_child("scene", local)
        batch: list[tuple[str, str, dict[str, Any]]] = []
        for name, scene in wanted.items():
            local = self._scenes.assign(name)
            props = {
                "name": name,
                "program": name == program,
                "preview": name == preview,
                "index": _int(scene.get("sceneIndex")),
            }
            if not self.is_child_registered("scene", local):
                self.register_child("scene", local, initial_state={**props, "item_count": 0})
            else:
                batch.append(("scene", local, props))
        if batch:
            self.set_children_state_batch(batch)
        self.set_states({
            "program_scene": str(program),
            "preview_scene": str(preview),
            "scene_options": json.dumps(list(wanted)),
            "scene_count": len(wanted),
        })
        self._publish_source_options()

    async def _read_inputs(self) -> None:
        listing = await self._request("GetInputList")
        inputs = [i for i in listing.get("inputs") or [] if isinstance(i, dict)]
        wanted: dict[str, dict[str, Any]] = {}
        for item in inputs:
            name = str(item.get("inputName", ""))
            if name:
                wanted[name] = item
        for name in list(self._inputs.id_by_name):
            if name not in wanted:
                self._drop_input(name)
        for name, item in wanted.items():
            kind = str(item.get("unversionedInputKind") or item.get("inputKind") or "")
            self._input_kinds[name] = kind
            local = self._inputs.assign(name)
            props = {
                "name": name,
                "kind": kind,
                "is_media": kind in MEDIA_INPUT_KINDS,
                "is_text": kind in TEXT_INPUT_KINDS,
            }
            if not self.is_child_registered("input", local):
                self.register_child("input", local, initial_state=props)
            else:
                self.set_child_state_batch("input", local, props)
        self.set_state("input_count", len(wanted))
        self._publish_source_options()
        await self._read_input_details(list(wanted))

    async def _read_input_details(self, names: list[str]) -> None:
        """One batch per input: audio state, active state, media status and
        text where the kind has them. A non-audio input refuses the audio
        requests, which is how has_audio is learned."""
        for name in names:
            local = self._inputs.id_by_name.get(name)
            if local is None:
                continue
            kind = self._input_kinds.get(name, "")
            requests: list[tuple[str, dict[str, Any] | None]] = [
                ("GetInputMute", {"inputName": name}),
                ("GetInputVolume", {"inputName": name}),
                ("GetInputAudioBalance", {"inputName": name}),
                ("GetInputAudioSyncOffset", {"inputName": name}),
                ("GetInputAudioMonitorType", {"inputName": name}),
                ("GetSourceActive", {"sourceName": name}),
            ]
            if kind in MEDIA_INPUT_KINDS:
                requests.append(("GetMediaInputStatus", {"inputName": name}))
            if kind in TEXT_INPUT_KINDS:
                requests.append(("GetInputSettings", {"inputName": name}))
            results = await self._batch(requests)
            mute, volume, balance, sync, monitor, active = results[:6]
            props: dict[str, Any] = {"has_audio": isinstance(mute, dict)}
            if isinstance(mute, dict):
                props["muted"] = _bool(mute.get("inputMuted"))
            if isinstance(volume, dict):
                props["volume_db"] = _db(volume.get("inputVolumeDb"))
                props["volume_mul"] = round(_num(volume.get("inputVolumeMul")), 4)
            if isinstance(balance, dict):
                props["balance"] = round(_num(balance.get("inputAudioBalance"), 0.5), 3)
            if isinstance(sync, dict):
                props["sync_offset_ms"] = _int(sync.get("inputAudioSyncOffset"))
            if isinstance(monitor, dict):
                props["monitor_type"] = MONITOR_TYPES.get(str(monitor.get("monitorType")), "none")
            if isinstance(active, dict):
                props["active"] = _bool(active.get("videoActive"))
                props["showing"] = _bool(active.get("videoShowing"))
            extra = results[6:]
            if kind in MEDIA_INPUT_KINDS:
                media = extra[0] if extra else None
                extra = extra[1:]
                if isinstance(media, dict):
                    props.update(self._media_props(media))
            if kind in TEXT_INPUT_KINDS:
                settings = extra[0] if extra else None
                if isinstance(settings, dict):
                    props["text"] = str((settings.get("inputSettings") or {}).get("text", ""))
            if self.is_child_registered("input", local):
                self.set_child_state_batch("input", local, props)

    @staticmethod
    def _media_props(media: dict[str, Any]) -> dict[str, Any]:
        return {
            "media_state": _media_state(media.get("mediaState")),
            "media_duration_ms": _int(media.get("mediaDuration")),
            "media_cursor_ms": _int(media.get("mediaCursor")),
        }

    def _drop_input(self, name: str) -> None:
        local = self._inputs.drop(name)
        self._input_kinds.pop(name, None)
        self._meter_last.pop(name, None)
        if local is not None and self.is_child_registered("input", local):
            self.deregister_child("input", local)

    async def _read_scene_items(self, scene_names: list[str] | None = None) -> None:
        names = scene_names if scene_names is not None else list(self._scenes.id_by_name)
        if not names:
            return
        results = await self._batch([("GetSceneItemList", {"sceneName": n}) for n in names])
        for scene_name, result in zip(names, results):
            if not isinstance(result, dict):
                continue
            self._apply_scene_items(scene_name, result.get("sceneItems") or [])

    def _apply_scene_items(self, scene_name: str, items: list[Any]) -> None:
        scene_local = self._scenes.id_by_name.get(scene_name)
        if scene_local is None:
            return
        seen: set[str] = set()
        batch: list[tuple[str, str, dict[str, Any]]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = _int(item.get("sceneItemId"), -1)
            if item_id < 0:
                continue
            source = str(item.get("sourceName", ""))
            local = self._item_local(scene_name, item_id)
            seen.add(local)
            if _bool(item.get("isGroup")):
                source_type = "group"
            else:
                source_type = SOURCE_TYPES.get(str(item.get("sourceType")), "input")
            props = {
                "name": f"{scene_name}: {source}",
                "scene": scene_name,
                "source": source,
                "source_type": source_type,
                "item_id": item_id,
                "enabled": _bool(item.get("sceneItemEnabled")),
                "locked": _bool(item.get("sceneItemLocked")),
                "index": _int(item.get("sceneItemIndex")),
            }
            if not self.is_child_registered("scene_item", local):
                self.register_child("scene_item", local, initial_state=props)
            else:
                batch.append(("scene_item", local, props))
        for local, (owner, _item_id) in list(self._items.items()):
            if owner == scene_name and local not in seen:
                self._drop_item(local)
        if batch:
            self.set_children_state_batch(batch)
        if self.is_child_registered("scene", scene_local):
            self.set_child_state("scene", scene_local, "item_count", len(seen))

    def _item_local(self, scene_name: str, item_id: int) -> str:
        key = (scene_name, item_id)
        local = self._item_ids.get(key)
        if local is None:
            scene_local = self._scenes.id_by_name.get(scene_name) or _sanitize_id(scene_name)
            suffix = f"__{item_id}"
            local = scene_local[:_CHILD_ID_MAX - len(suffix)] + suffix
            self._item_ids[key] = local
            self._items[local] = key
        return local

    def _drop_item(self, local: str) -> None:
        key = self._items.pop(local, None)
        if key is not None:
            self._item_ids.pop(key, None)
        if self.is_child_registered("scene_item", local):
            self.deregister_child("scene_item", local)

    async def _read_filters(self, source_names: list[str] | None = None) -> None:
        names = source_names if source_names is not None else (
            list(self._inputs.id_by_name) + list(self._scenes.id_by_name))
        if not names:
            return
        results = await self._batch([("GetSourceFilterList", {"sourceName": n}) for n in names])
        for source, result in zip(names, results):
            if not isinstance(result, dict):
                continue
            self._apply_filters(source, result.get("filters") or [])

    def _apply_filters(self, source: str, filters: list[Any]) -> None:
        seen: set[str] = set()
        batch: list[tuple[str, str, dict[str, Any]]] = []
        for entry in filters:
            if not isinstance(entry, dict):
                continue
            filter_name = str(entry.get("filterName", ""))
            if not filter_name:
                continue
            local = self._filter_local(source, filter_name)
            seen.add(local)
            props = {
                "name": f"{source}: {filter_name}",
                "source": source,
                "filter_name": filter_name,
                "kind": str(entry.get("filterKind", "")),
                "enabled": _bool(entry.get("filterEnabled")),
                "index": _int(entry.get("filterIndex")),
            }
            if not self.is_child_registered("filter", local):
                self.register_child("filter", local, initial_state=props)
            else:
                batch.append(("filter", local, props))
        for local, (owner, _fname) in list(self._filters.items()):
            if owner == source and local not in seen:
                self._drop_filter(local)
        if batch:
            self.set_children_state_batch(batch)

    def _filter_local(self, source: str, filter_name: str) -> str:
        key = (source, filter_name)
        local = self._filter_ids.get(key)
        if local is None:
            source_local = (self._inputs.id_by_name.get(source)
                            or self._scenes.id_by_name.get(source) or _sanitize_id(source))
            base = f"{source_local}__{_sanitize_id(filter_name)}"[:_CHILD_ID_MAX]
            local = base
            n = 2
            while local in self._filters:
                suffix = f"_{n}"
                local = base[:_CHILD_ID_MAX - len(suffix)] + suffix
                n += 1
            self._filter_ids[key] = local
            self._filters[local] = key
        return local

    def _drop_filter(self, local: str) -> None:
        key = self._filters.pop(local, None)
        if key is not None:
            self._filter_ids.pop(key, None)
        if self.is_child_registered("filter", local):
            self.deregister_child("filter", local)

    async def _read_outputs(self) -> None:
        listing = await self._request("GetOutputList")
        outputs = [o for o in listing.get("outputs") or [] if isinstance(o, dict)]
        wanted = {str(o.get("outputName", "")): o for o in outputs if o.get("outputName")}
        for name in list(self._outputs.id_by_name):
            if name not in wanted:
                local = self._outputs.drop(name)
                if local is not None and self.is_child_registered("output", local):
                    self.deregister_child("output", local)
        batch: list[tuple[str, str, dict[str, Any]]] = []
        for name, output in wanted.items():
            local = self._outputs.assign(name)
            props = {
                "name": name,
                "kind": str(output.get("outputKind", "")),
                "active": _bool(output.get("outputActive")),
                "width": _int(output.get("outputWidth")),
                "height": _int(output.get("outputHeight")),
            }
            if not self.is_child_registered("output", local):
                self.register_child("output", local, initial_state={**props, "reconnecting": False})
            else:
                batch.append(("output", local, props))
        if batch:
            self.set_children_state_batch(batch)

    async def _read_output_status(self) -> None:
        stream, record, vcam, replay, last_replay = await self._batch([
            ("GetStreamStatus", None),
            ("GetRecordStatus", None),
            ("GetVirtualCamStatus", None),
            ("GetReplayBufferStatus", None),
            ("GetLastReplayBufferReplay", None),
        ])
        updates: dict[str, Any] = {}
        if isinstance(stream, dict):
            updates.update({
                "streaming": _bool(stream.get("outputActive")),
                "stream_reconnecting": _bool(stream.get("outputReconnecting")),
                "stream_timecode": str(stream.get("outputTimecode", "")),
                "stream_duration_ms": _int(stream.get("outputDuration")),
                "stream_congestion": round(_num(stream.get("outputCongestion")), 3),
                "stream_bytes": _int(stream.get("outputBytes")),
                "stream_skipped_frames": _int(stream.get("outputSkippedFrames")),
                "stream_total_frames": _int(stream.get("outputTotalFrames")),
            })
            # An event names the transient states (starting, stopping,
            # reconnecting); the poll only settles the steady ones.
            if self.get_state("stream_state") not in TRANSIENT_OUTPUT_STATES:
                updates["stream_state"] = "started" if updates["streaming"] else "stopped"
        if isinstance(record, dict):
            updates.update({
                "recording": _bool(record.get("outputActive")),
                "record_paused": _bool(record.get("outputPaused")),
                "record_timecode": str(record.get("outputTimecode", "")),
                "record_duration_ms": _int(record.get("outputDuration")),
                "record_bytes": _int(record.get("outputBytes")),
            })
            if self.get_state("record_state") not in TRANSIENT_OUTPUT_STATES:
                if updates["recording"]:
                    updates["record_state"] = "paused" if updates["record_paused"] else "started"
                else:
                    updates["record_state"] = "stopped"
        # A status OBS refuses (the replay buffer is off in its settings) is
        # an output that cannot be active.
        updates["virtual_camera"] = isinstance(vcam, dict) and _bool(vcam.get("outputActive"))
        updates["replay_buffer"] = isinstance(replay, dict) and _bool(replay.get("outputActive"))
        if isinstance(last_replay, dict):
            updates["last_replay_file"] = str(last_replay.get("savedReplayPath") or "")
        if updates:
            self.set_states(updates)

    async def _read_hotkeys_and_monitors(self) -> None:
        hotkeys, monitors = await self._batch([("GetHotkeyList", None), ("GetMonitorList", None)])
        updates: dict[str, Any] = {}
        if isinstance(hotkeys, dict):
            updates["hotkey_options"] = json.dumps([str(h) for h in hotkeys.get("hotkeys") or []])
        if isinstance(monitors, dict):
            options = [{"value": "-1", "label": "Windowed"}]
            for mon in monitors.get("monitors") or []:
                if not isinstance(mon, dict):
                    continue
                index = _int(mon.get("monitorIndex"), -1)
                name = str(mon.get("monitorName") or f"Display {index + 1}")
                size = f"{_int(mon.get('monitorWidth'))}x{_int(mon.get('monitorHeight'))}"
                options.append({"value": str(index), "label": f"{name} ({size})"})
            updates["monitor_options"] = json.dumps(options)
        if updates:
            self.set_states(updates)

    async def _read_stats(self) -> None:
        stats = await self._request("GetStats")
        self.set_states({
            "cpu_percent": round(_num(stats.get("cpuUsage")), 1),
            "memory_mb": round(_num(stats.get("memoryUsage")), 1),
            "disk_free_mb": round(_num(stats.get("availableDiskSpace")), 1),
            "active_fps": round(_num(stats.get("activeFps")), 2),
            "frame_render_ms": round(_num(stats.get("averageFrameRenderTime")), 3),
            "render_skipped_frames": _int(stats.get("renderSkippedFrames")),
            "render_total_frames": _int(stats.get("renderTotalFrames")),
            "output_skipped_frames": _int(stats.get("outputSkippedFrames")),
            "output_total_frames": _int(stats.get("outputTotalFrames")),
        })

    def _publish_source_options(self) -> None:
        names = list(self._inputs.id_by_name) + list(self._scenes.id_by_name)
        self.set_state("source_options", json.dumps(names))

    # ── Poll ──────────────────────────────────────────────────────────────

    async def poll(self) -> None:
        if self._collection_changing:
            return
        if self._sync_lock.locked():
            return
        await self._read_stats()
        await self._read_output_status()
        await self._read_outputs()
        current = await self._request("GetCurrentSceneTransition")
        updates: dict[str, Any] = {}
        self._apply_current_transition(current, updates)
        self.set_states(updates)
        media = [n for n, k in self._input_kinds.items() if k in MEDIA_INPUT_KINDS]
        if media:
            results = await self._batch([("GetMediaInputStatus", {"inputName": n}) for n in media])
            for name, result in zip(media, results):
                local = self._inputs.id_by_name.get(name)
                if local is not None and isinstance(result, dict) and self.is_child_registered("input", local):
                    self.set_child_state_batch("input", local, self._media_props(result))

    # ── Events ────────────────────────────────────────────────────────────

    async def _handle_event(self, event: str, data: dict[str, Any]) -> None:
        handler = self._EVENTS.get(event)
        if handler is not None:
            await handler(self, data)

    async def _ev_exit_started(self, data: dict[str, Any]) -> None:
        log.info(f"[{self.device_id}] OBS is shutting down")

    async def _ev_collection_changing(self, data: dict[str, Any]) -> None:
        # Requests during a scene collection change are undefined behaviour
        # per the protocol reference; polling waits for the Changed event.
        self._collection_changing = True

    async def _ev_collection_changed(self, data: dict[str, Any]) -> None:
        self._collection_changing = False
        self.set_state("scene_collection", str(data.get("sceneCollectionName", "")))
        await self._full_sync()

    async def _ev_collection_list(self, data: dict[str, Any]) -> None:
        self.set_state("scene_collection_options",
                       json.dumps([str(n) for n in data.get("sceneCollections") or []]))

    async def _ev_profile_changed(self, data: dict[str, Any]) -> None:
        self.set_state("profile", str(data.get("profileName", "")))
        await self._read_version_and_config()

    async def _ev_profile_list(self, data: dict[str, Any]) -> None:
        self.set_state("profile_options", json.dumps([str(n) for n in data.get("profiles") or []]))

    async def _ev_scene_list(self, data: dict[str, Any]) -> None:
        await self._read_scenes()
        await self._read_scene_items()

    async def _ev_scene_created(self, data: dict[str, Any]) -> None:
        if _bool(data.get("isGroup")):
            return
        await self._read_scenes()
        name = str(data.get("sceneName", ""))
        if name:
            await self._read_scene_items([name])
            await self._read_filters([name])

    async def _ev_scene_removed(self, data: dict[str, Any]) -> None:
        if _bool(data.get("isGroup")):
            return
        name = str(data.get("sceneName", ""))
        for local, (owner, _item_id) in list(self._items.items()):
            if owner == name:
                self._drop_item(local)
        for local, (owner, _fname) in list(self._filters.items()):
            if owner == name:
                self._drop_filter(local)
        await self._read_scenes()

    async def _ev_scene_renamed(self, data: dict[str, Any]) -> None:
        old = str(data.get("oldSceneName", ""))
        for local, (owner, _item_id) in list(self._items.items()):
            if owner == old:
                self._drop_item(local)
        for local, (owner, _fname) in list(self._filters.items()):
            if owner == old:
                self._drop_filter(local)
        local = self._scenes.drop(old)
        if local is not None and self.is_child_registered("scene", local):
            self.deregister_child("scene", local)
        await self._read_scenes()
        new = str(data.get("sceneName", ""))
        if new:
            await self._read_scene_items([new])
            await self._read_filters([new])

    async def _ev_program_scene(self, data: dict[str, Any]) -> None:
        name = str(data.get("sceneName", ""))
        self.set_state("program_scene", name)
        self._flag_scene("program", name)

    async def _ev_preview_scene(self, data: dict[str, Any]) -> None:
        name = str(data.get("sceneName", "")) if self._studio_mode else ""
        self.set_state("preview_scene", name)
        self._flag_scene("preview", name)

    def _flag_scene(self, prop: str, name: str) -> None:
        batch = []
        for scene_name, local in self._scenes.id_by_name.items():
            if self.is_child_registered("scene", local):
                batch.append(("scene", local, {prop: scene_name == name}))
        if batch:
            self.set_children_state_batch(batch)

    async def _ev_studio_mode(self, data: dict[str, Any]) -> None:
        self._studio_mode = _bool(data.get("studioModeEnabled"))
        self.set_state("studio_mode", self._studio_mode)
        if self._studio_mode:
            try:
                current = await self._request("GetCurrentPreviewScene")
            except ObsRequestError:
                current = {}
            name = str(current.get("sceneName") or "")
        else:
            name = ""
        self.set_state("preview_scene", name)
        self._flag_scene("preview", name)

    async def _ev_transition_changed(self, data: dict[str, Any]) -> None:
        current = await self._request("GetCurrentSceneTransition")
        updates: dict[str, Any] = {}
        self._apply_current_transition(current, updates)
        self.set_states(updates)

    async def _ev_transition_duration(self, data: dict[str, Any]) -> None:
        self.set_state("transition_duration_ms", _int(data.get("transitionDuration")))

    async def _ev_transition_started(self, data: dict[str, Any]) -> None:
        self.set_state("transition_in_progress", True)

    async def _ev_transition_ended(self, data: dict[str, Any]) -> None:
        self.set_state("transition_in_progress", False)

    async def _ev_stream_state(self, data: dict[str, Any]) -> None:
        state = _output_state(data.get("outputState"))
        self.set_states({
            "streaming": _output_active(data, state),
            "stream_state": state,
            "stream_reconnecting": state == "reconnecting",
        })
        await self._outputs_settled(state)

    async def _outputs_settled(self, state: str) -> None:
        """The stream, record and virtual camera outputs are also entries in
        the output list; re-read it once one of them has settled so the
        output children follow without waiting for a poll."""
        if state in ("started", "stopped"):
            try:
                await self._read_outputs()
            except ObsRequestError:
                pass

    async def _ev_record_state(self, data: dict[str, Any]) -> None:
        state = _output_state(data.get("outputState"))
        updates: dict[str, Any] = {
            "recording": _output_active(data, state),
            "record_state": state,
        }
        if state == "paused":
            updates["record_paused"] = True
            self._paused_event.set()
        elif state in ("resumed", "started", "stopped", "starting", "stopping"):
            updates["record_paused"] = False
        path = data.get("outputPath")
        if path:
            updates["record_file"] = str(path)
        self.set_states(updates)
        await self._outputs_settled(state)

    async def _ev_record_file(self, data: dict[str, Any]) -> None:
        self.set_state("record_file", str(data.get("newOutputPath") or ""))

    async def _ev_replay_state(self, data: dict[str, Any]) -> None:
        self.set_state("replay_buffer", _output_active(data, _output_state(data.get("outputState"))))

    async def _ev_replay_saved(self, data: dict[str, Any]) -> None:
        self.set_state("last_replay_file", str(data.get("savedReplayPath") or ""))

    async def _ev_vcam_state(self, data: dict[str, Any]) -> None:
        state = _output_state(data.get("outputState"))
        self.set_state("virtual_camera", _output_active(data, state))
        await self._outputs_settled(state)

    async def _ev_input_created(self, data: dict[str, Any]) -> None:
        name = str(data.get("inputName", ""))
        if not name:
            return
        kind = str(data.get("unversionedInputKind") or data.get("inputKind") or "")
        self._input_kinds[name] = kind
        local = self._inputs.assign(name)
        props = {"name": name, "kind": kind, "is_media": kind in MEDIA_INPUT_KINDS,
                 "is_text": kind in TEXT_INPUT_KINDS}
        if not self.is_child_registered("input", local):
            self.register_child("input", local, initial_state=props)
        self.set_state("input_count", len(self._inputs.id_by_name))
        self._publish_source_options()
        await self._read_input_details([name])

    async def _ev_input_removed(self, data: dict[str, Any]) -> None:
        name = str(data.get("inputName", ""))
        self._drop_input(name)
        for local, (owner, _fname) in list(self._filters.items()):
            if owner == name:
                self._drop_filter(local)
        self.set_state("input_count", len(self._inputs.id_by_name))
        self._publish_source_options()

    async def _ev_input_renamed(self, data: dict[str, Any]) -> None:
        old = str(data.get("oldInputName", ""))
        new = str(data.get("inputName", ""))
        for local, (owner, _fname) in list(self._filters.items()):
            if owner == old:
                self._drop_filter(local)
        self._drop_input(old)
        await self._ev_input_created({"inputName": new, "inputKind": data.get("inputKind", "")})
        await self._read_filters([new])
        # Scene items name their source; re-read every scene so the labels follow.
        await self._read_scene_items()

    def _input_local(self, data: dict[str, Any]) -> str | None:
        local = self._inputs.id_by_name.get(str(data.get("inputName", "")))
        if local is None or not self.is_child_registered("input", local):
            return None
        return local

    async def _ev_input_mute(self, data: dict[str, Any]) -> None:
        local = self._input_local(data)
        if local:
            self.set_child_state_batch("input", local, {
                "has_audio": True, "muted": _bool(data.get("inputMuted"))})

    async def _ev_input_volume(self, data: dict[str, Any]) -> None:
        local = self._input_local(data)
        if local:
            self.set_child_state_batch("input", local, {
                "has_audio": True,
                "volume_db": _db(data.get("inputVolumeDb")),
                "volume_mul": round(_num(data.get("inputVolumeMul")), 4),
            })

    async def _ev_input_balance(self, data: dict[str, Any]) -> None:
        local = self._input_local(data)
        if local:
            self.set_child_state("input", local, "balance",
                                 round(_num(data.get("inputAudioBalance"), 0.5), 3))

    async def _ev_input_sync(self, data: dict[str, Any]) -> None:
        local = self._input_local(data)
        if local:
            self.set_child_state("input", local, "sync_offset_ms", _int(data.get("inputAudioSyncOffset")))

    async def _ev_input_monitor(self, data: dict[str, Any]) -> None:
        local = self._input_local(data)
        if local:
            self.set_child_state("input", local, "monitor_type",
                                 MONITOR_TYPES.get(str(data.get("monitorType")), "none"))

    async def _ev_input_active(self, data: dict[str, Any]) -> None:
        local = self._input_local(data)
        if local:
            self.set_child_state("input", local, "active", _bool(data.get("videoActive")))

    async def _ev_input_showing(self, data: dict[str, Any]) -> None:
        local = self._input_local(data)
        if local:
            self.set_child_state("input", local, "showing", _bool(data.get("videoShowing")))

    async def _ev_input_settings(self, data: dict[str, Any]) -> None:
        name = str(data.get("inputName", ""))
        if self._input_kinds.get(name) not in TEXT_INPUT_KINDS:
            return
        local = self._input_local(data)
        settings = data.get("inputSettings") or {}
        if local and "text" in settings:
            self.set_child_state("input", local, "text", str(settings.get("text", "")))

    async def _ev_meters(self, data: dict[str, Any]) -> None:
        now = asyncio.get_event_loop().time()
        batch: list[tuple[str, str, dict[str, Any]]] = []
        for entry in data.get("inputs") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("inputName", ""))
            local = self._inputs.id_by_name.get(name)
            if local is None or not self.is_child_registered("input", local):
                continue
            last = self._meter_last.get(name, 0.0)
            if now - last < METER_MIN_INTERVAL_S:
                continue
            peak = METER_FLOOR_DB
            for channel in entry.get("inputLevelsMul") or []:
                if isinstance(channel, (list, tuple)) and len(channel) >= 2:
                    peak = max(peak, _mul_to_db(channel[1]))
            self._meter_last[name] = now
            batch.append(("input", local, {"level_db": peak}))
        if batch:
            self.set_children_state_batch(batch)

    async def _ev_media_started(self, data: dict[str, Any]) -> None:
        await self._refresh_media(data)

    async def _ev_media_ended(self, data: dict[str, Any]) -> None:
        await self._refresh_media(data)

    async def _ev_media_action(self, data: dict[str, Any]) -> None:
        await self._refresh_media(data)

    async def _refresh_media(self, data: dict[str, Any]) -> None:
        name = str(data.get("inputName", ""))
        local = self._input_local(data)
        if not local:
            return
        try:
            media = await self._request("GetMediaInputStatus", {"inputName": name})
        except ObsRequestError:
            return
        self.set_child_state_batch("input", local, self._media_props(media))

    async def _ev_item_created(self, data: dict[str, Any]) -> None:
        await self._read_scene_items([str(data.get("sceneName", ""))])

    async def _ev_item_removed(self, data: dict[str, Any]) -> None:
        local = self._item_ids.get((str(data.get("sceneName", "")), _int(data.get("sceneItemId"), -1)))
        if local is not None:
            self._drop_item(local)
        scene_local = self._scenes.id_by_name.get(str(data.get("sceneName", "")))
        if scene_local and self.is_child_registered("scene", scene_local):
            count = sum(1 for owner, _i in self._items.values() if owner == data.get("sceneName"))
            self.set_child_state("scene", scene_local, "item_count", count)

    async def _ev_item_reindexed(self, data: dict[str, Any]) -> None:
        scene_name = str(data.get("sceneName", ""))
        batch = []
        for item in data.get("sceneItems") or []:
            if not isinstance(item, dict):
                continue
            local = self._item_ids.get((scene_name, _int(item.get("sceneItemId"), -1)))
            if local is not None and self.is_child_registered("scene_item", local):
                batch.append(("scene_item", local, {"index": _int(item.get("sceneItemIndex"))}))
        if batch:
            self.set_children_state_batch(batch)

    async def _ev_item_enabled(self, data: dict[str, Any]) -> None:
        local = self._item_ids.get((str(data.get("sceneName", "")), _int(data.get("sceneItemId"), -1)))
        if local is not None and self.is_child_registered("scene_item", local):
            self.set_child_state("scene_item", local, "enabled", _bool(data.get("sceneItemEnabled")))

    async def _ev_item_locked(self, data: dict[str, Any]) -> None:
        local = self._item_ids.get((str(data.get("sceneName", "")), _int(data.get("sceneItemId"), -1)))
        if local is not None and self.is_child_registered("scene_item", local):
            self.set_child_state("scene_item", local, "locked", _bool(data.get("sceneItemLocked")))

    async def _ev_filter_created(self, data: dict[str, Any]) -> None:
        await self._read_filters([str(data.get("sourceName", ""))])

    async def _ev_filter_removed(self, data: dict[str, Any]) -> None:
        local = self._filter_ids.get((str(data.get("sourceName", "")), str(data.get("filterName", ""))))
        if local is not None:
            self._drop_filter(local)

    async def _ev_filter_renamed(self, data: dict[str, Any]) -> None:
        local = self._filter_ids.get((str(data.get("sourceName", "")), str(data.get("oldFilterName", ""))))
        if local is not None:
            self._drop_filter(local)
        await self._read_filters([str(data.get("sourceName", ""))])

    async def _ev_filter_enabled(self, data: dict[str, Any]) -> None:
        local = self._filter_ids.get((str(data.get("sourceName", "")), str(data.get("filterName", ""))))
        if local is not None and self.is_child_registered("filter", local):
            self.set_child_state("filter", local, "enabled", _bool(data.get("filterEnabled")))

    _EVENTS = {
        "ExitStarted": _ev_exit_started,
        "CurrentSceneCollectionChanging": _ev_collection_changing,
        "CurrentSceneCollectionChanged": _ev_collection_changed,
        "SceneCollectionListChanged": _ev_collection_list,
        "CurrentProfileChanged": _ev_profile_changed,
        "ProfileListChanged": _ev_profile_list,
        "SceneListChanged": _ev_scene_list,
        "SceneCreated": _ev_scene_created,
        "SceneRemoved": _ev_scene_removed,
        "SceneNameChanged": _ev_scene_renamed,
        "CurrentProgramSceneChanged": _ev_program_scene,
        "CurrentPreviewSceneChanged": _ev_preview_scene,
        "StudioModeStateChanged": _ev_studio_mode,
        "CurrentSceneTransitionChanged": _ev_transition_changed,
        "CurrentSceneTransitionDurationChanged": _ev_transition_duration,
        "SceneTransitionStarted": _ev_transition_started,
        "SceneTransitionEnded": _ev_transition_ended,
        "StreamStateChanged": _ev_stream_state,
        "RecordStateChanged": _ev_record_state,
        "RecordFileChanged": _ev_record_file,
        "ReplayBufferStateChanged": _ev_replay_state,
        "ReplayBufferSaved": _ev_replay_saved,
        "VirtualcamStateChanged": _ev_vcam_state,
        "InputCreated": _ev_input_created,
        "InputRemoved": _ev_input_removed,
        "InputNameChanged": _ev_input_renamed,
        "InputMuteStateChanged": _ev_input_mute,
        "InputVolumeChanged": _ev_input_volume,
        "InputAudioBalanceChanged": _ev_input_balance,
        "InputAudioSyncOffsetChanged": _ev_input_sync,
        "InputAudioMonitorTypeChanged": _ev_input_monitor,
        "InputActiveStateChanged": _ev_input_active,
        "InputShowStateChanged": _ev_input_showing,
        "InputSettingsChanged": _ev_input_settings,
        "InputVolumeMeters": _ev_meters,
        "MediaInputPlaybackStarted": _ev_media_started,
        "MediaInputPlaybackEnded": _ev_media_ended,
        "MediaInputActionTriggered": _ev_media_action,
        "SceneItemCreated": _ev_item_created,
        "SceneItemRemoved": _ev_item_removed,
        "SceneItemListReindexed": _ev_item_reindexed,
        "SceneItemEnableStateChanged": _ev_item_enabled,
        "SceneItemLockStateChanged": _ev_item_locked,
        "SourceFilterCreated": _ev_filter_created,
        "SourceFilterRemoved": _ev_filter_removed,
        "SourceFilterNameChanged": _ev_filter_renamed,
        "SourceFilterEnableStateChanged": _ev_filter_enabled,
    }

    # ── Commands ──────────────────────────────────────────────────────────

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        handler = self._DISPATCH.get(command)
        if handler is None:
            raise ValueError(f"Unknown command: {command}")
        if not self._link_alive():
            raise ConnectionError(f"[{self.device_id}] Not connected to OBS")
        try:
            result = await handler(self, params)
        except ObsRequestError as exc:
            self.set_state("last_error", f"{exc.request_type}: {exc.text}")
            raise ValueError(f"OBS refused {command}: {exc.text}") from exc
        self.set_state("last_error", "")
        return result

    # Child lookups

    def _scene_name(self, params: dict[str, Any], key: str = "scene") -> str:
        local = str(params.get(key, ""))
        name = self._scenes.name_by_id.get(local)
        if name is None:
            raise ValueError(f"Unknown scene {local!r}")
        return name

    def _input_name(self, params: dict[str, Any]) -> str:
        local = str(params.get("input", ""))
        name = self._inputs.name_by_id.get(local)
        if name is None:
            raise ValueError(f"Unknown input {local!r}")
        return name

    def _output_name(self, params: dict[str, Any]) -> str:
        local = str(params.get("output", ""))
        name = self._outputs.name_by_id.get(local)
        if name is None:
            raise ValueError(f"Unknown output {local!r}")
        return name

    def _item_ref(self, params: dict[str, Any]) -> tuple[str, int]:
        local = str(params.get("item", ""))
        ref = self._items.get(local)
        if ref is None:
            raise ValueError(f"Unknown scene item {local!r}")
        return ref

    def _filter_ref(self, params: dict[str, Any]) -> tuple[str, str]:
        local = str(params.get("filter", ""))
        ref = self._filters.get(local)
        if ref is None:
            raise ValueError(f"Unknown filter {local!r}")
        return ref

    @staticmethod
    def _monitor_index(params: dict[str, Any]) -> int:
        return _int(params.get("monitor", -1), -1)

    # Scenes and transitions

    async def _cmd_set_program_scene(self, params: dict[str, Any]) -> None:
        await self._request("SetCurrentProgramScene", {"sceneName": self._scene_name(params)})

    async def _cmd_set_preview_scene(self, params: dict[str, Any]) -> None:
        await self._request("SetCurrentPreviewScene", {"sceneName": self._scene_name(params)})

    async def _cmd_transition(self, params: dict[str, Any]) -> None:
        await self._request("TriggerStudioModeTransition")

    async def _cmd_set_studio_mode(self, params: dict[str, Any]) -> None:
        await self._request("SetStudioModeEnabled", {"studioModeEnabled": _bool(params.get("enabled"))})

    async def _cmd_studio_mode_on(self, params: dict[str, Any]) -> None:
        await self._request("SetStudioModeEnabled", {"studioModeEnabled": True})

    async def _cmd_studio_mode_off(self, params: dict[str, Any]) -> None:
        await self._request("SetStudioModeEnabled", {"studioModeEnabled": False})

    async def _cmd_set_transition(self, params: dict[str, Any]) -> None:
        await self._request("SetCurrentSceneTransition", {"transitionName": str(params.get("name", ""))})

    async def _cmd_set_transition_duration(self, params: dict[str, Any]) -> None:
        await self._request("SetCurrentSceneTransitionDuration",
                            {"transitionDuration": _int(params.get("duration_ms"))})

    async def _cmd_set_tbar_position(self, params: dict[str, Any]) -> None:
        release = params.get("release")
        await self._request("SetTBarPosition", {
            "position": max(0.0, min(1.0, _num(params.get("position")))),
            "release": True if release is None else _bool(release),
        })

    # Stream

    async def _cmd_start_stream(self, params: dict[str, Any]) -> None:
        await self._request("StartStream")

    async def _cmd_stop_stream(self, params: dict[str, Any]) -> None:
        await self._request("StopStream")

    async def _cmd_toggle_stream(self, params: dict[str, Any]) -> bool:
        return _bool((await self._request("ToggleStream")).get("outputActive"))

    async def _cmd_send_caption(self, params: dict[str, Any]) -> None:
        await self._request("SendStreamCaption", {"captionText": str(params.get("text", ""))})

    # Record

    async def _cmd_start_record(self, params: dict[str, Any]) -> None:
        await self._request("StartRecord")

    async def _cmd_stop_record(self, params: dict[str, Any]) -> str:
        result = await self._request("StopRecord")
        path = str(result.get("outputPath") or "")
        if path:
            self.set_state("record_file", path)
        return path

    async def _cmd_toggle_record(self, params: dict[str, Any]) -> bool:
        return _bool((await self._request("ToggleRecord")).get("outputActive"))

    async def _cmd_pause_record(self, params: dict[str, Any]) -> None:
        self._paused_event.clear()
        await self._request("PauseRecord")
        await self._confirm_paused()

    async def _confirm_paused(self) -> None:
        """PauseRecord is acknowledged before it happens; hold the command
        until OBS reports the pause, and say so when it never does."""
        try:
            await asyncio.wait_for(self._paused_event.wait(), timeout=PAUSE_CONFIRM_S)
        except asyncio.TimeoutError:
            raise ValueError(
                "OBS accepted the pause but the recording did not pause. OBS cannot "
                "pause a recording that shares the stream encoder (Simple output mode "
                "with Recording Quality set to Same as stream) or a custom FFmpeg output."
            )

    async def _cmd_resume_record(self, params: dict[str, Any]) -> None:
        await self._request("ResumeRecord")

    async def _cmd_toggle_record_pause(self, params: dict[str, Any]) -> None:
        pausing = bool(self.get_state("recording")) and not bool(self.get_state("record_paused"))
        self._paused_event.clear()
        await self._request("ToggleRecordPause")
        if pausing:
            await self._confirm_paused()

    async def _cmd_split_record_file(self, params: dict[str, Any]) -> None:
        await self._request("SplitRecordFile")

    async def _cmd_create_record_chapter(self, params: dict[str, Any]) -> None:
        name = str(params.get("name") or "").strip()
        await self._request("CreateRecordChapter", {"chapterName": name} if name else None)

    # Virtual camera, replay buffer, outputs

    async def _cmd_start_virtual_camera(self, params: dict[str, Any]) -> None:
        await self._request("StartVirtualCam")

    async def _cmd_stop_virtual_camera(self, params: dict[str, Any]) -> None:
        await self._request("StopVirtualCam")

    async def _cmd_toggle_virtual_camera(self, params: dict[str, Any]) -> bool:
        return _bool((await self._request("ToggleVirtualCam")).get("outputActive"))

    async def _cmd_start_replay_buffer(self, params: dict[str, Any]) -> None:
        await self._request("StartReplayBuffer")

    async def _cmd_stop_replay_buffer(self, params: dict[str, Any]) -> None:
        await self._request("StopReplayBuffer")

    async def _cmd_toggle_replay_buffer(self, params: dict[str, Any]) -> bool:
        return _bool((await self._request("ToggleReplayBuffer")).get("outputActive"))

    async def _cmd_save_replay_buffer(self, params: dict[str, Any]) -> None:
        await self._request("SaveReplayBuffer")

    async def _cmd_start_output(self, params: dict[str, Any]) -> None:
        await self._request("StartOutput", {"outputName": self._output_name(params)})
        await self._read_outputs()

    async def _cmd_stop_output(self, params: dict[str, Any]) -> None:
        await self._request("StopOutput", {"outputName": self._output_name(params)})
        await self._read_outputs()

    async def _cmd_toggle_output(self, params: dict[str, Any]) -> bool:
        result = await self._request("ToggleOutput", {"outputName": self._output_name(params)})
        await self._read_outputs()
        return _bool(result.get("outputActive"))

    # Inputs: audio

    async def _cmd_mute_input(self, params: dict[str, Any]) -> None:
        await self._request("SetInputMute", {"inputName": self._input_name(params), "inputMuted": True})

    async def _cmd_unmute_input(self, params: dict[str, Any]) -> None:
        await self._request("SetInputMute", {"inputName": self._input_name(params), "inputMuted": False})

    async def _cmd_toggle_input_mute(self, params: dict[str, Any]) -> bool:
        result = await self._request("ToggleInputMute", {"inputName": self._input_name(params)})
        return _bool(result.get("inputMuted"))

    async def _cmd_set_input_volume(self, params: dict[str, Any]) -> None:
        level = max(VOLUME_DB_MIN, min(VOLUME_DB_MAX, _num(params.get("level_db"))))
        await self._request("SetInputVolume", {"inputName": self._input_name(params), "inputVolumeDb": level})

    async def _cmd_adjust_input_volume(self, params: dict[str, Any]) -> float:
        name = self._input_name(params)
        current = await self._request("GetInputVolume", {"inputName": name})
        level = _db(current.get("inputVolumeDb")) + _num(params.get("delta_db"))
        level = max(VOLUME_DB_MIN, min(VOLUME_DB_MAX, round(level, 1)))
        await self._request("SetInputVolume", {"inputName": name, "inputVolumeDb": level})
        return level

    async def _cmd_set_input_balance(self, params: dict[str, Any]) -> None:
        balance = max(0.0, min(1.0, _num(params.get("balance"), 0.5)))
        await self._request("SetInputAudioBalance", {"inputName": self._input_name(params), "inputAudioBalance": balance})

    async def _cmd_set_input_sync_offset(self, params: dict[str, Any]) -> None:
        await self._request("SetInputAudioSyncOffset", {
            "inputName": self._input_name(params), "inputAudioSyncOffset": _int(params.get("offset_ms"))})

    async def _cmd_set_input_monitoring(self, params: dict[str, Any]) -> None:
        wire = MONITOR_TYPES_WIRE.get(str(params.get("monitor_type", "none")))
        if wire is None:
            raise ValueError(f"Unknown monitoring type {params.get('monitor_type')!r}")
        await self._request("SetInputAudioMonitorType", {"inputName": self._input_name(params), "monitorType": wire})

    # Inputs: media, text, browser

    async def _media(self, params: dict[str, Any], action: str) -> None:
        await self._request("TriggerMediaInputAction", {
            "inputName": self._input_name(params),
            "mediaAction": f"OBS_WEBSOCKET_MEDIA_INPUT_ACTION_{action.upper()}",
        })

    async def _cmd_media_play(self, params: dict[str, Any]) -> None:
        await self._media(params, "play")

    async def _cmd_media_pause(self, params: dict[str, Any]) -> None:
        await self._media(params, "pause")

    async def _cmd_media_stop(self, params: dict[str, Any]) -> None:
        await self._media(params, "stop")

    async def _cmd_media_restart(self, params: dict[str, Any]) -> None:
        await self._media(params, "restart")

    async def _cmd_media_next(self, params: dict[str, Any]) -> None:
        await self._media(params, "next")

    async def _cmd_media_previous(self, params: dict[str, Any]) -> None:
        await self._media(params, "previous")

    async def _cmd_set_media_position(self, params: dict[str, Any]) -> None:
        await self._request("SetMediaInputCursor", {
            "inputName": self._input_name(params), "mediaCursor": max(0, _int(params.get("position_ms")))})
        await self._refresh_media({"inputName": self._input_name(params)})

    async def _cmd_offset_media_position(self, params: dict[str, Any]) -> None:
        await self._request("OffsetMediaInputCursor", {
            "inputName": self._input_name(params), "mediaCursorOffset": _int(params.get("offset_ms"))})
        await self._refresh_media({"inputName": self._input_name(params)})

    async def _cmd_set_text(self, params: dict[str, Any]) -> None:
        name = self._input_name(params)
        text = str(params.get("text", ""))
        await self._request("SetInputSettings", {"inputName": name, "inputSettings": {"text": text}, "overlay": True})
        local = self._inputs.id_by_name.get(name)
        if local and self.is_child_registered("input", local):
            self.set_child_state("input", local, "text", text)

    async def _cmd_refresh_browser_source(self, params: dict[str, Any]) -> None:
        await self._request("PressInputPropertiesButton", {
            "inputName": self._input_name(params), "propertyName": "refreshnocache"})

    # Scene items

    async def _set_item_enabled(self, params: dict[str, Any], enabled: bool) -> None:
        scene, item_id = self._item_ref(params)
        await self._request("SetSceneItemEnabled", {
            "sceneName": scene, "sceneItemId": item_id, "sceneItemEnabled": enabled})

    async def _cmd_show_scene_item(self, params: dict[str, Any]) -> None:
        await self._set_item_enabled(params, True)

    async def _cmd_hide_scene_item(self, params: dict[str, Any]) -> None:
        await self._set_item_enabled(params, False)

    async def _cmd_toggle_scene_item(self, params: dict[str, Any]) -> bool:
        scene, item_id = self._item_ref(params)
        current = await self._request("GetSceneItemEnabled", {"sceneName": scene, "sceneItemId": item_id})
        enabled = not _bool(current.get("sceneItemEnabled"))
        await self._request("SetSceneItemEnabled", {
            "sceneName": scene, "sceneItemId": item_id, "sceneItemEnabled": enabled})
        return enabled

    async def _cmd_set_source_visible(self, params: dict[str, Any]) -> None:
        scene = self._scene_name(params)
        source = str(params.get("source", ""))
        found = await self._request("GetSceneItemId", {"sceneName": scene, "sourceName": source})
        await self._request("SetSceneItemEnabled", {
            "sceneName": scene, "sceneItemId": _int(found.get("sceneItemId")),
            "sceneItemEnabled": _bool(params.get("visible"))})

    # Filters

    async def _set_filter_enabled(self, params: dict[str, Any], enabled: bool) -> None:
        source, filter_name = self._filter_ref(params)
        await self._request("SetSourceFilterEnabled", {
            "sourceName": source, "filterName": filter_name, "filterEnabled": enabled})

    async def _cmd_enable_filter(self, params: dict[str, Any]) -> None:
        await self._set_filter_enabled(params, True)

    async def _cmd_disable_filter(self, params: dict[str, Any]) -> None:
        await self._set_filter_enabled(params, False)

    async def _cmd_toggle_filter(self, params: dict[str, Any]) -> bool:
        local = str(params.get("filter", ""))
        self._filter_ref(params)
        enabled = not _bool(self.get_child_state("filter", local).get("enabled"))
        await self._set_filter_enabled(params, enabled)
        return enabled

    # Collections, profiles, hotkeys, UI

    async def _cmd_set_scene_collection(self, params: dict[str, Any]) -> None:
        await self._request("SetCurrentSceneCollection",
                            {"sceneCollectionName": str(params.get("name", ""))},
                            timeout=LONG_REQUEST_TIMEOUT_S)

    async def _cmd_set_profile(self, params: dict[str, Any]) -> None:
        await self._request("SetCurrentProfile", {"profileName": str(params.get("name", ""))},
                            timeout=LONG_REQUEST_TIMEOUT_S)

    async def _cmd_trigger_hotkey(self, params: dict[str, Any]) -> None:
        await self._request("TriggerHotkeyByName", {"hotkeyName": str(params.get("name", ""))})

    async def _cmd_trigger_key_sequence(self, params: dict[str, Any]) -> None:
        modifiers = {k: _bool(params.get(k)) for k in ("shift", "control", "alt", "command") if params.get(k) is not None}
        data: dict[str, Any] = {"keyId": str(params.get("key", ""))}
        if modifiers:
            data["keyModifiers"] = modifiers
        await self._request("TriggerHotkeyByKeySequence", data)

    async def _cmd_open_projector(self, params: dict[str, Any]) -> None:
        mix = VIDEO_MIX_TYPES.get(str(params.get("mix", "program")))
        if mix is None:
            raise ValueError(f"Unknown projector mix {params.get('mix')!r}")
        await self._request("OpenVideoMixProjector", {
            "videoMixType": mix, "monitorIndex": self._monitor_index(params)})

    async def _cmd_open_source_projector(self, params: dict[str, Any]) -> None:
        await self._request("OpenSourceProjector", {
            "sourceName": str(params.get("source", "")), "monitorIndex": self._monitor_index(params)})

    async def _cmd_save_screenshot(self, params: dict[str, Any]) -> None:
        path = str(params.get("file_path", ""))
        fmt = path.rsplit(".", 1)[-1].lower() if "." in path else "png"
        if fmt == "jpg":
            fmt = "jpeg"
        data: dict[str, Any] = {
            "sourceName": str(params.get("source", "")),
            "imageFormat": fmt,
            "imageFilePath": path,
        }
        if params.get("width"):
            data["imageWidth"] = _int(params.get("width"))
        if params.get("height"):
            data["imageHeight"] = _int(params.get("height"))
        await self._request("SaveSourceScreenshot", data)

    async def _cmd_broadcast_custom_event(self, params: dict[str, Any]) -> None:
        raw = str(params.get("data", "")).strip()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise ValueError(f"Event data is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("Event data must be a JSON object")
        await self._request("BroadcastCustomEvent", {"eventData": payload})

    _DISPATCH = {
        "set_program_scene": _cmd_set_program_scene,
        "set_preview_scene": _cmd_set_preview_scene,
        "transition": _cmd_transition,
        "set_studio_mode": _cmd_set_studio_mode,
        "studio_mode_on": _cmd_studio_mode_on,
        "studio_mode_off": _cmd_studio_mode_off,
        "set_transition": _cmd_set_transition,
        "set_transition_duration": _cmd_set_transition_duration,
        "set_tbar_position": _cmd_set_tbar_position,
        "start_stream": _cmd_start_stream,
        "stop_stream": _cmd_stop_stream,
        "toggle_stream": _cmd_toggle_stream,
        "send_caption": _cmd_send_caption,
        "start_record": _cmd_start_record,
        "stop_record": _cmd_stop_record,
        "toggle_record": _cmd_toggle_record,
        "pause_record": _cmd_pause_record,
        "resume_record": _cmd_resume_record,
        "toggle_record_pause": _cmd_toggle_record_pause,
        "split_record_file": _cmd_split_record_file,
        "create_record_chapter": _cmd_create_record_chapter,
        "start_virtual_camera": _cmd_start_virtual_camera,
        "stop_virtual_camera": _cmd_stop_virtual_camera,
        "toggle_virtual_camera": _cmd_toggle_virtual_camera,
        "start_replay_buffer": _cmd_start_replay_buffer,
        "stop_replay_buffer": _cmd_stop_replay_buffer,
        "toggle_replay_buffer": _cmd_toggle_replay_buffer,
        "save_replay_buffer": _cmd_save_replay_buffer,
        "start_output": _cmd_start_output,
        "stop_output": _cmd_stop_output,
        "toggle_output": _cmd_toggle_output,
        "mute_input": _cmd_mute_input,
        "unmute_input": _cmd_unmute_input,
        "toggle_input_mute": _cmd_toggle_input_mute,
        "set_input_volume": _cmd_set_input_volume,
        "adjust_input_volume": _cmd_adjust_input_volume,
        "set_input_balance": _cmd_set_input_balance,
        "set_input_sync_offset": _cmd_set_input_sync_offset,
        "set_input_monitoring": _cmd_set_input_monitoring,
        "media_play": _cmd_media_play,
        "media_pause": _cmd_media_pause,
        "media_stop": _cmd_media_stop,
        "media_restart": _cmd_media_restart,
        "media_next": _cmd_media_next,
        "media_previous": _cmd_media_previous,
        "set_media_position": _cmd_set_media_position,
        "offset_media_position": _cmd_offset_media_position,
        "set_text": _cmd_set_text,
        "refresh_browser_source": _cmd_refresh_browser_source,
        "show_scene_item": _cmd_show_scene_item,
        "hide_scene_item": _cmd_hide_scene_item,
        "toggle_scene_item": _cmd_toggle_scene_item,
        "set_source_visible": _cmd_set_source_visible,
        "enable_filter": _cmd_enable_filter,
        "disable_filter": _cmd_disable_filter,
        "toggle_filter": _cmd_toggle_filter,
        "set_scene_collection": _cmd_set_scene_collection,
        "set_profile": _cmd_set_profile,
        "trigger_hotkey": _cmd_trigger_hotkey,
        "trigger_key_sequence": _cmd_trigger_key_sequence,
        "open_projector": _cmd_open_projector,
        "open_source_projector": _cmd_open_source_projector,
        "save_screenshot": _cmd_save_screenshot,
        "broadcast_custom_event": _cmd_broadcast_custom_event,
    }

    # ── Device settings ───────────────────────────────────────────────────

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if not self._link_alive():
            raise ConnectionError(f"[{self.device_id}] Not connected to OBS")
        try:
            if key == "studio_mode":
                await self._request("SetStudioModeEnabled", {"studioModeEnabled": _bool(value)})
                return
            if key == "transition_duration_ms":
                await self._request("SetCurrentSceneTransitionDuration",
                                    {"transitionDuration": max(0, _int(value))})
                return
        except ObsRequestError as exc:
            raise ValueError(f"OBS refused {key}: {exc.text}") from exc
        raise ValueError(f"Unknown device setting: {key}")


def _unwrap(response: dict[str, Any]) -> dict[str, Any]:
    """responseData from a RequestResponse, or raise its failure."""
    status = response.get("requestStatus") or {}
    if not _bool(status.get("result")):
        raise ObsRequestError(
            str(response.get("requestType", "")),
            _int(status.get("code"), 0),
            str(status.get("comment") or ""),
        )
    data = response.get("responseData")
    return data if isinstance(data, dict) else {}


def _close_code(exc: BaseException) -> int:
    """The close code carried by a websockets ConnectionClosed, else 0."""
    rcvd = getattr(exc, "rcvd", None)
    code = getattr(rcvd, "code", None)
    if isinstance(code, int):
        return code
    return _int(getattr(exc, "code", 0), 0)


def _ws_is_open(ws: Any) -> bool:
    """True if a websockets connection is still open (websockets 16 exposes
    ``state``; older releases a ``closed`` flag)."""
    state = getattr(ws, "state", None)
    if state is not None:
        return getattr(state, "name", "") == "OPEN"
    return not getattr(ws, "closed", False)
