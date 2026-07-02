"""SoundCoreHero multi-zone audio system driver for OpenAVC.

SoundCoreHero is controlled over an HTTPS/JSON REST API with a companion
control WebSocket that pushes full-state snapshots and incremental updates.

Design notes
------------
* Transport is HTTP/REST, but the platform's declarative HTTP (.avcdriver)
  model cannot express this device: commands need JSON bodies carrying object
  UUIDs, auth lives in a header, the WS hand-shake needs a *session* (not the
  API key), and state is deeply nested. So this is a full Python driver.

* Auth: the control WebSocket validates a ``sessionId`` against the users
  manager, so an API key is NOT sufficient for real-time state. The driver
  logs in with email/password (``/usersmanager-login``), opens the WS with the
  returned ``(sessionId, receiverId)`` pair, and sends every REST command with
  the same session in the ``X-Session-Id`` header.

* Child entities: zones, players, speakers and inputs are registered as
  ``child_entity_types``. OpenAVC child ``id_format`` only supports integer
  local IDs, but the device addresses everything by UUID. We therefore use each
  object's stable 0-based ``order`` field as the integer local ID and stash the
  real UUID in a per-child ``uuid`` state variable. Commands resolve
  order -> UUID before hitting the wire. On every snapshot the order<->uuid map
  is rebuilt, so re-ordering on the device stays consistent after a reconnect.

* Updates: the WS pushes a snapshot on connect and ``*Update`` deltas after
  that. ``poll()`` is only a watchdog (a cheap ``/system-get-status-all``); the
  real data flows through the WS task.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import uuid as uuidlib
from typing import Any, Optional

import httpx
import websockets

from server.drivers.base import BaseDriver
from server.utils.logger import get_logger

log = get_logger(__name__)

# Maps each child entity type to the REST resource collection key returned by
# the matching *-get-status-all endpoint and pushed over the WebSocket.
# (child_type, snapshot_key, update_key)
_COLLECTIONS = [
    ("zone", "zones", "zonesUpdate"),
    ("player", "players", "playersUpdate"),
    ("speaker", "speakers", "speakersUpdate"),
    ("input", "inputs", "inputsUpdate"),
    ("playlist", "playlists", "playlistsUpdate"),
    ("announcement", "announcements", "announcementsUpdate"),
    ("action", "actions", "actionsUpdate"),
    ("action_group", "actionGroups", "actionGroupsUpdate"),
    ("controller", "controllers", "controllersUpdate"),
]
_SNAPSHOT_KEYS = {snap: ctype for ctype, snap, _ in _COLLECTIONS}
_UPDATE_KEYS = {upd: ctype for ctype, _, upd in _COLLECTIONS}

# REST *-get-status-all endpoint singular per child type, used by
# refresh_children. Controllers have no documented get-all endpoint — they
# only arrive over the WebSocket — so they are intentionally absent here.
_REFRESH_ENDPOINT = {
    "zone": "zone", "player": "player", "speaker": "speaker", "input": "input",
    "playlist": "playlist", "announcement": "announcement",
    "action": "action", "action_group": "action-group",
}


def _csv(value: Any) -> Optional[str]:
    """Join a list of UUIDs/ints into a comma-separated string for display."""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return None


def _fmt_channels(cf: Any) -> Optional[str]:
    """Render a channelsFormat object as a short human string."""
    if not isinstance(cf, dict) or not cf:
        return "none"
    if "mono" in cf:
        return f"mono:{cf['mono'].get('channel')}"
    if "stereo" in cf:
        s = cf["stereo"]
        return f"stereo:{s.get('left')}/{s.get('right')}"
    return "none"


def _unescape_json_pointer(seg: str) -> str:
    # RFC 6901: ~1 -> /, ~0 -> ~ (order matters).
    return seg.replace("~1", "/").replace("~0", "~")


def _descend(container: Any, seg: str):
    """Return (child, ok) for one path segment into a dict or list."""
    if isinstance(container, dict):
        if seg in container:
            return container[seg], True
        return None, False
    if isinstance(container, list):
        try:
            idx = int(seg)
        except ValueError:
            return None, False
        if -len(container) <= idx < len(container):
            return container[idx], True
        return None, False
    return None, False


def _assign(container: Any, seg: str, value: Any) -> None:
    if isinstance(container, dict):
        container[seg] = value
    elif isinstance(container, list):
        try:
            idx = int(seg)
        except ValueError:
            return
        if seg == "-" or idx == len(container):
            container.append(value)
        elif -len(container) <= idx < len(container):
            container[idx] = value


def _remove(container: Any, seg: str) -> None:
    if isinstance(container, dict):
        container.pop(seg, None)
    elif isinstance(container, list):
        try:
            idx = int(seg)
        except ValueError:
            return
        if -len(container) <= idx < len(container):
            del container[idx]


class SoundCoreHeroDriver(BaseDriver):
    """Controls a SoundCoreHero multi-zone audio system."""

    DRIVER_INFO = {
        "id": "soundcorehero",
        "name": "SoundCoreHero Audio System",
        "manufacturer": "Hero AV",
        "category": "audio",
        "version": "1.0.1",
        "author": "Wiktor Myszolow (Hero AV)",
        "description": "Controls a SoundCoreHero multi-zone audio distribution system (zones, players, speakers, inputs) over its HTTPS/WebSocket API.",
        "source_url": "https://soundcorehero.com",
        # Child entities (register_child / child_entity_types) require the
        # platform's child-entity runtime, which shipped in 0.13.0. The tls:
        # discovery probe needs 0.15.0 but degrades gracefully on older
        # instances (they skip the hint), so it does NOT raise this floor.
        "min_platform_version": "0.13.0",
        "transport": "tcp",  # nominal; real I/O is custom httpx + websockets
        "help": {
            "overview": (
                "SoundCoreHero is a professional multi-zone audio distribution "
                "and management system. This driver exposes its zones, players, "
                "speakers and inputs as child entities, controls volume/mute/"
                "transport, and plays announcements."
            ),
            "setup": (
                "1. Note the device IP. By default only HTTPS (port 443) is "
                "enabled.\n"
                "2. Create or pick a user account in Settings -> Users. The "
                "driver logs in with that account's email and password to "
                "obtain a session (required by the control WebSocket).\n"
                "3. Enter IP, email and password. Leave 'Verify SSL' off if the "
                "device uses its default self-signed certificate."
            ),
        },
        "default_config": {
            "host": "",
            "port": 443,
            "email": "",
            "password": "",
            "verify_ssl": False,
            "poll_interval": 20,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "default": 443, "label": "HTTPS Port"},
            "email": {"type": "string", "required": True, "label": "Login Email"},
            "password": {"type": "string", "required": True, "label": "Password", "secret": True},
            "verify_ssl": {"type": "boolean", "default": False, "label": "Verify SSL Certificate"},
            "poll_interval": {"type": "integer", "default": 20, "label": "Watchdog Interval (sec)"},
        },
        # Device-level state.
        "state_variables": {
            "internet": {"type": "boolean", "label": "Internet Connectivity"},
            "current_project": {"type": "string", "label": "Current Project"},
            # --- DSO (emergency input singleton) ---
            "dso_enabled": {"type": "boolean", "label": "DSO Enabled"},
            "dso_volume": {"type": "number", "label": "DSO Volume"},
            "dso_muted": {"type": "boolean", "label": "DSO Muted"},
            "dso_channels_format": {"type": "string", "label": "DSO Channels"},
            "dso_signal_active": {"type": "boolean", "label": "DSO Signal Active",
                                  "cloud_priority": "low"},
            "dso_eq_preset": {"type": "string", "label": "DSO EQ Preset"},
            "dso_eq_bypassed": {"type": "boolean", "label": "DSO EQ Bypassed"},
            "dso_gate_preset": {"type": "string", "label": "DSO Gate Preset"},
            "dso_gate_bypassed": {"type": "boolean", "label": "DSO Gate Bypassed"},
            "dso_compressor_preset": {"type": "string", "label": "DSO Compressor Preset"},
            "dso_compressor_bypassed": {"type": "boolean", "label": "DSO Compressor Bypassed"},
            # --- System signals (pushed over the control WebSocket) ---
            "cpu_usage": {"type": "number", "label": "CPU Usage (%)", "cloud_priority": "low"},
            "server_time": {"type": "string", "label": "Server Time (UTC)", "cloud_priority": "low"},
            "channels_used_inputs": {"type": "integer", "label": "Input Channels Used"},
            "channels_used_outputs": {"type": "integer", "label": "Output Channels Used"},
            "channels_max": {"type": "integer", "label": "Max Channels"},
            "update_status": {"type": "string", "label": "Software Update Status"},
            "update_progress": {"type": "number", "label": "Update Download Progress",
                                "cloud_priority": "low"},
            "last_system_error": {"type": "string", "label": "Last System Error"},
            "last_action_error": {"type": "string", "label": "Last Action Error"},
        },
        "child_entity_types": {
            "zone": {
                "label": "Zone",
                "label_plural": "Zones",
                "id_format": {"type": "integer", "min": 0, "max": 999, "pad_width": 3},
                "state_variables": {
                    "uuid": {"type": "string"},
                    "name": {"type": "string"},
                    "master_volume": {"type": "number", "cloud_priority": "high"},
                    "master_muted": {"type": "boolean", "cloud_priority": "high"},
                    "master_balance": {"type": "number"},
                    "player_volume": {"type": "number"},
                    "player_muted": {"type": "boolean"},
                    "player_balance": {"type": "number"},
                    "dso_volume": {"type": "number"},
                    "dso_muted": {"type": "boolean"},
                    "announcement_volume": {"type": "number"},
                    "announcement_muted": {"type": "boolean"},
                    "announcement_state": {"type": "string", "cloud_priority": "high"},
                    "announcement_duration": {"type": "number", "cloud_priority": "low"},
                    "announcement_progress": {"type": "number", "cloud_priority": "low"},
                    "line_ducker_bypassed": {"type": "boolean"},
                    "mic_ducker_bypassed": {"type": "boolean"},
                    "dso_ducker_bypassed": {"type": "boolean"},
                    "line_ducker_preset": {"type": "string"},
                    "mic_ducker_preset": {"type": "string"},
                    "dso_ducker_preset": {"type": "string"},
                    "connected_player": {"type": "string"},
                    "line_inputs": {"type": "string"},
                    "mic_inputs": {"type": "string"},
                    "speakers": {"type": "string"},
                    "order": {"type": "number"},
                    "signal_active": {"type": "boolean", "cloud_priority": "low"},
                },
                "summary_fields": ["name", "master_volume", "master_muted", "announcement_state"],
                "label_field": "name",
            },
            "player": {
                "label": "Player",
                "label_plural": "Players",
                "id_format": {"type": "integer", "min": 0, "max": 999, "pad_width": 3},
                "state_variables": {
                    "uuid": {"type": "string"},
                    "name": {"type": "string"},
                    "state": {"type": "string", "cloud_priority": "high"},
                    "volume": {"type": "number"},
                    "muted": {"type": "boolean"},
                    "source_type": {"type": "string"},
                    "stream_url": {"type": "string"},
                    "track_name": {"type": "string", "cloud_priority": "low"},
                    "track_path": {"type": "string", "cloud_priority": "low"},
                    "track_position": {"type": "number", "cloud_priority": "low"},
                    "track_path_valid": {"type": "boolean", "cloud_priority": "low"},
                    "progress": {"type": "number", "cloud_priority": "low"},
                    "duration": {"type": "number", "cloud_priority": "low"},
                    "remaining": {"type": "number", "cloud_priority": "low"},
                    "shuffle": {"type": "boolean"},
                    "repeat_one": {"type": "boolean"},
                    "repeat_all": {"type": "boolean"},
                    "fade": {"type": "number"},
                    "playlist_uuid": {"type": "string"},
                    "zones": {"type": "string"},
                    "order": {"type": "number"},
                    "signal_active": {"type": "boolean", "cloud_priority": "low"},
                },
                "summary_fields": ["name", "state", "volume", "track_name"],
                "label_field": "name",
            },
            "speaker": {
                "label": "Speaker",
                "label_plural": "Speakers",
                "id_format": {"type": "integer", "min": 0, "max": 999, "pad_width": 3},
                "state_variables": {
                    "uuid": {"type": "string"},
                    "name": {"type": "string"},
                    "type": {"type": "string"},
                    "description": {"type": "string"},
                    "volume": {"type": "number", "cloud_priority": "high"},
                    "muted": {"type": "boolean", "cloud_priority": "high"},
                    "delay_time": {"type": "number"},
                    "delay_bypassed": {"type": "boolean"},
                    "limiter_threshold": {"type": "number"},
                    "limiter_release": {"type": "number"},
                    "limiter_mode": {"type": "string"},
                    "eq_preset": {"type": "string"},
                    "eq_bypassed": {"type": "boolean"},
                    "limiter_preset": {"type": "string"},
                    "limiter_preset_bypassed": {"type": "boolean"},
                    "playing_pink_noise": {"type": "boolean"},
                    "zone": {"type": "string"},
                    "devices": {"type": "string"},
                    "order": {"type": "number"},
                    "signal_active": {"type": "boolean", "cloud_priority": "low"},
                },
                "summary_fields": ["name", "type", "volume", "muted"],
                "label_field": "name",
            },
            "input": {
                "label": "Input",
                "label_plural": "Inputs",
                "id_format": {"type": "integer", "min": 0, "max": 999, "pad_width": 3},
                "state_variables": {
                    "uuid": {"type": "string"},
                    "name": {"type": "string"},
                    "mode": {"type": "string"},
                    "volume": {"type": "number"},
                    "muted": {"type": "boolean"},
                    "channels_format": {"type": "string"},
                    "eq_preset": {"type": "string"},
                    "eq_bypassed": {"type": "boolean"},
                    "gate_preset": {"type": "string"},
                    "gate_bypassed": {"type": "boolean"},
                    "compressor_preset": {"type": "string"},
                    "compressor_bypassed": {"type": "boolean"},
                    "zones": {"type": "string"},
                    "order": {"type": "number"},
                    "signal_active": {"type": "boolean", "cloud_priority": "low"},
                },
                "summary_fields": ["name", "mode", "volume", "muted"],
                "label_field": "name",
            },
            "playlist": {
                "label": "Playlist",
                "label_plural": "Playlists",
                "id_format": {"type": "integer", "min": 0, "max": 999, "pad_width": 3},
                "state_variables": {
                    "uuid": {"type": "string"},
                    "name": {"type": "string"},
                    "root_path": {"type": "string"},
                    "item_count": {"type": "integer"},
                    "order": {"type": "number"},
                },
                "summary_fields": ["name", "root_path", "item_count"],
                "label_field": "name",
            },
            "announcement": {
                "label": "Announcement",
                "label_plural": "Announcements",
                "id_format": {"type": "integer", "min": 0, "max": 999, "pad_width": 3},
                "state_variables": {
                    "uuid": {"type": "string"},
                    "name": {"type": "string"},
                    "path": {"type": "string"},
                    "path_valid": {"type": "boolean"},
                    "volume": {"type": "number"},
                    "priority": {"type": "integer"},
                    "attack": {"type": "integer"},
                    "release": {"type": "integer"},
                    "ducking_volume": {"type": "number"},
                    "duration": {"type": "number"},
                    "order": {"type": "number"},
                },
                "summary_fields": ["name", "priority", "duration", "path_valid"],
                "label_field": "name",
            },
            "action": {
                "label": "Action",
                "label_plural": "Actions",
                "id_format": {"type": "integer", "min": 0, "max": 999, "pad_width": 3},
                "state_variables": {
                    "uuid": {"type": "string"},
                    "name": {"type": "string"},
                    "running": {"type": "boolean"},
                    "trigger_code": {"type": "string"},
                    "step_count": {"type": "integer"},
                    "group_ids": {"type": "string"},
                    "description": {"type": "string"},
                    "order": {"type": "number"},
                },
                "summary_fields": ["name", "running", "trigger_code", "step_count"],
                "label_field": "name",
            },
            "action_group": {
                "label": "Action Group",
                "label_plural": "Action Groups",
                "id_format": {"type": "integer", "min": 0, "max": 999, "pad_width": 3},
                "state_variables": {
                    "uuid": {"type": "string"},
                    "name": {"type": "string"},
                    "action_ids": {"type": "string"},
                    "action_count": {"type": "integer"},
                    "order": {"type": "number"},
                },
                "summary_fields": ["name", "action_count"],
                "label_field": "name",
            },
            "controller": {
                "label": "Controller",
                "label_plural": "Controllers",
                "id_format": {"type": "integer", "min": 0, "max": 999, "pad_width": 3},
                "state_variables": {
                    # Physical wall controller (e.g. Bose CC-3D): bound to one
                    # zone, selects one of N preset players and rides volume.
                    # Schema confirmed against a live device object.
                    "uuid": {"type": "string"},
                    "name": {"type": "string"},
                    "model": {"type": "string"},
                    "ip": {"type": "string"},
                    "mac": {"type": "string"},
                    "online": {"type": "boolean"},
                    "zone": {"type": "string"},
                    "zone_volume": {"type": "number"},
                    "selected_index": {"type": "integer"},
                    "selected_player": {"type": "string"},
                    "players": {"type": "string"},
                    "order": {"type": "number"},
                    "raw_json": {"type": "string", "cloud_priority": "low"},
                },
                "summary_fields": ["name", "model", "selected_index", "zone_volume"],
                "label_field": "name",
            },
        },
        "commands": {
            # ============================================================ #
            # PLAYERS
            # ============================================================ #
            "player_play": {
                "label": "Player: Play",
                "params": {"player_id": {"type": "child_id", "child_type": "player", "required": True}},
            },
            "player_pause": {
                "label": "Player: Pause",
                "params": {"player_id": {"type": "child_id", "child_type": "player", "required": True}},
            },
            "player_stop": {
                "label": "Player: Stop",
                "params": {"player_id": {"type": "child_id", "child_type": "player", "required": True}},
            },
            "player_next": {
                "label": "Player: Next Track",
                "params": {"player_id": {"type": "child_id", "child_type": "player", "required": True}},
            },
            "player_previous": {
                "label": "Player: Previous",
                "params": {"player_id": {"type": "child_id", "child_type": "player", "required": True}},
            },
            "player_previous_track": {
                "label": "Player: Previous Track (no restart)",
                "params": {"player_id": {"type": "child_id", "child_type": "player", "required": True}},
            },
            "player_set_volume": {
                "label": "Player: Set Volume",
                "params": {
                    "player_id": {"type": "child_id", "child_type": "player", "required": True},
                    "volume": {"type": "number", "required": True, "min": -80, "max": 0,
                               "help": "Output level in dBFS (0.0 = unity)."},
                },
            },
            "player_set_muted": {
                "label": "Player: Set Mute",
                "params": {
                    "player_id": {"type": "child_id", "child_type": "player", "required": True},
                    "muted": {"type": "boolean", "required": True},
                },
            },
            "player_set_shuffle": {
                "label": "Player: Set Shuffle",
                "params": {
                    "player_id": {"type": "child_id", "child_type": "player", "required": True},
                    "shuffle": {"type": "boolean", "required": True},
                },
            },
            "player_set_repeat_one": {
                "label": "Player: Set Repeat-One",
                "params": {
                    "player_id": {"type": "child_id", "child_type": "player", "required": True},
                    "repeat_one": {"type": "boolean", "required": True},
                },
            },
            "player_set_repeat_all": {
                "label": "Player: Set Repeat-All",
                "params": {
                    "player_id": {"type": "child_id", "child_type": "player", "required": True},
                    "repeat_all": {"type": "boolean", "required": True},
                },
            },
            "player_set_fade": {
                "label": "Player: Set Fade Time",
                "params": {
                    "player_id": {"type": "child_id", "child_type": "player", "required": True},
                    "fade": {"type": "number", "required": True, "min": 0, "max": 60,
                             "help": "Crossfade/fade time in seconds."},
                },
            },
            "player_set_track": {
                "label": "Player: Set Track Position",
                "params": {
                    "player_id": {"type": "child_id", "child_type": "player", "required": True},
                    "position": {"type": "integer", "required": True, "min": 0,
                                 "help": "Zero-based track index in the playlist."},
                },
            },
            "player_set_playback_time": {
                "label": "Player: Seek (set playback time)",
                "params": {
                    "player_id": {"type": "child_id", "child_type": "player", "required": True},
                    "time": {"type": "number", "required": True, "min": 0,
                             "help": "Playback position in seconds."},
                },
            },
            "player_set_source": {
                "label": "Player: Set Source (type/stream)",
                "params": {
                    "player_id": {"type": "child_id", "child_type": "player", "required": True},
                    "source_type": {"type": "enum", "values": ["playlist", "stream"], "required": True},
                    "stream_url": {"type": "string", "required": False,
                                   "help": "Required when source_type is 'stream'."},
                },
            },
            "player_set_zones": {
                "label": "Player: Set Zones (replace)",
                "params": {
                    "player_id": {"type": "child_id", "child_type": "player", "required": True},
                    "zone_ids": {"type": "string", "required": True,
                                 "help": "Comma-separated zone child IDs (order numbers), or 'all'."},
                },
            },
            "player_add_zones": {
                "label": "Player: Add Zones",
                "params": {
                    "player_id": {"type": "child_id", "child_type": "player", "required": True},
                    "zone_ids": {"type": "string", "required": True,
                                 "help": "Comma-separated zone child IDs (order numbers), or 'all'."},
                },
            },
            "player_remove_zones": {
                "label": "Player: Remove Zones",
                "params": {
                    "player_id": {"type": "child_id", "child_type": "player", "required": True},
                    "zone_ids": {"type": "string", "required": True,
                                 "help": "Comma-separated zone child IDs (order numbers), or 'all'."},
                },
            },
            "player_set_playlist": {
                "label": "Player: Set Playlist",
                "params": {
                    "player_id": {"type": "child_id", "child_type": "player", "required": True},
                    "playlist_uuid": {"type": "string", "required": True,
                                      "help": "UUID of the playlist to assign."},
                },
            },
            "player_remove_playlist": {
                "label": "Player: Remove Playlist",
                "params": {
                    "player_id": {"type": "child_id", "child_type": "player", "required": True},
                    "playlist_uuid": {"type": "string", "required": True,
                                      "help": "UUID of the playlist to unassign."},
                },
            },
            "player_rename": {
                "label": "Player: Rename",
                "params": {
                    "player_id": {"type": "child_id", "child_type": "player", "required": True},
                    "name": {"type": "string", "required": True},
                },
            },
            "player_set_order": {
                "label": "Player: Set Order",
                "params": {
                    "player_id": {"type": "child_id", "child_type": "player", "required": True},
                    "order": {"type": "integer", "required": True, "min": 0},
                },
            },
            "player_create": {
                "label": "Player: Create",
                "params": {
                    "name": {"type": "string", "required": True},
                    "source_type": {"type": "enum", "values": ["playlist", "stream"], "required": True},
                    "stream_url": {"type": "string", "required": False,
                                   "help": "Required when source_type is 'stream'."},
                    "volume": {"type": "number", "required": False, "min": -80, "max": 0},
                    "muted": {"type": "boolean", "required": False},
                    "fade_time": {"type": "number", "required": False, "min": 0, "max": 60},
                    "repeat_one": {"type": "boolean", "required": False},
                    "repeat_all": {"type": "boolean", "required": False},
                    "shuffle": {"type": "boolean", "required": False},
                },
            },
            "player_remove": {
                "label": "Player: Remove",
                "params": {"player_id": {"type": "child_id", "child_type": "player", "required": True}},
            },

            # ============================================================ #
            # ZONES
            # ============================================================ #
            "zone_set_mixer": {
                "label": "Zone: Set Mixer Bus",
                "params": {
                    "zone_id": {"type": "child_id", "child_type": "zone", "required": True},
                    "bus": {"type": "enum", "required": True,
                            "values": ["master", "player", "dso", "announcement"]},
                    "volume": {"type": "number", "required": False, "min": -80, "max": 12},
                    "balance": {"type": "integer", "required": False, "min": -100, "max": 100},
                    "muted": {"type": "boolean", "required": False},
                },
                "help": "Set volume/balance/mute on a fixed mixer bus. Omit fields to leave them unchanged.",
            },
            "zone_set_input_mixer": {
                "label": "Zone: Set Per-Input Mixer",
                "params": {
                    "zone_id": {"type": "child_id", "child_type": "zone", "required": True},
                    "input_id": {"type": "child_id", "child_type": "input", "required": True},
                    "bus": {"type": "enum", "required": True, "values": ["lineInputs", "micInputs"]},
                    "volume": {"type": "number", "required": False, "min": -80, "max": 12},
                    "balance": {"type": "integer", "required": False, "min": -100, "max": 100},
                    "muted": {"type": "boolean", "required": False},
                },
                "help": "Set the per-input mixer strip for an input routed into this zone.",
            },
            "zone_set_master_volume": {
                "label": "Zone: Set Master Volume",
                "params": {
                    "zone_id": {"type": "child_id", "child_type": "zone", "required": True},
                    "volume": {"type": "number", "required": True, "min": -80, "max": 12},
                },
            },
            "zone_set_master_muted": {
                "label": "Zone: Set Master Mute",
                "params": {
                    "zone_id": {"type": "child_id", "child_type": "zone", "required": True},
                    "muted": {"type": "boolean", "required": True},
                },
            },
            "zone_set_ducker": {
                "label": "Zone: Set Ducker (custom params)",
                "params": {
                    "zone_id": {"type": "child_id", "child_type": "zone", "required": True},
                    "ducker": {"type": "enum", "required": True, "values": ["lineInputs", "micInputs", "dso"]},
                    "threshold": {"type": "number", "required": True, "help": "Sidechain level dBFS."},
                    "range": {"type": "number", "required": True, "help": "Gain reduction dB (<= 0)."},
                    "attack": {"type": "integer", "required": True, "help": "ms"},
                    "hold": {"type": "integer", "required": True, "help": "ms"},
                    "release": {"type": "integer", "required": True, "help": "ms"},
                },
                "help": "Replace a ducker stage with custom parameters (clears its preset).",
            },
            "zone_set_ducker_preset": {
                "label": "Zone: Assign Ducker Preset",
                "params": {
                    "zone_id": {"type": "child_id", "child_type": "zone", "required": True},
                    "ducker": {"type": "enum", "required": True, "values": ["line", "mic", "dso"]},
                    "preset_id": {"type": "string", "required": True, "help": "Ducker preset UUID."},
                    "bypassed": {"type": "boolean", "required": False},
                },
            },
            "zone_set_ducker_bypass": {
                "label": "Zone: Bypass Ducker",
                "params": {
                    "zone_id": {"type": "child_id", "child_type": "zone", "required": True},
                    "ducker": {"type": "enum", "required": True, "values": ["line", "mic", "dso"]},
                    "bypassed": {"type": "boolean", "required": True},
                },
            },
            "zone_set_players": {
                "label": "Zone: Set Player (replace)",
                "params": {
                    "zone_id": {"type": "child_id", "child_type": "zone", "required": True},
                    "player_id": {"type": "child_id", "child_type": "player", "required": True},
                },
            },
            "zone_add_players": {
                "label": "Zone: Add Player",
                "params": {
                    "zone_id": {"type": "child_id", "child_type": "zone", "required": True},
                    "player_id": {"type": "child_id", "child_type": "player", "required": True},
                },
            },
            "zone_remove_players": {
                "label": "Zone: Remove Player",
                "params": {
                    "zone_id": {"type": "child_id", "child_type": "zone", "required": True},
                    "player_id": {"type": "child_id", "child_type": "player", "required": True},
                },
            },
            "zone_set_inputs": {
                "label": "Zone: Set Inputs (replace)",
                "params": {
                    "zone_id": {"type": "child_id", "child_type": "zone", "required": True},
                    "input_ids": {"type": "string", "required": True,
                                  "help": "Comma-separated input child IDs, or 'all'."},
                },
            },
            "zone_add_inputs": {
                "label": "Zone: Add Inputs",
                "params": {
                    "zone_id": {"type": "child_id", "child_type": "zone", "required": True},
                    "input_ids": {"type": "string", "required": True,
                                  "help": "Comma-separated input child IDs, or 'all'."},
                },
            },
            "zone_remove_inputs": {
                "label": "Zone: Remove Inputs",
                "params": {
                    "zone_id": {"type": "child_id", "child_type": "zone", "required": True},
                    "input_ids": {"type": "string", "required": True,
                                  "help": "Comma-separated input child IDs, or 'all'."},
                },
            },
            "zone_set_speakers": {
                "label": "Zone: Set Speakers (replace)",
                "params": {
                    "zone_id": {"type": "child_id", "child_type": "zone", "required": True},
                    "speaker_ids": {"type": "string", "required": True,
                                    "help": "Comma-separated speaker child IDs, or 'all'."},
                },
            },
            "zone_add_speakers": {
                "label": "Zone: Add Speakers",
                "params": {
                    "zone_id": {"type": "child_id", "child_type": "zone", "required": True},
                    "speaker_ids": {"type": "string", "required": True,
                                    "help": "Comma-separated speaker child IDs, or 'all'."},
                },
            },
            "zone_remove_speakers": {
                "label": "Zone: Remove Speakers",
                "params": {
                    "zone_id": {"type": "child_id", "child_type": "zone", "required": True},
                    "speaker_ids": {"type": "string", "required": True,
                                    "help": "Comma-separated speaker child IDs, or 'all'."},
                },
            },
            "zone_rename": {
                "label": "Zone: Rename",
                "params": {
                    "zone_id": {"type": "child_id", "child_type": "zone", "required": True},
                    "name": {"type": "string", "required": True},
                },
            },
            "zone_set_order": {
                "label": "Zone: Set Order",
                "params": {
                    "zone_id": {"type": "child_id", "child_type": "zone", "required": True},
                    "order": {"type": "integer", "required": True, "min": 0},
                },
            },
            "zone_create": {
                "label": "Zone: Create",
                "params": {"name": {"type": "string", "required": True}},
            },
            "zone_remove": {
                "label": "Zone: Remove",
                "params": {"zone_id": {"type": "child_id", "child_type": "zone", "required": True}},
            },
            "zone_announcement_abort": {
                "label": "Zone: Abort Announcement",
                "params": {"zone_id": {"type": "child_id", "child_type": "zone", "required": True}},
            },
            "play_announcement": {
                "label": "Zone: Play Announcement",
                "params": {
                    "zone_id": {"type": "child_id", "child_type": "zone", "required": True},
                    "announcement_uuid": {"type": "string", "required": True,
                                          "help": "UUID of the announcement clip."},
                },
            },

            # ============================================================ #
            # SPEAKERS
            # ============================================================ #
            "speaker_set_volume": {
                "label": "Speaker: Set Volume",
                "params": {
                    "speaker_id": {"type": "child_id", "child_type": "speaker", "required": True},
                    "volume": {"type": "number", "required": True, "min": -80, "max": 12},
                },
            },
            "speaker_set_muted": {
                "label": "Speaker: Set Mute",
                "params": {
                    "speaker_id": {"type": "child_id", "child_type": "speaker", "required": True},
                    "muted": {"type": "boolean", "required": True},
                },
            },
            "speaker_set_type": {
                "label": "Speaker: Set Type",
                "params": {
                    "speaker_id": {"type": "child_id", "child_type": "speaker", "required": True},
                    "type": {"type": "enum", "required": True, "values": ["left", "right", "center"]},
                },
            },
            "speaker_set_description": {
                "label": "Speaker: Set Description",
                "params": {
                    "speaker_id": {"type": "child_id", "child_type": "speaker", "required": True},
                    "description": {"type": "string", "required": True},
                },
            },
            "speaker_set_delay": {
                "label": "Speaker: Set Delay",
                "params": {
                    "speaker_id": {"type": "child_id", "child_type": "speaker", "required": True},
                    "time": {"type": "integer", "required": True, "min": 0, "help": "Delay in ms."},
                    "bypassed": {"type": "boolean", "required": False},
                },
            },
            "speaker_set_limiter": {
                "label": "Speaker: Set Limiter (custom)",
                "params": {
                    "speaker_id": {"type": "child_id", "child_type": "speaker", "required": True},
                    "threshold": {"type": "number", "required": True, "help": "dBFS."},
                    "release": {"type": "number", "required": True, "help": "ms."},
                    "mode": {"type": "enum", "required": True, "values": ["rms", "peak"]},
                },
                "help": "Set a custom limiter (clears the limiter preset).",
            },
            "speaker_set_limiter_preset": {
                "label": "Speaker: Assign Limiter Preset",
                "params": {
                    "speaker_id": {"type": "child_id", "child_type": "speaker", "required": True},
                    "preset_id": {"type": "string", "required": True},
                    "bypassed": {"type": "boolean", "required": False},
                },
            },
            "speaker_set_eq_preset": {
                "label": "Speaker: Assign EQ Preset",
                "params": {
                    "speaker_id": {"type": "child_id", "child_type": "speaker", "required": True},
                    "preset_id": {"type": "string", "required": True},
                    "bypassed": {"type": "boolean", "required": False},
                },
            },
            "speaker_set_eq_bypass": {
                "label": "Speaker: Bypass EQ",
                "params": {
                    "speaker_id": {"type": "child_id", "child_type": "speaker", "required": True},
                    "bypassed": {"type": "boolean", "required": True},
                },
            },
            "speaker_set_zone": {
                "label": "Speaker: Set Zone",
                "params": {
                    "speaker_id": {"type": "child_id", "child_type": "speaker", "required": True},
                    "zone_id": {"type": "child_id", "child_type": "zone", "required": True},
                },
            },
            "speaker_remove_zone": {
                "label": "Speaker: Remove Zone",
                "params": {"speaker_id": {"type": "child_id", "child_type": "speaker", "required": True}},
            },
            "speaker_set_channel": {
                "label": "Speaker: Set Output Channel",
                "params": {
                    "speaker_id": {"type": "child_id", "child_type": "speaker", "required": True},
                    "device_channel": {"type": "integer", "required": True, "min": 1,
                                       "help": "Hardware output channel (1..total)."},
                },
            },
            "speaker_remove_channel": {
                "label": "Speaker: Remove Output Channel",
                "params": {"speaker_id": {"type": "child_id", "child_type": "speaker", "required": True}},
            },
            "speaker_play_pink_noise": {
                "label": "Speaker: Pink Noise (calibration)",
                "params": {
                    "speaker_id": {"type": "child_id", "child_type": "speaker", "required": True},
                    "play": {"type": "boolean", "required": True},
                },
            },
            "speaker_rename": {
                "label": "Speaker: Rename",
                "params": {
                    "speaker_id": {"type": "child_id", "child_type": "speaker", "required": True},
                    "name": {"type": "string", "required": True},
                },
            },
            "speaker_set_order": {
                "label": "Speaker: Set Order",
                "params": {
                    "speaker_id": {"type": "child_id", "child_type": "speaker", "required": True},
                    "order": {"type": "integer", "required": True, "min": 0},
                },
            },
            "speaker_create": {
                "label": "Speaker: Create",
                "params": {
                    "name": {"type": "string", "required": True},
                    "type": {"type": "enum", "required": True, "values": ["left", "right", "center"]},
                    "description": {"type": "string", "required": False},
                    "volume": {"type": "number", "required": False, "min": -80, "max": 12},
                    "muted": {"type": "boolean", "required": False},
                },
            },
            "speaker_remove": {
                "label": "Speaker: Remove",
                "params": {"speaker_id": {"type": "child_id", "child_type": "speaker", "required": True}},
            },

            # ============================================================ #
            # INPUTS
            # ============================================================ #
            "input_set_volume": {
                "label": "Input: Set Volume",
                "params": {
                    "input_id": {"type": "child_id", "child_type": "input", "required": True},
                    "volume": {"type": "number", "required": True, "min": -80, "max": 12},
                },
            },
            "input_set_muted": {
                "label": "Input: Set Mute",
                "params": {
                    "input_id": {"type": "child_id", "child_type": "input", "required": True},
                    "muted": {"type": "boolean", "required": True},
                },
            },
            "input_set_mode": {
                "label": "Input: Set Mode",
                "params": {
                    "input_id": {"type": "child_id", "child_type": "input", "required": True},
                    "mode": {"type": "enum", "required": True, "values": ["mic", "line"]},
                },
            },
            "input_set_channels": {
                "label": "Input: Set Channels",
                "params": {
                    "input_id": {"type": "child_id", "child_type": "input", "required": True},
                    "format": {"type": "enum", "required": True, "values": ["mono", "stereo"]},
                    "channel": {"type": "integer", "required": False, "min": 1,
                                "help": "For mono: the device channel."},
                    "left": {"type": "integer", "required": False, "min": 1,
                             "help": "For stereo: left device channel."},
                    "right": {"type": "integer", "required": False, "min": 1,
                              "help": "For stereo: right device channel."},
                },
            },
            "input_remove_channels": {
                "label": "Input: Remove Channels",
                "params": {"input_id": {"type": "child_id", "child_type": "input", "required": True}},
            },
            "input_set_eq_preset": {
                "label": "Input: Assign EQ Preset",
                "params": {
                    "input_id": {"type": "child_id", "child_type": "input", "required": True},
                    "preset_id": {"type": "string", "required": True},
                    "bypassed": {"type": "boolean", "required": False},
                },
            },
            "input_set_gate_preset": {
                "label": "Input: Assign Gate Preset",
                "params": {
                    "input_id": {"type": "child_id", "child_type": "input", "required": True},
                    "preset_id": {"type": "string", "required": True},
                    "bypassed": {"type": "boolean", "required": False},
                },
            },
            "input_set_compressor_preset": {
                "label": "Input: Assign Compressor Preset",
                "params": {
                    "input_id": {"type": "child_id", "child_type": "input", "required": True},
                    "preset_id": {"type": "string", "required": True},
                    "bypassed": {"type": "boolean", "required": False},
                },
            },
            "input_set_processor_bypass": {
                "label": "Input: Bypass Processor",
                "params": {
                    "input_id": {"type": "child_id", "child_type": "input", "required": True},
                    "processor": {"type": "enum", "required": True, "values": ["eq", "gate", "compressor"]},
                    "bypassed": {"type": "boolean", "required": True},
                },
            },
            "input_set_zones": {
                "label": "Input: Set Zones (replace)",
                "params": {
                    "input_id": {"type": "child_id", "child_type": "input", "required": True},
                    "zone_ids": {"type": "string", "required": True,
                                 "help": "Comma-separated zone child IDs, or 'all'."},
                },
            },
            "input_add_zones": {
                "label": "Input: Add Zones",
                "params": {
                    "input_id": {"type": "child_id", "child_type": "input", "required": True},
                    "zone_ids": {"type": "string", "required": True,
                                 "help": "Comma-separated zone child IDs, or 'all'."},
                },
            },
            "input_remove_zones": {
                "label": "Input: Remove Zones",
                "params": {
                    "input_id": {"type": "child_id", "child_type": "input", "required": True},
                    "zone_ids": {"type": "string", "required": True,
                                 "help": "Comma-separated zone child IDs, or 'all'."},
                },
            },
            "input_rename": {
                "label": "Input: Rename",
                "params": {
                    "input_id": {"type": "child_id", "child_type": "input", "required": True},
                    "name": {"type": "string", "required": True},
                },
            },
            "input_set_order": {
                "label": "Input: Set Order",
                "params": {
                    "input_id": {"type": "child_id", "child_type": "input", "required": True},
                    "order": {"type": "integer", "required": True, "min": 0},
                },
            },
            "input_create": {
                "label": "Input: Create",
                "params": {
                    "name": {"type": "string", "required": True},
                    "mode": {"type": "enum", "required": True, "values": ["mic", "line"]},
                    "muted": {"type": "boolean", "required": True},
                    "format": {"type": "enum", "required": True, "values": ["mono", "stereo"]},
                    "channel": {"type": "integer", "required": False, "min": 1},
                    "left": {"type": "integer", "required": False, "min": 1},
                    "right": {"type": "integer", "required": False, "min": 1},
                    "volume": {"type": "number", "required": False, "min": -80, "max": 12},
                },
            },
            "input_remove": {
                "label": "Input: Remove",
                "params": {"input_id": {"type": "child_id", "child_type": "input", "required": True}},
            },

            # ============================================================ #
            # DSO (emergency input singleton)
            # ============================================================ #
            "dso_set_enabled": {
                "label": "DSO: Enable/Disable",
                "params": {"enabled": {"type": "boolean", "required": True}},
            },
            "dso_set_volume": {
                "label": "DSO: Set Volume",
                "params": {"volume": {"type": "number", "required": True, "min": -80, "max": 12}},
            },
            "dso_set_muted": {
                "label": "DSO: Set Mute",
                "params": {"muted": {"type": "boolean", "required": True}},
            },
            "dso_set_channel": {
                "label": "DSO: Set Channel",
                "params": {"channel": {"type": "integer", "required": True, "min": 1,
                                       "help": "Mono device channel."}},
            },
            "dso_remove_channel": {
                "label": "DSO: Remove Channel",
                "params": {},
            },
            "dso_set_eq_preset": {
                "label": "DSO: Assign EQ Preset",
                "params": {
                    "preset_id": {"type": "string", "required": True},
                    "bypassed": {"type": "boolean", "required": False},
                },
            },
            "dso_set_gate_preset": {
                "label": "DSO: Assign Gate Preset",
                "params": {
                    "preset_id": {"type": "string", "required": True},
                    "bypassed": {"type": "boolean", "required": False},
                },
            },
            "dso_set_compressor_preset": {
                "label": "DSO: Assign Compressor Preset",
                "params": {
                    "preset_id": {"type": "string", "required": True},
                    "bypassed": {"type": "boolean", "required": False},
                },
            },

            # ============================================================ #
            # PLAYLISTS
            # ============================================================ #
            "playlist_create": {
                "label": "Playlist: Create",
                "params": {"name": {"type": "string", "required": True}},
            },
            "playlist_remove": {
                "label": "Playlist: Remove",
                "params": {"playlist_id": {"type": "string", "required": True, "help": "Playlist UUID."}},
            },
            "playlist_rename": {
                "label": "Playlist: Rename",
                "params": {
                    "playlist_id": {"type": "string", "required": True},
                    "name": {"type": "string", "required": True},
                },
            },
            "playlist_set_order": {
                "label": "Playlist: Set Order",
                "params": {
                    "playlist_id": {"type": "string", "required": True},
                    "order": {"type": "integer", "required": True, "min": 0},
                },
            },
            "playlist_set_players": {
                "label": "Playlist: Set Players",
                "params": {
                    "playlist_id": {"type": "string", "required": True},
                    "player_ids": {"type": "string", "required": True,
                                   "help": "Comma-separated player child IDs, or 'all'."},
                },
            },
            "playlist_add_item": {
                "label": "Playlist: Add Item",
                "params": {
                    "playlist_id": {"type": "string", "required": True},
                    "path": {"type": "string", "required": True, "help": "File path to add."},
                },
            },
            "playlist_remove_item": {
                "label": "Playlist: Remove Item(s)",
                "params": {
                    "playlist_id": {"type": "string", "required": True},
                    "positions": {"type": "string", "required": True,
                                  "help": "Comma-separated positions, e.g. '0,3,10'."},
                },
            },
            "playlist_move_item": {
                "label": "Playlist: Move Item",
                "params": {
                    "playlist_id": {"type": "string", "required": True},
                    "source": {"type": "integer", "required": True, "min": 0},
                    "destination": {"type": "integer", "required": True, "min": 0},
                },
            },
            "playlist_scan_folder": {
                "label": "Playlist: Scan Folder Item",
                "params": {
                    "playlist_id": {"type": "string", "required": True},
                    "position": {"type": "integer", "required": True, "min": 0},
                },
            },
            "playlist_set_folder_autoscan": {
                "label": "Playlist: Set Folder Autoscan",
                "params": {
                    "playlist_id": {"type": "string", "required": True},
                    "position": {"type": "integer", "required": True, "min": 0},
                    "autoscan": {"type": "boolean", "required": True},
                },
            },

            # ============================================================ #
            # ANNOUNCEMENTS
            # ============================================================ #
            "announcement_create": {
                "label": "Announcement: Create",
                "params": {
                    "name": {"type": "string", "required": True},
                    "path": {"type": "string", "required": True, "help": "Path to the audio file."},
                    "ducking_volume": {"type": "number", "required": False,
                                       "help": "Level other sources duck to, dBFS."},
                    "volume": {"type": "number", "required": False, "min": -80, "max": 12},
                    "attack": {"type": "integer", "required": False, "help": "Ducking fade-in ms."},
                    "release": {"type": "integer", "required": False, "help": "Ducking fade-out ms."},
                    "priority": {"type": "integer", "required": False},
                },
            },
            "announcement_modify": {
                "label": "Announcement: Modify",
                "params": {
                    "announcement_id": {"type": "string", "required": True},
                    "name": {"type": "string", "required": False},
                    "order": {"type": "integer", "required": False, "min": 0},
                    "path": {"type": "string", "required": False},
                    "ducking_volume": {"type": "number", "required": False},
                    "volume": {"type": "number", "required": False, "min": -80, "max": 12},
                    "attack": {"type": "integer", "required": False},
                    "release": {"type": "integer", "required": False},
                    "priority": {"type": "integer", "required": False},
                },
            },
            "announcement_remove": {
                "label": "Announcement: Remove",
                "params": {"announcement_id": {"type": "string", "required": True}},
            },
            "announcement_play_to_zones": {
                "label": "Announcement: Play To Zones",
                "params": {
                    "announcement_id": {"type": "string", "required": True},
                    "zone_ids": {"type": "string", "required": True,
                                 "help": "Comma-separated zone child IDs, or 'all'."},
                },
            },
            "announcements_abort": {
                "label": "Announcement: Abort (by id)",
                "params": {
                    "announcement_ids": {"type": "string", "required": True,
                                         "help": "Comma-separated announcement UUIDs."},
                },
            },

            # ============================================================ #
            # PRESETS (eq / gate / ducker / limiter / compressor)
            # ============================================================ #
            "preset_create": {
                "label": "Preset: Create",
                "params": {
                    "kind": {"type": "enum", "required": True,
                             "values": ["eq", "gate", "ducker", "limiter", "compressor"]},
                    "name": {"type": "string", "required": True},
                    "preset_json": {"type": "string", "required": True,
                                    "help": "Preset parameters as a JSON object."},
                },
            },
            "preset_modify": {
                "label": "Preset: Modify",
                "params": {
                    "kind": {"type": "enum", "required": True,
                             "values": ["eq", "gate", "ducker", "limiter", "compressor"]},
                    "preset_id": {"type": "string", "required": True, "help": "Preset UUID."},
                    "rename": {"type": "string", "required": False},
                    "preset_json": {"type": "string", "required": False,
                                    "help": "New preset parameters as a JSON object."},
                },
            },
            "preset_remove": {
                "label": "Preset: Remove",
                "params": {
                    "kind": {"type": "enum", "required": True,
                             "values": ["eq", "gate", "ducker", "limiter", "compressor"]},
                    "preset_id": {"type": "string", "required": True},
                },
            },
            "preset_get_all": {
                "label": "Preset: Get All",
                "params": {
                    "kind": {"type": "enum", "required": True,
                             "values": ["eq", "gate", "ducker", "limiter", "compressor"]},
                },
            },

            # ============================================================ #
            # ACTIONS & ACTION GROUPS
            # ============================================================ #
            "action_create": {
                "label": "Action: Create",
                "params": {
                    "name": {"type": "string", "required": True},
                    "sequence_json": {"type": "string", "required": True,
                                      "help": "Sequence steps as a JSON array."},
                    "description": {"type": "string", "required": False},
                    "trigger_code": {"type": "string", "required": False},
                    "action_group_names": {"type": "string", "required": False,
                                           "help": "Comma-separated group names."},
                },
            },
            "action_modify": {
                "label": "Action: Modify",
                "params": {
                    "action_id": {"type": "string", "required": True},
                    "rename": {"type": "string", "required": False},
                    "order": {"type": "integer", "required": False, "min": 0},
                    "sequence_json": {"type": "string", "required": False},
                    "description": {"type": "string", "required": False},
                    "trigger_code": {"type": "string", "required": False},
                    "action_group_names": {"type": "string", "required": False},
                },
            },
            "action_play": {
                "label": "Action: Play",
                "params": {"action_ids": {"type": "string", "required": True,
                                          "help": "Comma-separated action UUIDs."}},
            },
            "action_stop": {
                "label": "Action: Stop",
                "params": {"action_id": {"type": "string", "required": True}},
            },
            "action_remove": {
                "label": "Action: Remove",
                "params": {"action_id": {"type": "string", "required": True}},
            },
            "action_get_all": {
                "label": "Action: Get All",
                "params": {},
            },
            "action_group_create": {
                "label": "Action Group: Create",
                "params": {
                    "name": {"type": "string", "required": True},
                    "action_ids": {"type": "string", "required": False,
                                   "help": "Comma-separated action UUIDs."},
                },
            },
            "action_group_modify": {
                "label": "Action Group: Modify",
                "params": {
                    "action_group_id": {"type": "string", "required": True},
                    "name": {"type": "string", "required": False},
                    "order": {"type": "integer", "required": False, "min": 0},
                    "action_ids": {"type": "string", "required": False},
                },
            },
            "action_group_remove": {
                "label": "Action Group: Remove",
                "params": {"action_group_id": {"type": "string", "required": True}},
            },
            "action_group_get_all": {
                "label": "Action Group: Get All",
                "params": {},
            },

            # ============================================================ #
            # EXTERNAL ACTIONS (network egress)
            # ============================================================ #
            "external_send_tcp": {
                "label": "External: Send TCP Message",
                "params": {
                    "target_url": {"type": "string", "required": True, "help": "ip:port or host:port."},
                    "body": {"type": "string", "required": True, "trim": False,
                             "help": "Sent verbatim - include any terminator the target needs."},
                    "timeout_ms": {"type": "integer", "required": False},
                },
            },
            "external_send_udp": {
                "label": "External: Send UDP Message",
                "params": {
                    "target_url": {"type": "string", "required": True},
                    "body": {"type": "string", "required": True, "trim": False,
                             "help": "Sent verbatim - include any terminator the target needs."},
                    "timeout_ms": {"type": "integer", "required": False},
                },
            },
            "external_send_tcp_hex": {
                "label": "External: Send TCP (hex)",
                "params": {
                    "target_url": {"type": "string", "required": True},
                    "body": {"type": "string", "required": True, "help": "Hex string (whitespace ok)."},
                    "timeout_ms": {"type": "integer", "required": False},
                },
            },
            "external_send_udp_hex": {
                "label": "External: Send UDP (hex)",
                "params": {
                    "target_url": {"type": "string", "required": True},
                    "body": {"type": "string", "required": True, "help": "Hex string (whitespace ok)."},
                    "timeout_ms": {"type": "integer", "required": False},
                },
            },
            "external_send_http": {
                "label": "External: Send HTTP Request",
                "params": {
                    "target_url": {"type": "string", "required": True},
                    "method": {"type": "enum", "required": True,
                               "values": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                    "body": {"type": "string", "required": False, "trim": False,
                             "help": "Sent verbatim as the request body."},
                    "timeout_ms": {"type": "integer", "required": False},
                },
            },
            "external_modbus_read": {
                "label": "External: Modbus Read",
                "params": {
                    "kind": {"type": "enum", "required": True,
                             "values": ["coils", "discrete-inputs", "holding-registers", "input-registers"]},
                    "target_url": {"type": "string", "required": True},
                    "uid": {"type": "integer", "required": True},
                    "address": {"type": "integer", "required": True},
                    "quantity": {"type": "integer", "required": True},
                    "port": {"type": "integer", "required": False, "default": 502},
                    "timeout_ms": {"type": "integer", "required": False},
                },
            },
            "external_modbus_write_single": {
                "label": "External: Modbus Write Single",
                "params": {
                    "kind": {"type": "enum", "required": True, "values": ["coil", "register"]},
                    "target_url": {"type": "string", "required": True},
                    "uid": {"type": "integer", "required": True},
                    "address": {"type": "integer", "required": True},
                    "value": {"type": "string", "required": True,
                              "help": "Coil: true/false. Register: integer."},
                    "port": {"type": "integer", "required": False, "default": 502},
                    "timeout_ms": {"type": "integer", "required": False},
                },
            },
            "external_modbus_write_multiple": {
                "label": "External: Modbus Write Multiple",
                "params": {
                    "kind": {"type": "enum", "required": True, "values": ["coils", "registers"]},
                    "target_url": {"type": "string", "required": True},
                    "uid": {"type": "integer", "required": True},
                    "address": {"type": "integer", "required": True},
                    "values_json": {"type": "string", "required": True,
                                    "help": "JSON array. Coils: booleans. Registers: integers."},
                    "port": {"type": "integer", "required": False, "default": 502},
                    "timeout_ms": {"type": "integer", "required": False},
                },
            },

            # ============================================================ #
            # PROJECTS
            # ============================================================ #
            "project_new": {
                "label": "Project: New",
                "params": {"project_name": {"type": "string", "required": True}},
            },
            "project_load": {
                "label": "Project: Load",
                "params": {"project_name": {"type": "string", "required": True}},
            },
            "project_save": {
                "label": "Project: Save",
                "params": {"project_name": {"type": "string", "required": False,
                                            "help": "Provide to save-as; omit to save current."}},
            },
            "project_remove": {
                "label": "Project: Remove",
                "params": {"project_name": {"type": "string", "required": True}},
            },
            "project_get_all": {
                "label": "Project: Get Current & Available",
                "params": {},
            },

            # ============================================================ #
            # SYSTEM / NETWORK / SOFTWARE UPDATE / LOGGER / USERS
            # ============================================================ #
            "system_get_status": {
                "label": "System: Get Status",
                "params": {},
            },
            "system_modify": {
                "label": "System: Modify Settings",
                "params": {"settings_json": {"type": "string", "required": True,
                                             "help": "Partial settings as a JSON object."}},
            },
            "system_set_timezone": {
                "label": "System: Set Timezone",
                "params": {"timezone": {"type": "string", "required": True}},
            },
            "system_get_timezones": {
                "label": "System: Get Timezones",
                "params": {},
            },
            "system_channels_limit": {
                "label": "System: Channels Limit",
                "params": {},
            },
            "system_restart": {
                "label": "System: Restart Audio Service",
                "params": {},
            },
            "network_get": {
                "label": "Network: Get Config",
                "params": {},
            },
            "network_set": {
                "label": "Network: Set Config",
                "params": {"config_json": {"type": "string", "required": True,
                                           "help": "Network config as a JSON object."}},
            },
            "network_validate": {
                "label": "Network: Validate",
                "params": {},
            },
            "network_revert": {
                "label": "Network: Revert",
                "params": {},
            },
            "software_update_check": {
                "label": "Update: Check",
                "params": {},
            },
            "software_update_download": {
                "label": "Update: Download",
                "params": {},
            },
            "software_update_remove": {
                "label": "Update: Remove Downloaded",
                "params": {},
            },
            "software_update_install": {
                "label": "Update: Install",
                "params": {},
            },
            "logger_get": {
                "label": "Logger: Get Logs (paged)",
                "params": {
                    "page": {"type": "integer", "required": True, "min": 1},
                    "size": {"type": "integer", "required": True, "min": 1},
                    "origin_name": {"type": "string", "required": False},
                    "action": {"type": "string", "required": False},
                    "sort": {"type": "enum", "required": False, "values": ["newest", "oldest"]},
                },
            },
            "users_get": {
                "label": "Users: Get All",
                "params": {},
            },
            "users_get_active": {
                "label": "Users: Get Active",
                "params": {},
            },
            "users_create": {
                "label": "Users: Create",
                "params": {
                    "name": {"type": "string", "required": True},
                    "email": {"type": "string", "required": True},
                    "user_type": {"type": "enum", "required": True, "values": ["admin", "user", "panel"]},
                    "auto_logout": {"type": "boolean", "required": False},
                },
            },
            "users_delete": {
                "label": "Users: Delete",
                "params": {"user_name": {"type": "string", "required": True}},
            },
            "users_generate_token": {
                "label": "Users: Generate Password Token",
                "params": {"user_name": {"type": "string", "required": True}},
            },
            "users_create_api_key": {
                "label": "Users: Create/Rotate API Key",
                "params": {"user_name": {"type": "string", "required": True}},
            },
            "users_remove_api_key": {
                "label": "Users: Remove API Key",
                "params": {"user_name": {"type": "string", "required": True}},
            },
            "users_logout": {
                "label": "Users: Logout",
                "params": {"user_name": {"type": "string", "required": True}},
            },

            # ============================================================ #
            # FILE MANAGER
            # ============================================================ #
            "file_list": {
                "label": "Files: List Folder",
                "params": {
                    "path": {"type": "string", "required": False},
                    "search": {"type": "string", "required": False},
                    "recursive": {"type": "boolean", "required": False},
                },
            },
            "file_disk_space": {
                "label": "Files: Disk Space",
                "params": {},
            },
            "file_create_folder": {
                "label": "Files: Create Folder",
                "params": {"path": {"type": "string", "required": True}},
            },
            "file_copy": {
                "label": "Files: Copy File",
                "params": {
                    "path_from": {"type": "string", "required": True},
                    "path_to": {"type": "string", "required": True},
                },
            },
            "file_move": {
                "label": "Files: Move",
                "params": {
                    "path_from": {"type": "string", "required": True,
                                  "help": "Comma-separated source paths."},
                    "path_to": {"type": "string", "required": True, "help": "Destination folder."},
                    "force": {"type": "boolean", "required": False},
                },
            },
            "file_rename": {
                "label": "Files: Rename/Move",
                "params": {
                    "path_from": {"type": "string", "required": True},
                    "path_to": {"type": "string", "required": True},
                },
            },
            "file_remove": {
                "label": "Files: Remove",
                "params": {
                    "path": {"type": "string", "required": True, "help": "Comma-separated paths."},
                    "force": {"type": "boolean", "required": False},
                },
            },

            # ============================================================ #
            # GENERIC ESCAPE HATCH
            # ============================================================ #
            "api_call": {
                "label": "Raw API Call",
                "params": {
                    "endpoint": {"type": "string", "required": True,
                                 "help": "Endpoint name, e.g. 'zone-get-status-all'."},
                    "params_json": {"type": "string", "required": False,
                                    "help": "Request params as a JSON object."},
                    "method": {"type": "enum", "required": False,
                               "values": ["GET", "POST", "PUT", "DELETE", "OPTIONS"]},
                },
                "help": "Call any SoundCoreHero endpoint directly. For endpoints not covered "
                        "by a typed command.",
            },
        },
        "protocols": ["soundcorehero"],
        "discovery": {
            # Fingerprint: the unit serves its web UI over HTTPS only, and its
            # static landing page carries <title>SoundCoreHero</title>. A TLS
            # GET of / is the one vendor-specific signal it exposes — the mDNS
            # it advertises (_netaudio-*) is the generic Audinate Dante stack
            # shared by every Dante device, so it is NOT a SoundCoreHero
            # fingerprint and is left for a future generic Dante driver.
            "tcp_probe": {
                "port": 443,
                "tls": True,
                "send_ascii": "GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                "expect": "SoundCoreHero",
            },
            # Hints — narrow candidates; never identify on their own.
            "hostname": ["soundcorehero"],   # default mDNS host: soundcorehero.local
            "manufacturer_alias": ["Hero AV", "SoundCoreHero"],
        },
    }

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def connect(self) -> None:
        self._session_id: Optional[str] = None
        self._receiver_id = str(uuidlib.uuid4())
        self._ws_task: Optional[asyncio.Task] = None
        self._ws_ready = asyncio.Event()
        self._client: Optional[httpx.AsyncClient] = None
        # order (int) -> uuid (str), per child type. Rebuilt on each snapshot.
        self._order_to_uuid: dict[str, dict[int, str]] = {c: {} for c in _SNAPSHOT_KEYS.values()}
        # uuid -> last full object, per child type. Snapshots replace it;
        # incremental updates (JSON-Patch arrays) are applied on top of it.
        self._obj_cache: dict[str, dict[str, dict[str, Any]]] = {c: {} for c in _SNAPSHOT_KEYS.values()}
        # Fallback local-id allocation for objects without an `order` field
        # (e.g. controllers). uuid -> synthetic int, plus a per-type counter.
        self._synthetic_ids: dict[str, dict[str, int]] = {c: {} for c in _SNAPSHOT_KEYS.values()}
        self._synthetic_next: dict[str, int] = {c: 0 for c in _SNAPSHOT_KEYS.values()}
        # Last full DSO object (device-level singleton). Snapshot replaces it;
        # dsoUpdate JSON-Patch arrays are applied on top.
        self._dso_cache: dict[str, Any] = {}

        host = self.config.get("host", "")
        port = int(self.config.get("port", 443))
        verify = self._as_bool(self.config.get("verify_ssl", False))
        base_url = f"https://{host}:{port}"

        self._client = httpx.AsyncClient(base_url=base_url, verify=verify, timeout=10.0)

        # 1. Log in to obtain a session (required by the control WebSocket).
        await self._login()

        # 2. Start the WebSocket receiver. It performs the auth hand-shake and
        #    consumes the initial snapshot + incremental updates.
        log.info(f"[{self.device_id}] Opening control WebSocket")
        self._ws_task = asyncio.create_task(self._ws_loop(host, port, verify))

        # 3. Wait until the first snapshot has been applied before declaring
        #    the device connected.
        try:
            await asyncio.wait_for(self._ws_ready.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            await self._teardown()
            raise ConnectionError(
                f"[{self.device_id}] No snapshot within 15s "
                f"(WS opened but no resource collection arrived)"
            )
        log.info(f"[{self.device_id}] Snapshot applied, device ready")

        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")

        poll_interval = int(self.config.get("poll_interval", 20))
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def disconnect(self) -> None:
        await self.stop_polling()
        await self._teardown()
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")

    async def _teardown(self) -> None:
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
            self._ws_task = None
        # Best-effort logout, then close the HTTP client.
        if self._client and self._session_id:
            try:
                await self._client.post(
                    "/api/usersmanager-logout",
                    headers=self._auth_headers(),
                    json={"userName": self.config.get("email", "")},
                )
            except Exception:
                pass
        if self._client:
            await self._client.aclose()
            self._client = None
        self._session_id = None
        self._ws_ready.clear()

    # ------------------------------------------------------------------ #
    # Auth / HTTP
    # ------------------------------------------------------------------ #
    def _auth_headers(self) -> dict[str, str]:
        return {"X-Session-Id": self._session_id} if self._session_id else {}

    @staticmethod
    def _as_bool(value: Any) -> bool:
        """Coerce UI/config values to bool. The Add Device toggle may deliver
        a real bool, or a string like 'No'/'false'/'0' — bool('No') is True,
        which would wrongly enable cert verification, so parse explicitly."""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    async def _login(self) -> None:
        assert self._client is not None
        email = self.config.get("email", "")
        log.info(f"[{self.device_id}] Logging in as {email!r} at {self._client.base_url}")
        try:
            resp = await self._client.post(
                "/api/usersmanager-login",
                json={"email": email, "password": self.config.get("password", "")},
            )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError) as exc:
            raise ConnectionError(f"[{self.device_id}] Login transport error: {exc}") from exc

        body: dict[str, Any] = {}
        try:
            body = resp.json()
        except Exception:
            pass
        if resp.status_code != 200:
            reason = body.get("error", f"HTTP {resp.status_code}")
            raise ConnectionError(f"[{self.device_id}] Login rejected: {reason}")
        self._session_id = body.get("sessionId")
        if not self._session_id:
            raise ConnectionError(f"[{self.device_id}] Login response missing sessionId")
        log.info(f"[{self.device_id}] Logged in, session acquired")

    async def _post(self, endpoint: str, params: dict[str, Any],
                    method: str = "POST") -> dict[str, Any]:
        """Send a command. Note: the API returns HTTP 200 even on some errors,
        with the reason in the body under 'error' — so we check both.

        method selects the HTTP verb. GET/OPTIONS carry params as query string;
        POST/PUT/DELETE carry them as a JSON body."""
        assert self._client is not None
        if not self._session_id:
            raise ConnectionError(f"[{self.device_id}] Not authenticated")
        verb = method.upper()
        url = f"/api/{endpoint}"
        headers = self._auth_headers()
        if verb in ("GET", "OPTIONS"):
            resp = await self._client.request(verb, url, headers=headers, params=params or None)
        else:
            resp = await self._client.request(verb, url, headers=headers, json=params)
        if resp.status_code == 401:
            # Session expired — surface as a connection error so the watchdog
            # tears the device down and reconnects (re-login).
            raise ConnectionError(f"[{self.device_id}] Session rejected (401) on {endpoint}")
        body: dict[str, Any] = {}
        try:
            body = resp.json()
        except Exception:
            pass
        if resp.status_code >= 400 or "error" in body:
            raise ValueError(f"[{self.device_id}] {endpoint} failed: {body.get('error', resp.status_code)}")
        return body

    # ------------------------------------------------------------------ #
    # WebSocket receiver
    # ------------------------------------------------------------------ #
    async def _ws_loop(self, host: str, port: int, verify: bool) -> None:
        ws_url = f"wss://{host}:{port}/ws/"
        ssl_ctx = ssl.create_default_context()
        if not verify:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        try:
            async with websockets.connect(ws_url, ssl=ssl_ctx) as ws:
                log.info(f"[{self.device_id}] WS connected to {ws_url}, awaiting prompt")
                # The server first pushes {"status": "unauthorized"} and waits
                # for the (sessionId, receiverId) auth frame.
                async for raw in ws:
                    msg = json.loads(raw)
                    # Hand-shake: reply to the unauthorized prompt once.
                    if msg.get("status") == "unauthorized":
                        log.info(f"[{self.device_id}] WS prompt received, sending auth frame")
                        await ws.send(json.dumps({
                            "sessionId": self._session_id,
                            "receiverId": self._receiver_id,
                        }))
                        continue
                    if msg.get("status") == "failed":
                        raise ConnectionError(f"[{self.device_id}] WS auth rejected (session invalid)")
                    self._handle_ws_message(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(f"[{self.device_id}] WebSocket closed: {exc!r}")
            # Let the watchdog notice and trigger reconnect.
            self._connected = False
            self.set_state("connected", False)
            await self.events.emit(f"device.disconnected.{self.device_id}")

    def _handle_ws_message(self, msg: Any) -> None:
        # Some server frames are JSON arrays (e.g. certain list payloads) or
        # scalars rather than objects. Only dict frames carry resource keys.
        if not isinstance(msg, dict):
            return
        # allowedPages arrives right after a successful hand-shake; ignore.
        if "allowedPages" in msg and len(msg) == 1:
            return
        if msg.get("message") == "goodbye":
            self._connected = False
            self.set_state("connected", False)
            return

        applied_snapshot = False
        for key, value in msg.items():
            if key in _SNAPSHOT_KEYS:
                self._apply_collection(_SNAPSHOT_KEYS[key], value, snapshot=True)
                applied_snapshot = True
            elif key in _UPDATE_KEYS:
                self._apply_collection(_UPDATE_KEYS[key], value, snapshot=False)
            elif key == "dso":
                self._apply_dso(value, snapshot=True)
            elif key == "dsoUpdate":
                self._apply_dso(value, snapshot=False)
            elif key == "internetStatus":
                self.set_state("internet", bool(value))
            elif key == "cpuUsage":
                self._set_cpu_usage(value)
            elif key == "serverTime":
                if isinstance(value, str):
                    self.set_state("server_time", value)
            elif key == "channelsLimit":
                self._set_channels_limit(value)
            elif key == "softwareUpdateStatus":
                self._set_update_status(value)
            elif key == "systemError":
                text = self._signal_text(value)
                self.set_state("last_system_error", text)
                log.warning(f"[{self.device_id}] system error: {text}")
            elif key == "actionError":
                text = self._signal_text(value)
                self.set_state("last_action_error", text)
                log.warning(f"[{self.device_id}] action error: {text}")
            elif key == "config" and isinstance(value, dict):
                if "defaultProject" in value:
                    self.set_state("current_project", value.get("defaultProject"))

        # The first message carrying any resource collection counts as the
        # snapshot that unblocks connect().
        if applied_snapshot and not self._ws_ready.is_set():
            self._ws_ready.set()

    def _apply_collection(self, ctype: str, objects: dict[str, Any], *, snapshot: bool) -> None:
        """Register/update children.

        Two wire shapes:
          * snapshot   -> {uuid: <full object>}
          * incremental -> {uuid: [<RFC 6902 patch op>, ...]}

        Snapshots replace the cached object; deltas are JSON-Patch arrays
        applied on top of the cached object. After either, we re-extract the
        flat child state and (re)register the child. On a full snapshot we also
        reconcile: anything not present is removed.
        """
        if not isinstance(objects, dict):
            return
        seen_orders: set[int] = set()
        changed_uuids: set[str] = set()
        for obj_uuid, payload in objects.items():
            if snapshot:
                if not isinstance(payload, dict):
                    continue
                obj = payload
                self._obj_cache[ctype][obj_uuid] = obj
            else:
                # Incremental: payload is a list of patch ops applied to the
                # last known full object. If we never saw a snapshot for this
                # uuid, we cannot reconstruct it — skip until one arrives.
                obj = self._obj_cache[ctype].get(obj_uuid)
                if obj is None:
                    continue
                if isinstance(payload, list):
                    self._apply_patch(obj, payload)
                elif isinstance(payload, dict):
                    # Defensive: some builds may send a partial object instead.
                    obj.update(payload)
                else:
                    continue

            order = obj.get("order")
            if order is None:
                # Some objects (notably controllers) may not expose an
                # `order`. Synthesize a stable integer local_id from a
                # per-type uuid->id map so they can still be registered and
                # addressed consistently across snapshots and deltas.
                order = self._synthetic_id(ctype, obj_uuid)
            order = int(order)
            seen_orders.add(order)
            changed_uuids.add(obj_uuid)
            self._order_to_uuid[ctype][order] = obj_uuid
            state = self._extract_state(ctype, obj_uuid, obj)
            # register_child only creates the child; its initial_state is
            # ignored for an already-registered child. Push live state through
            # set_child_state_batch so both snapshots and deltas reach the UI.
            if not self.is_child_registered(ctype, order):
                self.register_child(ctype, order, initial_state=state)
            else:
                self.set_child_state_batch(ctype, order, state)

        if snapshot:
            # Drop children that vanished from the device.
            live_uuids = set(objects.keys())
            for stale in set(self._order_to_uuid[ctype]) - seen_orders:
                self.deregister_child(ctype, stale)
                self._order_to_uuid[ctype].pop(stale, None)
            for cached in set(self._obj_cache[ctype]) - live_uuids:
                self._obj_cache[ctype].pop(cached, None)

        # A controller mirrors the master volume of its bound zone. When zones
        # change (snapshot or delta) re-push any controller bound to a changed
        # zone so its `zone_volume` stays live.
        if ctype == "zone" and changed_uuids:
            self._refresh_controllers_for_zones(changed_uuids)

    def _refresh_controllers_for_zones(self, zone_uuids: set[str]) -> None:
        for ctrl_uuid, ctrl in (self._obj_cache.get("controller") or {}).items():
            if not isinstance(ctrl, dict):
                continue
            bound = (ctrl.get("connections") or {}).get("zone")
            if bound not in zone_uuids:
                continue
            order = next(
                (o for o, u in self._order_to_uuid["controller"].items() if u == ctrl_uuid),
                None,
            )
            if order is None or not self.is_child_registered("controller", order):
                continue
            vol = self._zone_master_volume(bound)
            self.set_child_state_batch("controller", order, {"zone_volume": vol})

    @staticmethod
    def _apply_patch(obj: dict[str, Any], ops: list[Any]) -> None:
        """Apply a minimal subset of RFC 6902 JSON-Patch in place.

        SoundCoreHero emits flat single-level paths like "/progress" and
        occasionally nested ones like "/mixers/master/volume". Supports
        replace/add (same effect) and remove. Array indices are handled when
        a path segment is numeric."""
        for op in ops:
            if not isinstance(op, dict):
                continue
            action = op.get("op")
            path = op.get("path")
            if not isinstance(path, str) or not path:
                continue
            segments = [_unescape_json_pointer(p) for p in path.lstrip("/").split("/")]
            target = obj
            ok = True
            for seg in segments[:-1]:
                target, ok = _descend(target, seg)
                if not ok:
                    break
            if not ok or not isinstance(target, (dict, list)):
                continue
            leaf = segments[-1]
            if action in ("replace", "add"):
                _assign(target, leaf, op.get("value"))
            elif action == "remove":
                _remove(target, leaf)

    # ------------------------------------------------------------------ #
    # DSO + system signals -> device-level state
    # ------------------------------------------------------------------ #
    def _apply_dso(self, value: Any, *, snapshot: bool) -> None:
        """DSO is a device-level singleton. Snapshot ('dso') carries the full
        object; 'dsoUpdate' carries a JSON-Patch array applied on top."""
        if snapshot:
            if not isinstance(value, dict):
                return
            self._dso_cache = value
        else:
            if not self._dso_cache:
                return
            if isinstance(value, list):
                self._apply_patch(self._dso_cache, value)
            elif isinstance(value, dict):
                self._dso_cache.update(value)
            else:
                return
        d = self._dso_cache
        cf = d.get("channelsFormat") or {}
        updates = {
            "dso_enabled": d.get("enabled"),
            "dso_volume": d.get("volume"),
            "dso_muted": d.get("muted"),
            "dso_channels_format": _fmt_channels(cf),
            "dso_signal_active": d.get("signalActive"),
            "dso_eq_preset": d.get("presetEqId"),
            "dso_eq_bypassed": d.get("presetEqBypassed"),
            "dso_gate_preset": d.get("presetGateId"),
            "dso_gate_bypassed": d.get("presetGateBypassed"),
            "dso_compressor_preset": d.get("presetCompressorId"),
            "dso_compressor_bypassed": d.get("presetCompressorBypassed"),
        }
        self.set_states({k: v for k, v in updates.items() if v is not None})

    def _set_cpu_usage(self, value: Any) -> None:
        if isinstance(value, (int, float)):
            self.set_state("cpu_usage", float(value))
        elif isinstance(value, dict):
            for k in ("usage", "cpu", "percent", "value"):
                if isinstance(value.get(k), (int, float)):
                    self.set_state("cpu_usage", float(value[k]))
                    return

    def _set_channels_limit(self, value: Any) -> None:
        if not isinstance(value, dict):
            return
        inner = value.get("channelsLimit") if isinstance(value.get("channelsLimit"), dict) else value
        self.set_states({k: v for k, v in {
            "channels_used_inputs": inner.get("usedInputs"),
            "channels_used_outputs": inner.get("usedOutputs"),
            "channels_max": inner.get("maxAllowed"),
        }.items() if v is not None})

    def _set_update_status(self, value: Any) -> None:
        """softwareUpdateStatus: surface to state and log it (both)."""
        if isinstance(value, dict):
            inner = value.get("softwareUpdate") if isinstance(value.get("softwareUpdate"), dict) else value
            prog = inner.get("downloadProgress")
            size = inner.get("updateSize")
            if isinstance(prog, (int, float)) and isinstance(size, (int, float)) and size:
                self.set_state("update_progress", round(prog / size * 100.0, 1))
            text = json.dumps(inner, ensure_ascii=False, default=str)
        else:
            text = str(value)
        self.set_state("update_status", text)
        log.info(f"[{self.device_id}] software update status: {text}")

    @staticmethod
    def _signal_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for k in ("error", "message", "text", "reason"):
                if isinstance(value.get(k), str):
                    return value[k]
        return json.dumps(value, ensure_ascii=False, default=str)

    def _zone_master_volume(self, zone_uuid: Optional[str]) -> Optional[float]:
        """Live master volume of a zone, read from the WS object cache.

        Controllers (e.g. CC-3D) have no volume field of their own — they ride
        the master volume of their bound zone. We surface that value so the
        controller card reflects the current level live.
        """
        if not zone_uuid:
            return None
        zone = (self._obj_cache.get("zone") or {}).get(zone_uuid)
        if not isinstance(zone, dict):
            return None
        master = (zone.get("mixers") or {}).get("master") or {}
        return master.get("volume")

    def _extract_state(self, ctype: str, obj_uuid: str, obj: dict[str, Any]) -> dict[str, Any]:
        base = {"uuid": obj_uuid, "name": obj.get("name", ""), "online": True}
        if ctype == "zone":
            mixers = obj.get("mixers", {}) or {}
            master = mixers.get("master", {}) or {}
            player = mixers.get("player", {}) or {}
            dso = mixers.get("dso", {}) or {}
            ann_mix = mixers.get("announcement", {}) or {}
            ann = obj.get("announcementInfo", {}) or {}
            conns = obj.get("connections") or {}
            base.update({
                "master_volume": master.get("volume"),
                "master_muted": master.get("muted"),
                "master_balance": master.get("balance"),
                "player_volume": player.get("volume"),
                "player_muted": player.get("muted"),
                "player_balance": player.get("balance"),
                "dso_volume": dso.get("volume"),
                "dso_muted": dso.get("muted"),
                "announcement_volume": ann_mix.get("volume"),
                "announcement_muted": ann_mix.get("muted"),
                "announcement_state": ann.get("announcementState"),
                "announcement_duration": ann.get("announcementDuration"),
                "announcement_progress": ann.get("announcementProgress"),
                "line_ducker_bypassed": obj.get("linePresetDuckerBypassed"),
                "mic_ducker_bypassed": obj.get("micPresetDuckerBypassed"),
                "dso_ducker_bypassed": obj.get("dsoPresetDuckerBypassed"),
                "line_ducker_preset": obj.get("linePresetDuckerId"),
                "mic_ducker_preset": obj.get("micPresetDuckerId"),
                "dso_ducker_preset": obj.get("dsoPresetDuckerId"),
                "connected_player": conns.get("player"),
                "line_inputs": _csv(conns.get("lineInputs")),
                "mic_inputs": _csv(conns.get("micInputs")),
                "speakers": _csv(conns.get("speakers")),
                "order": obj.get("order"),
                "signal_active": obj.get("signalActive"),
            })
        elif ctype == "player":
            track = obj.get("track") or {}
            track_file = (track.get("file") or {}) if isinstance(track, dict) else {}
            conns = obj.get("connections") or {}
            base.update({
                "state": obj.get("state"),
                "volume": obj.get("volume"),
                "muted": obj.get("muted"),
                "source_type": obj.get("sourceType"),
                "stream_url": obj.get("streamUrl"),
                "track_name": track_file.get("relativePath"),
                "track_path": track_file.get("absolutePath"),
                "track_position": track_file.get("position"),
                "track_path_valid": track_file.get("pathValid"),
                "progress": obj.get("progress"),
                "duration": obj.get("duration"),
                "remaining": obj.get("remaining"),
                "shuffle": obj.get("shuffle"),
                "repeat_one": obj.get("repeatOne"),
                "repeat_all": obj.get("repeatAll"),
                "fade": obj.get("fade"),
                "playlist_uuid": obj.get("playlist"),
                "zones": _csv(conns.get("zones")),
                "order": obj.get("order"),
                "signal_active": obj.get("signalActive"),
            })
        elif ctype == "speaker":
            delay = obj.get("delay") or {}
            limiter = obj.get("limiter") or {}
            conns = obj.get("connections") or {}
            base.update({
                "type": obj.get("type"),
                "description": obj.get("description"),
                "volume": obj.get("volume"),
                "muted": obj.get("muted"),
                "delay_time": delay.get("time") if isinstance(delay, dict) else None,
                "delay_bypassed": obj.get("delayBypassed"),
                "limiter_threshold": limiter.get("threshold") if isinstance(limiter, dict) else None,
                "limiter_release": limiter.get("release") if isinstance(limiter, dict) else None,
                "limiter_mode": limiter.get("mode") if isinstance(limiter, dict) else None,
                "eq_preset": obj.get("presetEqId"),
                "eq_bypassed": obj.get("presetEqBypassed"),
                "limiter_preset": obj.get("presetLimiterId"),
                "limiter_preset_bypassed": obj.get("presetLimiterBypassed"),
                "playing_pink_noise": obj.get("playingPinkNoise"),
                "zone": conns.get("zone"),
                "devices": _csv(conns.get("devices")),
                "order": obj.get("order"),
                "signal_active": obj.get("signalActive"),
            })
        elif ctype == "input":
            cf = obj.get("channelsFormat") or {}
            conns = obj.get("connections") or {}
            base.update({
                "mode": obj.get("mode"),
                "volume": obj.get("volume"),
                "muted": obj.get("muted"),
                "channels_format": _fmt_channels(cf),
                "eq_preset": obj.get("presetEqId"),
                "eq_bypassed": obj.get("presetEqBypassed"),
                "gate_preset": obj.get("presetGateId"),
                "gate_bypassed": obj.get("presetGateBypassed"),
                "compressor_preset": obj.get("presetCompressorId"),
                "compressor_bypassed": obj.get("presetCompressorBypassed"),
                "zones": _csv(conns.get("zones")),
                "order": obj.get("order"),
                "signal_active": obj.get("signalActive"),
            })
        elif ctype == "playlist":
            contents = obj.get("contents")
            base.update({
                "root_path": obj.get("rootPath"),
                "item_count": len(contents) if isinstance(contents, list) else None,
                "order": obj.get("order"),
            })
        elif ctype == "announcement":
            base.update({
                "path": obj.get("path"),
                "path_valid": obj.get("pathValid"),
                "volume": obj.get("volume"),
                "priority": obj.get("priority"),
                "attack": obj.get("attack"),
                "release": obj.get("release"),
                "ducking_volume": obj.get("duckingVolume"),
                "duration": obj.get("duration"),
                "order": obj.get("order"),
            })
        elif ctype == "action":
            seq = obj.get("sequence")
            base.update({
                "running": obj.get("running"),
                "trigger_code": obj.get("triggerCode"),
                "step_count": len(seq) if isinstance(seq, list) else None,
                "group_ids": _csv(obj.get("actionGroupIds")),
                "description": obj.get("description"),
                "order": obj.get("order"),
            })
        elif ctype == "action_group":
            ids = obj.get("actionIds")
            base.update({
                "action_ids": _csv(ids),
                "action_count": len(ids) if isinstance(ids, list) else None,
                "order": obj.get("order"),
            })
        elif ctype == "controller":
            conns = obj.get("connections") or {}
            players = conns.get("players") if isinstance(conns.get("players"), list) else []
            sel = obj.get("select")
            selected_player = None
            if isinstance(sel, int) and 0 <= sel < len(players):
                selected_player = players[sel]
            base.update({
                "model": obj.get("model"),
                "ip": obj.get("ip"),
                "mac": obj.get("mac"),
                # `online` is reported by the controller itself; override the
                # default True so a disconnected wall unit shows correctly.
                "online": obj.get("online"),
                "zone": conns.get("zone"),
                "zone_volume": self._zone_master_volume(conns.get("zone")),
                "selected_index": sel,
                "selected_player": selected_player,
                "players": _csv(players),
                "order": obj.get("order"),
                "raw_json": json.dumps(obj, ensure_ascii=False, default=str),
            })
        # Strip None so we don't clobber existing values on partial updates.
        return {k: v for k, v in base.items() if v is not None}

    def _synthetic_id(self, ctype: str, obj_uuid: str) -> int:
        """Return a stable integer local_id for an object that lacks an
        `order` field, allocating a fresh one on first sight."""
        table = self._synthetic_ids[ctype]
        if obj_uuid not in table:
            table[obj_uuid] = self._synthetic_next[ctype]
            self._synthetic_next[ctype] += 1
        return table[obj_uuid]

    def _resolve(self, ctype: str, order: Any) -> str:
        """Resolve a child integer local_id (order) to the device UUID."""
        try:
            order_int = int(order)
        except (TypeError, ValueError):
            raise ValueError(f"[{self.device_id}] Invalid {ctype} id: {order!r}")
        obj_uuid = self._order_to_uuid.get(ctype, {}).get(order_int)
        if not obj_uuid:
            raise ValueError(f"[{self.device_id}] Unknown {ctype} (order {order_int}). "
                             f"State may be stale — try refreshing.")
        return obj_uuid

    # ------------------------------------------------------------------ #
    # Command helpers
    # ------------------------------------------------------------------ #
    def _resolve_ids(self, ctype: str, raw: Any) -> Any:
        """Resolve a comma-separated child-id string (or 'all') to a list of
        device UUIDs. 'all' is passed straight through to the API."""
        if raw is None:
            return []
        if isinstance(raw, str) and raw.strip().lower() == "all":
            return "all"
        if isinstance(raw, (list, tuple)):
            items = raw
        else:
            items = [p for p in str(raw).split(",") if p.strip() != ""]
        return [self._resolve(ctype, p.strip()) for p in items]

    @staticmethod
    def _opt(params: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
        """Build a body from params, including only keys that are present.
        mapping: {param_key: api_field}. Values are passed through as-is."""
        body: dict[str, Any] = {}
        for pkey, afield in mapping.items():
            if pkey in params and params[pkey] is not None:
                body[afield] = params[pkey]
        return body

    @staticmethod
    def _channels_format(params: dict[str, Any]) -> dict[str, Any]:
        fmt = params.get("format")
        if fmt == "mono":
            return {"mono": {"channel": int(params["channel"])}}
        if fmt == "stereo":
            return {"stereo": {"left": int(params["left"]), "right": int(params["right"])}}
        return {}

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        p = params or {}

        match command:
            # ===================== PLAYERS ============================= #
            case ("player_play" | "player_pause" | "player_stop"
                  | "player_next" | "player_previous" | "player_previous_track"):
                action = {
                    "player_play": "play", "player_pause": "pause", "player_stop": "stop",
                    "player_next": "next", "player_previous": "previous",
                    "player_previous_track": "previousTrack",
                }[command]
                pid = self._resolve("player", p["player_id"])
                return await self._post("players-action", {"playerIds": [pid], "action": action})

            case "player_set_volume":
                pid = self._resolve("player", p["player_id"])
                return await self._post("players-modify", {"playerIds": [pid], "volume": float(p["volume"])})
            case "player_set_muted":
                pid = self._resolve("player", p["player_id"])
                return await self._post("players-modify", {"playerIds": [pid], "muted": bool(p["muted"])})
            case "player_set_shuffle":
                pid = self._resolve("player", p["player_id"])
                return await self._post("players-modify", {"playerIds": [pid], "shuffle": bool(p["shuffle"])})
            case "player_set_repeat_one":
                pid = self._resolve("player", p["player_id"])
                return await self._post("players-modify", {"playerIds": [pid], "repeatOne": bool(p["repeat_one"])})
            case "player_set_repeat_all":
                pid = self._resolve("player", p["player_id"])
                return await self._post("players-modify", {"playerIds": [pid], "repeatAll": bool(p["repeat_all"])})
            case "player_set_fade":
                pid = self._resolve("player", p["player_id"])
                return await self._post("players-modify", {"playerIds": [pid], "fadeTime": float(p["fade"])})
            case "player_set_track":
                pid = self._resolve("player", p["player_id"])
                return await self._post("player-set-track", {"playerId": pid, "position": int(p["position"])})
            case "player_set_playback_time":
                pid = self._resolve("player", p["player_id"])
                return await self._post("player-set-playback-time", {"playerId": pid, "time": float(p["time"])})
            case "player_set_source":
                pid = self._resolve("player", p["player_id"])
                body = {"playerIds": [pid], "sourceType": p["source_type"]}
                if p.get("stream_url"):
                    body["streamUrl"] = p["stream_url"]
                return await self._post("players-modify", body)
            case "player_set_zones":
                pid = self._resolve("player", p["player_id"])
                return await self._post("player-set-zones",
                                        {"playerId": pid, "zoneIds": self._resolve_ids("zone", p["zone_ids"])})
            case "player_add_zones":
                pid = self._resolve("player", p["player_id"])
                return await self._post("player-add-zones",
                                        {"playerId": pid, "zoneIds": self._resolve_ids("zone", p["zone_ids"])})
            case "player_remove_zones":
                pid = self._resolve("player", p["player_id"])
                return await self._post("player-remove-zones",
                                        {"playerId": pid, "zoneIds": self._resolve_ids("zone", p["zone_ids"])})
            case "player_set_playlist":
                pid = self._resolve("player", p["player_id"])
                return await self._post("player-set-playlist",
                                        {"playerIds": [pid], "playlistId": p["playlist_uuid"]})
            case "player_remove_playlist":
                pid = self._resolve("player", p["player_id"])
                return await self._post("player-remove-playlist",
                                        {"playerId": pid, "playlistId": p["playlist_uuid"]})
            case "player_rename":
                pid = self._resolve("player", p["player_id"])
                return await self._post("players-modify", {"playerIds": [pid], "rename": p["name"]})
            case "player_set_order":
                pid = self._resolve("player", p["player_id"])
                return await self._post("players-modify", {"playerIds": [pid], "order": int(p["order"])})
            case "player_create":
                body = {"name": p["name"], "sourceType": p["source_type"]}
                body.update(self._opt(p, {
                    "stream_url": "streamUrl", "volume": "volume", "muted": "muted",
                    "fade_time": "fadeTime", "repeat_one": "repeatOne",
                    "repeat_all": "repeatAll", "shuffle": "shuffle",
                }))
                return await self._post("player-create", body)
            case "player_remove":
                pid = self._resolve("player", p["player_id"])
                return await self._post("player-remove", {"playerId": pid})

            # ===================== ZONES =============================== #
            case "zone_set_mixer":
                zid = self._resolve("zone", p["zone_id"])
                strip = self._opt(p, {"volume": "volume", "balance": "balance", "muted": "muted"})
                return await self._post("zones-modify",
                                        {"zoneIds": [zid], "mixers": {p["bus"]: strip}})
            case "zone_set_input_mixer":
                zid = self._resolve("zone", p["zone_id"])
                iid = self._resolve("input", p["input_id"])
                strip = {"inputId": iid}
                strip.update(self._opt(p, {"volume": "volume", "balance": "balance", "muted": "muted"}))
                return await self._post("zones-modify",
                                        {"zoneIds": [zid], "mixers": {p["bus"]: [strip]}})
            case "zone_set_master_volume":
                zid = self._resolve("zone", p["zone_id"])
                return await self._post("zones-modify",
                                        {"zoneIds": [zid], "mixers": {"master": {"volume": float(p["volume"])}}})
            case "zone_set_master_muted":
                zid = self._resolve("zone", p["zone_id"])
                return await self._post("zones-modify",
                                        {"zoneIds": [zid], "mixers": {"master": {"muted": bool(p["muted"])}}})
            case "zone_set_ducker":
                zid = self._resolve("zone", p["zone_id"])
                duck = {
                    "threshold": float(p["threshold"]), "range": float(p["range"]),
                    "attack": int(p["attack"]), "hold": int(p["hold"]), "release": int(p["release"]),
                }
                return await self._post("zones-modify",
                                        {"zoneIds": [zid], "duckers": {p["ducker"]: duck}})
            case "zone_set_ducker_preset":
                zid = self._resolve("zone", p["zone_id"])
                field = {"line": "linePresetDuckerId", "mic": "micPresetDuckerId", "dso": "dsoPresetDuckerId"}[p["ducker"]]
                body = {"zoneIds": [zid], field: p["preset_id"]}
                if "bypassed" in p and p["bypassed"] is not None:
                    bfield = {"line": "linePresetDuckerBypassed", "mic": "micPresetDuckerBypassed",
                              "dso": "dsoPresetDuckerBypassed"}[p["ducker"]]
                    body[bfield] = bool(p["bypassed"])
                return await self._post("zones-modify", body)
            case "zone_set_ducker_bypass":
                zid = self._resolve("zone", p["zone_id"])
                bfield = {"line": "linePresetDuckerBypassed", "mic": "micPresetDuckerBypassed",
                          "dso": "dsoPresetDuckerBypassed"}[p["ducker"]]
                return await self._post("zones-modify", {"zoneIds": [zid], bfield: bool(p["bypassed"])})
            case "zone_set_players":
                zid = self._resolve("zone", p["zone_id"])
                return await self._post("zone-set-players",
                                        {"zoneId": zid, "playerIds": [self._resolve("player", p["player_id"])]})
            case "zone_add_players":
                zid = self._resolve("zone", p["zone_id"])
                return await self._post("zone-add-players",
                                        {"zoneId": zid, "playerIds": [self._resolve("player", p["player_id"])]})
            case "zone_remove_players":
                zid = self._resolve("zone", p["zone_id"])
                return await self._post("zone-remove-players",
                                        {"zoneId": zid, "playerIds": [self._resolve("player", p["player_id"])]})
            case "zone_set_inputs":
                zid = self._resolve("zone", p["zone_id"])
                return await self._post("zone-set-inputs",
                                        {"zoneId": zid, "inputIds": self._resolve_ids("input", p["input_ids"])})
            case "zone_add_inputs":
                zid = self._resolve("zone", p["zone_id"])
                return await self._post("zone-add-inputs",
                                        {"zoneId": zid, "inputIds": self._resolve_ids("input", p["input_ids"])})
            case "zone_remove_inputs":
                zid = self._resolve("zone", p["zone_id"])
                return await self._post("zone-remove-inputs",
                                        {"zoneId": zid, "inputIds": self._resolve_ids("input", p["input_ids"])})
            case "zone_set_speakers":
                zid = self._resolve("zone", p["zone_id"])
                return await self._post("zone-set-speakers",
                                        {"zoneId": zid, "speakerIds": self._resolve_ids("speaker", p["speaker_ids"])})
            case "zone_add_speakers":
                zid = self._resolve("zone", p["zone_id"])
                return await self._post("zone-add-speakers",
                                        {"zoneId": zid, "speakerIds": self._resolve_ids("speaker", p["speaker_ids"])})
            case "zone_remove_speakers":
                zid = self._resolve("zone", p["zone_id"])
                return await self._post("zone-remove-speakers",
                                        {"zoneId": zid, "speakerIds": self._resolve_ids("speaker", p["speaker_ids"])})
            case "zone_rename":
                zid = self._resolve("zone", p["zone_id"])
                return await self._post("zones-modify", {"zoneIds": [zid], "rename": p["name"]})
            case "zone_set_order":
                zid = self._resolve("zone", p["zone_id"])
                return await self._post("zones-modify", {"zoneIds": [zid], "order": int(p["order"])})
            case "zone_create":
                return await self._post("zone-create", {"name": p["name"]})
            case "zone_remove":
                zid = self._resolve("zone", p["zone_id"])
                return await self._post("zone-remove", {"zoneId": zid})
            case "zone_announcement_abort":
                zid = self._resolve("zone", p["zone_id"])
                return await self._post("zone-announcement-abort", {"zoneId": zid})
            case "play_announcement":
                zid = self._resolve("zone", p["zone_id"])
                return await self._post("announcement-play-to-zones",
                                        {"announcementId": p["announcement_uuid"], "zoneIds": [zid]})

            # ===================== SPEAKERS ============================ #
            case "speaker_set_volume":
                sid = self._resolve("speaker", p["speaker_id"])
                return await self._post("speakers-modify", {"speakerIds": [sid], "volume": float(p["volume"])})
            case "speaker_set_muted":
                sid = self._resolve("speaker", p["speaker_id"])
                return await self._post("speakers-modify", {"speakerIds": [sid], "muted": bool(p["muted"])})
            case "speaker_set_type":
                sid = self._resolve("speaker", p["speaker_id"])
                return await self._post("speakers-modify", {"speakerIds": [sid], "type": p["type"]})
            case "speaker_set_description":
                sid = self._resolve("speaker", p["speaker_id"])
                return await self._post("speakers-modify", {"speakerIds": [sid], "description": p["description"]})
            case "speaker_set_delay":
                sid = self._resolve("speaker", p["speaker_id"])
                body = {"speakerIds": [sid], "delay": {"time": int(p["time"])}}
                if "bypassed" in p and p["bypassed"] is not None:
                    body["delayBypassed"] = bool(p["bypassed"])
                return await self._post("speakers-modify", body)
            case "speaker_set_limiter":
                sid = self._resolve("speaker", p["speaker_id"])
                return await self._post("speakers-modify", {
                    "speakerIds": [sid],
                    "limiter": {"threshold": float(p["threshold"]), "release": float(p["release"]), "mode": p["mode"]},
                })
            case "speaker_set_limiter_preset":
                sid = self._resolve("speaker", p["speaker_id"])
                body = {"speakerIds": [sid], "presetLimiterId": p["preset_id"]}
                if "bypassed" in p and p["bypassed"] is not None:
                    body["presetLimiterBypassed"] = bool(p["bypassed"])
                return await self._post("speakers-modify", body)
            case "speaker_set_eq_preset":
                sid = self._resolve("speaker", p["speaker_id"])
                body = {"speakerIds": [sid], "presetEqId": p["preset_id"]}
                if "bypassed" in p and p["bypassed"] is not None:
                    body["presetEqBypassed"] = bool(p["bypassed"])
                return await self._post("speakers-modify", body)
            case "speaker_set_eq_bypass":
                sid = self._resolve("speaker", p["speaker_id"])
                return await self._post("speakers-modify", {"speakerIds": [sid], "presetEqBypassed": bool(p["bypassed"])})
            case "speaker_set_zone":
                sid = self._resolve("speaker", p["speaker_id"])
                zid = self._resolve("zone", p["zone_id"])
                return await self._post("speaker-set-zone", {"speakerId": sid, "zoneId": zid})
            case "speaker_remove_zone":
                sid = self._resolve("speaker", p["speaker_id"])
                return await self._post("speaker-remove-zone", {"speakerId": sid})
            case "speaker_set_channel":
                sid = self._resolve("speaker", p["speaker_id"])
                return await self._post("speaker-set-channel",
                                        {"speakerId": sid, "deviceChannel": int(p["device_channel"])})
            case "speaker_remove_channel":
                sid = self._resolve("speaker", p["speaker_id"])
                return await self._post("speaker-remove-channel", {"speakerId": sid})
            case "speaker_play_pink_noise":
                sid = self._resolve("speaker", p["speaker_id"])
                return await self._post("speaker-play-pink-noise", {"speakerId": sid, "play": bool(p["play"])})
            case "speaker_rename":
                sid = self._resolve("speaker", p["speaker_id"])
                return await self._post("speakers-modify", {"speakerIds": [sid], "rename": p["name"]})
            case "speaker_set_order":
                sid = self._resolve("speaker", p["speaker_id"])
                return await self._post("speakers-modify", {"speakerIds": [sid], "order": int(p["order"])})
            case "speaker_create":
                body = {"name": p["name"], "type": p["type"]}
                body.update(self._opt(p, {"description": "description", "volume": "volume", "muted": "muted"}))
                return await self._post("speaker-create", body)
            case "speaker_remove":
                sid = self._resolve("speaker", p["speaker_id"])
                return await self._post("speaker-remove", {"speakerId": sid})

            # ===================== INPUTS ============================== #
            case "input_set_volume":
                iid = self._resolve("input", p["input_id"])
                return await self._post("inputs-modify", {"inputIds": [iid], "volume": float(p["volume"])})
            case "input_set_muted":
                iid = self._resolve("input", p["input_id"])
                return await self._post("inputs-modify", {"inputIds": [iid], "muted": bool(p["muted"])})
            case "input_set_mode":
                iid = self._resolve("input", p["input_id"])
                return await self._post("inputs-modify", {"inputIds": [iid], "mode": p["mode"]})
            case "input_set_channels":
                iid = self._resolve("input", p["input_id"])
                return await self._post("input-set-channels",
                                        {"inputId": iid, "channelsFormat": self._channels_format(p)})
            case "input_remove_channels":
                iid = self._resolve("input", p["input_id"])
                return await self._post("input-remove-channels", {"inputId": iid})
            case "input_set_eq_preset":
                iid = self._resolve("input", p["input_id"])
                body = {"inputIds": [iid], "presetEqId": p["preset_id"]}
                if "bypassed" in p and p["bypassed"] is not None:
                    body["presetEqBypassed"] = bool(p["bypassed"])
                return await self._post("inputs-modify", body)
            case "input_set_gate_preset":
                iid = self._resolve("input", p["input_id"])
                body = {"inputIds": [iid], "presetGateId": p["preset_id"]}
                if "bypassed" in p and p["bypassed"] is not None:
                    body["presetGateBypassed"] = bool(p["bypassed"])
                return await self._post("inputs-modify", body)
            case "input_set_compressor_preset":
                iid = self._resolve("input", p["input_id"])
                body = {"inputIds": [iid], "presetCompressorId": p["preset_id"]}
                if "bypassed" in p and p["bypassed"] is not None:
                    body["presetCompressorBypassed"] = bool(p["bypassed"])
                return await self._post("inputs-modify", body)
            case "input_set_processor_bypass":
                iid = self._resolve("input", p["input_id"])
                bfield = {"eq": "presetEqBypassed", "gate": "presetGateBypassed",
                          "compressor": "presetCompressorBypassed"}[p["processor"]]
                return await self._post("inputs-modify", {"inputIds": [iid], bfield: bool(p["bypassed"])})
            case "input_set_zones":
                iid = self._resolve("input", p["input_id"])
                return await self._post("input-set-zones",
                                        {"inputId": iid, "zoneIds": self._resolve_ids("zone", p["zone_ids"])})
            case "input_add_zones":
                iid = self._resolve("input", p["input_id"])
                return await self._post("input-add-zones",
                                        {"inputId": iid, "zoneIds": self._resolve_ids("zone", p["zone_ids"])})
            case "input_remove_zones":
                iid = self._resolve("input", p["input_id"])
                return await self._post("input-remove-zones",
                                        {"inputId": iid, "zoneIds": self._resolve_ids("zone", p["zone_ids"])})
            case "input_rename":
                iid = self._resolve("input", p["input_id"])
                return await self._post("inputs-modify", {"inputIds": [iid], "rename": p["name"]})
            case "input_set_order":
                iid = self._resolve("input", p["input_id"])
                return await self._post("inputs-modify", {"inputIds": [iid], "order": int(p["order"])})
            case "input_create":
                body = {"name": p["name"], "mode": p["mode"], "muted": bool(p["muted"]),
                        "channelsFormat": self._channels_format(p)}
                body.update(self._opt(p, {"volume": "volume"}))
                return await self._post("input-create", body)
            case "input_remove":
                iid = self._resolve("input", p["input_id"])
                return await self._post("input-remove", {"inputId": iid})

            # ===================== DSO ================================= #
            case "dso_set_enabled":
                return await self._post("dso-modify", {"enabled": bool(p["enabled"])})
            case "dso_set_volume":
                return await self._post("dso-modify", {"volume": float(p["volume"])})
            case "dso_set_muted":
                return await self._post("dso-modify", {"muted": bool(p["muted"])})
            case "dso_set_channel":
                return await self._post("dso-modify", {"channelsFormat": {"mono": {"channel": int(p["channel"])}}})
            case "dso_remove_channel":
                return await self._post("dso-modify", {"channelsFormat": {}})
            case "dso_set_eq_preset":
                body = {"presetEqId": p["preset_id"]}
                if "bypassed" in p and p["bypassed"] is not None:
                    body["presetEqBypassed"] = bool(p["bypassed"])
                return await self._post("dso-modify", body)
            case "dso_set_gate_preset":
                body = {"presetGateId": p["preset_id"]}
                if "bypassed" in p and p["bypassed"] is not None:
                    body["presetGateBypassed"] = bool(p["bypassed"])
                return await self._post("dso-modify", body)
            case "dso_set_compressor_preset":
                body = {"presetCompressorId": p["preset_id"]}
                if "bypassed" in p and p["bypassed"] is not None:
                    body["presetCompressorBypassed"] = bool(p["bypassed"])
                return await self._post("dso-modify", body)

            # ===================== PLAYLISTS =========================== #
            case "playlist_create":
                return await self._post("playlist-create", {"name": p["name"]})
            case "playlist_remove":
                return await self._post("playlist-remove", {"playlistId": p["playlist_id"]})
            case "playlist_rename":
                return await self._post("playlist-modify", {"playlistId": p["playlist_id"], "rename": p["name"]})
            case "playlist_set_order":
                return await self._post("playlist-modify", {"playlistId": p["playlist_id"], "order": int(p["order"])})
            case "playlist_set_players":
                return await self._post("playlist-set-players",
                                        {"playlistId": p["playlist_id"],
                                         "playerIds": self._resolve_ids("player", p["player_ids"])})
            case "playlist_add_item":
                return await self._post("playlist-add-item", {"playlistId": p["playlist_id"], "path": p["path"]})
            case "playlist_remove_item":
                positions = [int(x.strip()) for x in str(p["positions"]).split(",") if x.strip() != ""]
                return await self._post("playlist-remove-item",
                                        {"playlistId": p["playlist_id"], "position": positions})
            case "playlist_move_item":
                return await self._post("playlist-move-item",
                                        {"playlistId": p["playlist_id"],
                                         "source": int(p["source"]), "destination": int(p["destination"])})
            case "playlist_scan_folder":
                return await self._post("playlist-scan-folder",
                                        {"playlistId": p["playlist_id"], "position": int(p["position"])})
            case "playlist_set_folder_autoscan":
                return await self._post("playlist-set-folder-autoscan",
                                        {"playlistId": p["playlist_id"], "position": int(p["position"]),
                                         "autoscan": bool(p["autoscan"])})

            # ===================== ANNOUNCEMENTS ======================= #
            case "announcement_create":
                body = {"name": p["name"], "path": p["path"]}
                body.update(self._opt(p, {
                    "ducking_volume": "duckingVolume", "volume": "volume",
                    "attack": "attack", "release": "release", "priority": "priority",
                }))
                return await self._post("announcement-create", body)
            case "announcement_modify":
                body = {"announcementId": p["announcement_id"]}
                body.update(self._opt(p, {
                    "name": "rename", "order": "order", "path": "path",
                    "ducking_volume": "duckingVolume", "volume": "volume",
                    "attack": "attack", "release": "release", "priority": "priority",
                }))
                return await self._post("announcements-modify", body)
            case "announcement_remove":
                return await self._post("announcement-remove", {"announcementId": p["announcement_id"]})
            case "announcement_play_to_zones":
                return await self._post("announcement-play-to-zones",
                                        {"announcementId": p["announcement_id"],
                                         "zoneIds": self._resolve_ids("zone", p["zone_ids"])})
            case "announcements_abort":
                ids = [x.strip() for x in str(p["announcement_ids"]).split(",") if x.strip() != ""]
                return await self._post("announcements-abort", {"announcementIds": ids})

            # ===================== PRESETS ============================= #
            case "preset_create":
                return await self._post(f"preset-{p['kind']}-create",
                                        {"name": p["name"], "preset": json.loads(p["preset_json"])})
            case "preset_modify":
                body = {f"preset{p['kind'].capitalize()}Id": p["preset_id"]}
                if p.get("rename"):
                    body["rename"] = p["rename"]
                if p.get("preset_json"):
                    body["preset"] = json.loads(p["preset_json"])
                return await self._post(f"preset-{p['kind']}-modify", body)
            case "preset_remove":
                return await self._post(f"preset-{p['kind']}-remove",
                                        {f"preset{p['kind'].capitalize()}Id": p["preset_id"]})
            case "preset_get_all":
                return await self._post(f"preset-{p['kind']}-get-status-all", {})

            # ===================== ACTIONS ============================= #
            case "action_create":
                body = {"name": p["name"], "sequence": json.loads(p["sequence_json"])}
                if p.get("description"):
                    body["description"] = p["description"]
                if p.get("trigger_code"):
                    body["triggerCode"] = p["trigger_code"]
                if p.get("action_group_names"):
                    body["actionGroupNames"] = [x.strip() for x in str(p["action_group_names"]).split(",") if x.strip()]
                return await self._post("action-create", body)
            case "action_modify":
                body = {"actionId": p["action_id"]}
                if p.get("rename"):
                    body["rename"] = p["rename"]
                if "order" in p and p["order"] is not None:
                    body["order"] = int(p["order"])
                if p.get("sequence_json"):
                    body["sequence"] = json.loads(p["sequence_json"])
                if p.get("description"):
                    body["description"] = p["description"]
                if p.get("trigger_code"):
                    body["triggerCode"] = p["trigger_code"]
                if p.get("action_group_names"):
                    body["actionGroupNames"] = [x.strip() for x in str(p["action_group_names"]).split(",") if x.strip()]
                return await self._post("action-modify", body)
            case "action_play":
                ids = [x.strip() for x in str(p["action_ids"]).split(",") if x.strip() != ""]
                return await self._post("action-play", {"actionIds": ids})
            case "action_stop":
                return await self._post("action-stop", {"actionId": p["action_id"]})
            case "action_remove":
                return await self._post("action-remove", {"actionId": p["action_id"]})
            case "action_get_all":
                return await self._post("action-get-status-all", {})
            case "action_group_create":
                body = {"name": p["name"]}
                if p.get("action_ids"):
                    body["actionIds"] = [x.strip() for x in str(p["action_ids"]).split(",") if x.strip()]
                return await self._post("action-group-create", body)
            case "action_group_modify":
                body = {"actionGroupId": p["action_group_id"]}
                if p.get("name"):
                    body["name"] = p["name"]
                if "order" in p and p["order"] is not None:
                    body["order"] = int(p["order"])
                if p.get("action_ids"):
                    body["actionIds"] = [x.strip() for x in str(p["action_ids"]).split(",") if x.strip()]
                return await self._post("action-group-modify", body)
            case "action_group_remove":
                return await self._post("action-group-remove", {"actionGroupId": p["action_group_id"]})
            case "action_group_get_all":
                return await self._post("action-group-get-status-all", {})

            # ===================== EXTERNAL ============================ #
            case "external_send_tcp":
                return await self._post("external-send-tcp-message", self._external_body(p))
            case "external_send_udp":
                return await self._post("external-send-udp-message", self._external_body(p))
            case "external_send_tcp_hex":
                return await self._post("external-send-tcp-hex", self._external_body(p))
            case "external_send_udp_hex":
                return await self._post("external-send-udp-hex", self._external_body(p))
            case "external_send_http":
                body = {"targetUrl": p["target_url"], "method": p["method"]}
                body.update(self._opt(p, {"body": "body", "timeout_ms": "timeoutMs"}))
                return await self._post("external-send-http-request", body)
            case "external_modbus_read":
                ep = f"external-modbus-read-{p['kind']}"
                body = {"targetUrl": p["target_url"], "uid": int(p["uid"]),
                        "address": int(p["address"]), "quantity": int(p["quantity"])}
                body.update(self._opt(p, {"port": "port", "timeout_ms": "timeoutMs"}))
                return await self._post(ep, body)
            case "external_modbus_write_single":
                ep = f"external-modbus-write-single-{p['kind']}"
                raw = p["value"]
                value = (str(raw).strip().lower() in ("true", "1", "yes")) if p["kind"] == "coil" else int(raw)
                body = {"targetUrl": p["target_url"], "uid": int(p["uid"]),
                        "address": int(p["address"]), "value": value}
                body.update(self._opt(p, {"port": "port", "timeout_ms": "timeoutMs"}))
                return await self._post(ep, body)
            case "external_modbus_write_multiple":
                ep = f"external-modbus-write-multiple-{p['kind']}"
                body = {"targetUrl": p["target_url"], "uid": int(p["uid"]),
                        "address": int(p["address"]), "values": json.loads(p["values_json"])}
                body.update(self._opt(p, {"port": "port", "timeout_ms": "timeoutMs"}))
                return await self._post(ep, body)

            # ===================== PROJECTS ============================ #
            case "project_new":
                return await self._post("project-new", {"projectName": p["project_name"]})
            case "project_load":
                return await self._post("project-load", {"projectName": p["project_name"]})
            case "project_save":
                body = {}
                if p.get("project_name"):
                    body["projectName"] = p["project_name"]
                return await self._post("project-save", body)
            case "project_remove":
                return await self._post("project-remove", {"projectName": p["project_name"]})
            case "project_get_all":
                return await self._post("project-get-status-all", {}, method="GET")

            # ===================== SYSTEM ============================== #
            case "system_get_status":
                return await self._post("system-get-status-all", {}, method="GET")
            case "system_modify":
                return await self._post("system-modify", json.loads(p["settings_json"]), method="PUT")
            case "system_set_timezone":
                return await self._post("system-set-timezone", {"timezone": p["timezone"]})
            case "system_get_timezones":
                return await self._post("system-get-timezones", {}, method="GET")
            case "system_channels_limit":
                return await self._post("channels-limit", {})
            case "system_restart":
                return await self._post("restart", {})

            # ===================== NETWORK ============================= #
            case "network_get":
                return await self._post("network-config", {}, method="GET")
            case "network_set":
                return await self._post("network-config", json.loads(p["config_json"]))
            case "network_validate":
                return await self._post("network-validate", {})
            case "network_revert":
                return await self._post("network-revert", {})

            # ===================== SOFTWARE UPDATE ===================== #
            case "software_update_check":
                return await self._post("software-update-check-update", {})
            case "software_update_download":
                return await self._post("software-update-download-update", {})
            case "software_update_remove":
                return await self._post("software-update-remove-downloaded-update", {})
            case "software_update_install":
                return await self._post("software-update-install-update", {})

            # ===================== LOGGER ============================== #
            case "logger_get":
                qs = {"page": int(p["page"]), "size": int(p["size"])}
                qs.update(self._opt(p, {"origin_name": "originName", "action": "action", "sort": "sort"}))
                return await self._post("logger-get-users-paged-filtered", qs, method="GET")

            # ===================== USERS =============================== #
            case "users_get":
                return await self._post("usersmanager-get-users", {}, method="GET")
            case "users_get_active":
                return await self._post("usersmanager-get-active-users", {}, method="GET")
            case "users_create":
                body = {"name": p["name"], "email": p["email"], "userType": p["user_type"]}
                if "auto_logout" in p and p["auto_logout"] is not None:
                    body["autoLogout"] = bool(p["auto_logout"])
                return await self._post("usersmanager-create", body)
            case "users_delete":
                return await self._post("usersmanager-delete", {"userName": p["user_name"]})
            case "users_generate_token":
                return await self._post("usersmanager-generate-token", {"userName": p["user_name"]})
            case "users_create_api_key":
                return await self._post("usersmanager-create-api-key", {"userName": p["user_name"]})
            case "users_remove_api_key":
                return await self._post("usersmanager-remove-api-key", {"userName": p["user_name"]})
            case "users_logout":
                return await self._post("usersmanager-logout", {"userName": p["user_name"]})

            # ===================== FILE MANAGER ======================== #
            case "file_list":
                qs = self._opt(p, {"path": "path", "search": "search", "recursive": "recursive"})
                return await self._post("filemanager-list-folder", qs, method="GET")
            case "file_disk_space":
                return await self._post("filemanager-get-disk-space", {}, method="GET")
            case "file_create_folder":
                return await self._post("filemanager-create-folder", {"path": p["path"]})
            case "file_copy":
                return await self._post("filemanager-copy-file-folder",
                                        {"pathFrom": p["path_from"], "pathTo": p["path_to"]})
            case "file_move":
                sources = [x.strip() for x in str(p["path_from"]).split(",") if x.strip() != ""]
                body = {"pathTo": p["path_to"], "pathFrom": sources}
                if "force" in p and p["force"] is not None:
                    body["force"] = bool(p["force"])
                return await self._post("filemanager-move-file-folder", body)
            case "file_rename":
                return await self._post("filemanager-rename-file-folder",
                                        {"pathFrom": p["path_from"], "pathTo": p["path_to"]}, method="PUT")
            case "file_remove":
                paths = [x.strip() for x in str(p["path"]).split(",") if x.strip() != ""]
                body = {"path": paths}
                if "force" in p and p["force"] is not None:
                    body["force"] = bool(p["force"])
                return await self._post("filemanager-remove-file-folder", body, method="DELETE")

            # ===================== GENERIC ============================= #
            case "api_call":
                endpoint = p["endpoint"].lstrip("/")
                body = json.loads(p["params_json"]) if p.get("params_json") else {}
                method = p.get("method", "POST")
                return await self._post(endpoint, body, method=method)

            case _:
                raise ValueError(f"[{self.device_id}] Unknown command: {command}")

    def _external_body(self, p: dict[str, Any]) -> dict[str, Any]:
        body = {"targetUrl": p["target_url"], "body": p["body"]}
        if "timeout_ms" in p and p["timeout_ms"] is not None:
            body["timeoutMs"] = int(p["timeout_ms"])
        return body

    # ------------------------------------------------------------------ #
    # Watchdog
    # ------------------------------------------------------------------ #
    async def poll(self) -> None:
        """Liveness watchdog. The control WebSocket is the single source of
        truth for both state and reachability, and it shares the one session
        with REST commands. Issuing an HTTP poll here would race the WS for the
        session (a re-login closes the prior session and would kill the WS), so
        the watchdog only checks whether the WS receiver task is still alive.
        Transport loss shows up as the task finishing; we surface it so the
        platform reconnects (and re-logs in)."""
        if self._ws_task is None or self._ws_task.done():
            raise ConnectionError(f"[{self.device_id}] WebSocket task is not running")

    # Optional: power the IDE "Refresh from Device" button by re-pulling
    # every collection over REST (independent of the WS).
    async def refresh_children(self) -> dict:
        counts: dict[str, int] = {}
        for ctype, snap_key, _ in _COLLECTIONS:
            endpoint = _REFRESH_ENDPOINT.get(ctype)
            if endpoint is None:
                # No REST get-all (e.g. controllers) — WS snapshot only.
                continue
            # All *-get-status-all endpoints accept POST (verified against the
            # core types during bring-up); keep them uniform.
            body = await self._post(f"{endpoint}-get-status-all", {})
            collection = body.get(snap_key, {})
            self._apply_collection(ctype, collection, snapshot=True)
            counts[snap_key] = len(collection)
        # DSO singleton.
        try:
            dso_body = await self._post("dso-get-status", {}, method="GET")
            if isinstance(dso_body.get("dso"), dict):
                self._apply_dso(dso_body["dso"], snapshot=True)
        except Exception as exc:
            log.debug(f"[{self.device_id}] DSO refresh skipped: {exc}")
        return counts
