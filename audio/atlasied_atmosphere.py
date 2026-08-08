"""
OpenAVC AtlasIED Atmosphere Driver.

Controls AtlasIED Atmosphere AZM4 / AZM8 audio processing and control
systems over the third-party JSON-RPC 2.0 protocol on TCP port 5321.

Protocol summary:
    - Newline-delimited JSON-RPC 2.0 over TCP. Methods sent to the device:
      "set", "bmp" (bump), "sub" (subscribe), "unsub" (unsubscribe), "get".
      Methods sent back to the client: "update", "getResp", "error".
    - Parameter names are dynamically assigned during device configuration
      (e.g., "ZoneGain_0", "SourceMute_3"). There is no enumeration API —
      the client subscribes to indices 0..N-1 and the device silently
      ignores out-of-range subscribes — so the rosters are sized from the
      driver config and registered as child entities per type: sources,
      zones, mixes, groups, messages, routines, scenes, GPO presets, GPOs,
      and bell schedules. Entity names arrive over the subscription and
      become the child labels.
    - Names, ZoneGrouped, and GpoState are read-only for third-party
      clients (ATS006993 §6 parameter table). GPOs are driven through GPO
      presets (RecallGpoPreset), not by setting GpoState directly.
    - Inactivity drops the TCP connection after 5 minutes. The BaseDriver
      liveness watchdog probes `get KeepAlive` (every HEALTH_INTERVAL_S,
      default 30 s) and awaits the getResp — the probe both holds the
      connection open and detects a link that died without a FIN, forcing
      a reconnect with a typed no_response fault after consecutive misses.
    - Meters (SourceMeter / ZoneMeter / MixMeter / GroupMeter) come over
      UDP port 3131 and are not exposed by this driver — most integration
      use cases don't need realtime level metering, and the TCP control
      surface is fully usable without it.

Python driver because the protocol is push-subscription JSON-RPC whose
per-param updates fan out into config-sized child rosters, and the
liveness probe correlates KeepAlive replies by param name — none of which
the YAML runtime expresses.

Source: https://www.atlasied.com/ATS006993-B-AZM4-AZM8-3rd-Party-Control.pdf
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from openavc.drivers.base import BaseDriver
from openavc.utils.logger import get_logger

log = get_logger(__name__)


# The device closes any control connection that stays silent for 5 minutes
# (ATS006993 §2: "keep-alive message at least once every 5 minutes"). The
# liveness probe (`get KeepAlive`, awaited) doubles as that keep-alive; the
# BaseDriver default HEALTH_INTERVAL_S of 30 s is comfortably under the cap
# and detects a silently dead link within a minute.

# Default entity counts, sized for an AZM8. The driver subscribes to
# {Source,Zone,Mix,Group}_0..N-1 — the device drops subscribes for indices
# that aren't configured, so over-sizing is harmless.
DEFAULT_NUM_SOURCES = 8
DEFAULT_NUM_ZONES = 8
DEFAULT_NUM_MIXES = 8
DEFAULT_NUM_GROUPS = 4
DEFAULT_NUM_MESSAGES = 8
DEFAULT_NUM_ROUTINES = 8
DEFAULT_NUM_SCENES = 8
DEFAULT_NUM_GPO_PRESETS = 8
DEFAULT_NUM_GPOS = 4
DEFAULT_NUM_BELL_SCHEDULES = 4


# ── Child entity types ───────────────────────────────────────────────────────
#
# Mutes relay at high cloud priority (a muted paging zone is an incident),
# as do GPO pin states (they drive strobes / door releases); continuous
# gains and cosmetic names are low. Indices are zero-based to match the
# device's own Third Party Control Message Table (SourceMute_2 = index 2).

_ID_FORMAT: dict[str, Any] = {"type": "integer", "min": 0, "pad_width": 2}


def _gain_prop() -> dict[str, Any]:
    return {"type": "number", "label": "Gain (dB)", "min": -80, "max": 0,
            "unit": "dB", "cloud_priority": "low", "control": True}


def _mute_prop() -> dict[str, Any]:
    return {"type": "boolean", "label": "Mute", "cloud_priority": "high",
            "control": True}


def _name_prop() -> dict[str, Any]:
    # Names are assigned in the Atmosphere web UI and are read-only to
    # third-party clients (ATS006993 §4) — no control flag, no rename.
    return {"type": "string", "label": "Name", "cloud_priority": "low"}


def _named_roster_type(label: str, plural: str) -> dict[str, Any]:
    return {
        "label": label,
        "label_plural": plural,
        "id_format": dict(_ID_FORMAT),
        "state_variables": {"name": _name_prop()},
        "summary_fields": ["name"],
        "label_field": "name",
    }


def _build_child_types() -> dict[str, dict[str, Any]]:
    return {
        "source": {
            "label": "Source",
            "label_plural": "Sources",
            "id_format": dict(_ID_FORMAT),
            "state_variables": {
                "name": _name_prop(),
                "gain": _gain_prop(),
                "mute": _mute_prop(),
            },
            "summary_fields": ["mute", "gain"],
            "label_field": "name",
        },
        "zone": {
            "label": "Zone",
            "label_plural": "Zones",
            "id_format": dict(_ID_FORMAT),
            "state_variables": {
                "name": _name_prop(),
                "gain": _gain_prop(),
                "mute": _mute_prop(),
                "source": {"type": "integer", "label": "Selected Source",
                           "min": -1, "cloud_priority": "low",
                           "control": True},
                "grouped": {"type": "boolean", "label": "Grouped",
                            "cloud_priority": "low"},
            },
            "summary_fields": ["mute", "gain", "source"],
            "label_field": "name",
        },
        "mix": {
            "label": "Mix",
            "label_plural": "Mixes",
            "id_format": dict(_ID_FORMAT),
            "state_variables": {
                "name": _name_prop(),
                "gain": _gain_prop(),
                "mute": _mute_prop(),
            },
            "summary_fields": ["mute", "gain"],
            "label_field": "name",
        },
        "group": {
            "label": "Group",
            "label_plural": "Groups",
            "id_format": dict(_ID_FORMAT),
            "state_variables": {
                "name": _name_prop(),
                "gain": _gain_prop(),
                "mute": _mute_prop(),
                "source": {"type": "integer", "label": "Selected Source",
                           "min": -1, "cloud_priority": "low",
                           "control": True},
                "active": {"type": "boolean", "label": "Active (Combined)",
                           "cloud_priority": "high", "control": True},
            },
            "summary_fields": ["mute", "gain", "active"],
            "label_field": "name",
        },
        "message": _named_roster_type("Message", "Messages"),
        "routine": _named_roster_type("Routine", "Routines"),
        "scene": _named_roster_type("Scene", "Scenes"),
        "gpo_preset": _named_roster_type("GPO Preset", "GPO Presets"),
        "gpo": {
            # GpoState is read-only over third-party control (ATS006993
            # §6) — pins are driven by recalling GPO presets.
            "label": "GPO",
            "label_plural": "GPOs",
            "id_format": dict(_ID_FORMAT),
            "state_variables": {
                "state": {"type": "boolean", "label": "State",
                          "cloud_priority": "high"},
            },
            "summary_fields": ["state"],
        },
        "bell_schedule": _named_roster_type("Bell Schedule", "Bell Schedules"),
    }


CHILD_ENTITY_TYPES: dict[str, dict[str, Any]] = _build_child_types()

# Roster sizing: (child_type, config_key, default, placeholder label prefix).
_ROSTERS: list[tuple[str, str, int, str]] = [
    ("source", "num_sources", DEFAULT_NUM_SOURCES, "Source"),
    ("zone", "num_zones", DEFAULT_NUM_ZONES, "Zone"),
    ("mix", "num_mixes", DEFAULT_NUM_MIXES, "Mix"),
    ("group", "num_groups", DEFAULT_NUM_GROUPS, "Group"),
    ("message", "num_messages", DEFAULT_NUM_MESSAGES, "Message"),
    ("routine", "num_routines", DEFAULT_NUM_ROUTINES, "Routine"),
    ("scene", "num_scenes", DEFAULT_NUM_SCENES, "Scene"),
    ("gpo_preset", "num_gpo_presets", DEFAULT_NUM_GPO_PRESETS, "GPO Preset"),
    ("gpo", "num_gpos", DEFAULT_NUM_GPOS, "GPO"),
    ("bell_schedule", "num_bell_schedules", DEFAULT_NUM_BELL_SCHEDULES,
     "Bell Schedule"),
]

# Wire params subscribed per child type: (param prefix, fmt). Names first
# so child labels populate before the numeric sweep.
_TYPE_SUBS: dict[str, list[tuple[str, str]]] = {
    "source": [("SourceName", "str"), ("SourceGain", "val"),
               ("SourceMute", "val")],
    "zone": [("ZoneName", "str"), ("ZoneGain", "val"), ("ZoneMute", "val"),
             ("ZoneSource", "val"), ("ZoneGrouped", "val")],
    "mix": [("MixName", "str"), ("MixGain", "val"), ("MixMute", "val")],
    "group": [("GroupName", "str"), ("GroupGain", "val"),
              ("GroupMute", "val"), ("GroupSource", "val"),
              ("GroupActive", "val")],
    "message": [("MessageName", "str")],
    "routine": [("RoutineName", "str")],
    "scene": [("SceneName", "str")],
    "gpo_preset": [("GpoPresetName", "str")],
    "gpo": [("GpoState", "val")],
    "bell_schedule": [("BellScheduleName", "str")],
}

# Incoming wire param prefix -> (child type, child prop, converter).
_PARAM_MAP: dict[str, tuple[str, str, str]] = {
    "SourceGain": ("source", "gain", "float"),
    "SourceMute": ("source", "mute", "bool"),
    "SourceName": ("source", "name", "str"),
    "ZoneGain": ("zone", "gain", "float"),
    "ZoneMute": ("zone", "mute", "bool"),
    "ZoneName": ("zone", "name", "str"),
    "ZoneSource": ("zone", "source", "int"),
    "ZoneGrouped": ("zone", "grouped", "bool"),
    "MixGain": ("mix", "gain", "float"),
    "MixMute": ("mix", "mute", "bool"),
    "MixName": ("mix", "name", "str"),
    "GroupGain": ("group", "gain", "float"),
    "GroupMute": ("group", "mute", "bool"),
    "GroupName": ("group", "name", "str"),
    "GroupSource": ("group", "source", "int"),
    "GroupActive": ("group", "active", "bool"),
    "MessageName": ("message", "name", "str"),
    "RoutineName": ("routine", "name", "str"),
    "SceneName": ("scene", "name", "str"),
    "GpoPresetName": ("gpo_preset", "name", "str"),
    "GpoState": ("gpo", "state", "bool"),
    "BellScheduleName": ("bell_schedule", "name", "str"),
}


def _child_param(ctype: str, label: str | None = None) -> dict[str, Any]:
    return {"type": "child_id", "child_type": ctype, "required": True,
            "label": label or CHILD_ENTITY_TYPES[ctype]["label"]}


class AtlasIEDAtmosphereDriver(BaseDriver):
    """AtlasIED Atmosphere AZM4/AZM8 driver."""

    DRIVER_INFO = {
        "id": "atlasied_atmosphere",
        "name": "AtlasIED Atmosphere",
        "manufacturer": "AtlasIED",
        "category": "audio",
        "version": "2.0.2",
        # The connection lifecycle hooks this driver overrides landed in 0.24.0.
        "min_platform_version": "0.25.0",
        "author": "OpenAVC",
        "description": (
            "Controls AtlasIED Atmosphere AZM4 and AZM8 audio processing "
            "and control systems over the third-party JSON-RPC protocol "
            "on TCP port 5321. Models sources, zones, mixes, groups, "
            "messages, routines, scenes, GPO presets, GPOs, and bell "
            "schedules as child entities labelled with the names from the "
            "Atmosphere configuration. Covers gain and mute per source / "
            "zone / mix / group, zone and group source select, group "
            "combine, message / routine / scene / GPO-preset triggers, "
            "GPO pin state, and today's bell schedule. Subscribes for "
            "state updates on connect."
        ),
        "source_url": "https://www.atlasied.com/ATS006993-B-AZM4-AZM8-3rd-Party-Control.pdf",
        "tags": ["atmosphere", "azm4", "azm8", "paging", "install-amp", "json-rpc"],
        "verified": False,
        "simulated": True,
        "protocols": ["atlasied-atmosphere-thirdparty"],
        "ports": [5321],
        "transport": "tcp",
        "discovery": {
            # AZM4 / AZM8 do not advertise via mDNS, SSDP, or SNMP. The
            # AtlasIED 3rd-Party Control PDF and AtlasIED Network Protocols
            # & Ports doc both list only the JSON-RPC control surface
            # (TCP 5321) and the meter subscription channel (UDP 3131).
            # No Atlas-registered IEEE OUI surfaces in public registries.
            # Match by the JSON-RPC port; expect a `possible (candidate:
            # atlasied_atmosphere)` outcome when a host listens there.
            #   Source: https://www.atlasied.com/ATS006993-B-AZM4-AZM8-3rd-Party-Control.pdf
            "port_open": [5321],
            "manufacturer_alias": ["atlasied", "atlas ied", "atmosphere"],
        },
        "compatible_models": [
            {
                "manufacturer": "AtlasIED",
                "models": [
                    "AZM4",
                    "AZM8",
                    "AZMP4",
                    "AZMP8",
                ],
                "confidence": "untested",
                "notes": (
                    "AZM4 / AZM8 are the standalone DSPs; AZMP4 / AZMP8 add "
                    "internal amplification but share the same third-party "
                    "control surface."
                ),
            }
        ],
        "help": {
            "overview": (
                "AtlasIED Atmosphere is a configurable audio DSP / mixer / "
                "paging system widely deployed in restaurants, retail, "
                "fitness, and house-of-worship installs. This driver speaks "
                "the third-party JSON-RPC protocol on TCP port 5321. It "
                "subscribes to entity state on connect, so updates push "
                "live as users change settings on the front panel or in "
                "the web UI.\n\n"
                "Sources, zones, mixes, groups, messages, routines, "
                "scenes, GPO presets, GPOs, and bell schedules appear as "
                "child entities, labelled with the names assigned during "
                "device configuration (via the Atmosphere web UI). "
                "Indices are zero-based, matching the device's Settings → "
                "Third Party Control → Message Table.\n\n"
                "Entity names and GPO pin states are read-only over "
                "third-party control — rename entities in the Atmosphere "
                "web UI, and drive GPOs by recalling GPO presets."
            ),
            "setup": (
                "1. In the Atmosphere web UI, go to Settings → Third Party "
                "Control → General and toggle 'Enable' on for the "
                "third-party control API.\n"
                "2. Confirm the Atmosphere has a static IP (or DHCP "
                "reservation).\n"
                "3. In OpenAVC, enter the IP. Port 5321 is fixed.\n"
                "4. Set the entity counts (zones, sources, mixes, groups) "
                "to match the device configuration. Counts default to "
                "AZM8 sizing; over-sizing is harmless but bigger than "
                "needed clutters the child entity lists.\n"
                "5. After connecting, confirm the source / zone / mix / "
                "group names from the Atmosphere config show up as the "
                "child entity labels."
            ),
        },
        "default_config": {
            "host": "",
            "port": 5321,
            "num_sources": DEFAULT_NUM_SOURCES,
            "num_zones": DEFAULT_NUM_ZONES,
            "num_mixes": DEFAULT_NUM_MIXES,
            "num_groups": DEFAULT_NUM_GROUPS,
            "num_messages": DEFAULT_NUM_MESSAGES,
            "num_routines": DEFAULT_NUM_ROUTINES,
            "num_scenes": DEFAULT_NUM_SCENES,
            "num_gpo_presets": DEFAULT_NUM_GPO_PRESETS,
            "num_gpos": DEFAULT_NUM_GPOS,
            "num_bell_schedules": DEFAULT_NUM_BELL_SCHEDULES,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
            },
            "port": {
                "type": "integer",
                "default": 5321,
                "label": "TCP Port",
                "description": "Atmosphere third-party control port — always 5321.",
            },
            "num_sources": {
                "type": "integer",
                "default": DEFAULT_NUM_SOURCES,
                "min": 0,
                "label": "Source Count",
                "description": (
                    "Number of audio sources defined in the Atmosphere "
                    "configuration. Default is 8."
                ),
            },
            "num_zones": {
                "type": "integer",
                "default": DEFAULT_NUM_ZONES,
                "min": 0,
                "label": "Zone Count",
                "description": (
                    "Number of zones — 4 for AZM4, 8 for AZM8."
                ),
            },
            "num_mixes": {
                "type": "integer",
                "default": DEFAULT_NUM_MIXES,
                "min": 0,
                "label": "Mix Count",
            },
            "num_groups": {
                "type": "integer",
                "default": DEFAULT_NUM_GROUPS,
                "min": 0,
                "label": "Group Count",
            },
            "num_messages": {
                "type": "integer",
                "default": DEFAULT_NUM_MESSAGES,
                "min": 0,
                "label": "Stored Message Count",
            },
            "num_routines": {
                "type": "integer",
                "default": DEFAULT_NUM_ROUTINES,
                "min": 0,
                "label": "Routine Count",
            },
            "num_scenes": {
                "type": "integer",
                "default": DEFAULT_NUM_SCENES,
                "min": 0,
                "label": "Scene Count",
            },
            "num_gpo_presets": {
                "type": "integer",
                "default": DEFAULT_NUM_GPO_PRESETS,
                "min": 0,
                "label": "GPO Preset Count",
            },
            "num_gpos": {
                "type": "integer",
                "default": DEFAULT_NUM_GPOS,
                "min": 0,
                "label": "GPO Count",
            },
            "num_bell_schedules": {
                "type": "integer",
                "default": DEFAULT_NUM_BELL_SCHEDULES,
                "min": 0,
                "label": "Bell Schedule Count",
            },
        },
        "state_variables": {
            "firmware_version": {"type": "string", "label": "Firmware Version"},
            "todays_bell_schedule": {
                "type": "integer",
                "label": "Today's Bell Schedule Index",
            },
        },
        "child_entity_types": CHILD_ENTITY_TYPES,
        "commands": {
            "set_source_gain": {
                "label": "Set Source Gain",
                "params": {
                    "source": _child_param("source"),
                    "gain_db": {
                        "type": "number",
                        "required": True,
                        "min": -80,
                        "max": 0,
                        "help": "Gain in dB (-80 to 0).",
                    },
                },
                "help": "Set the gain on a source.",
            },
            "set_source_mute": {
                "label": "Set Source Mute",
                "params": {
                    "source": _child_param("source"),
                    "mute": {"type": "boolean", "required": True},
                },
            },
            "bump_source_gain": {
                "label": "Bump Source Gain",
                "params": {
                    "source": _child_param("source"),
                    "delta_db": {
                        "type": "number",
                        "required": True,
                        "help": "Gain change in dB (positive or negative).",
                    },
                },
                "help": "Adjust source gain by a relative amount.",
            },
            "set_zone_gain": {
                "label": "Set Zone Gain",
                "params": {
                    "zone": _child_param("zone"),
                    "gain_db": {
                        "type": "number",
                        "required": True,
                        "min": -80,
                        "max": 0,
                    },
                },
            },
            "set_zone_mute": {
                "label": "Set Zone Mute",
                "params": {
                    "zone": _child_param("zone"),
                    "mute": {"type": "boolean", "required": True},
                },
            },
            "bump_zone_gain": {
                "label": "Bump Zone Gain",
                "params": {
                    "zone": _child_param("zone"),
                    "delta_db": {"type": "number", "required": True},
                },
            },
            "set_zone_source": {
                "label": "Select Zone Source",
                "params": {
                    "zone": _child_param("zone"),
                    "source": _child_param("source"),
                },
                "help": (
                    "Select which source feeds a zone. Use Clear Zone "
                    "Source to select none."
                ),
            },
            "clear_zone_source": {
                "label": "Clear Zone Source",
                "params": {
                    "zone": _child_param("zone"),
                },
                "help": "Select no source for a zone (silence it).",
            },
            "set_mix_gain": {
                "label": "Set Mix Gain",
                "params": {
                    "mix": _child_param("mix"),
                    "gain_db": {
                        "type": "number",
                        "required": True,
                        "min": -80,
                        "max": 0,
                    },
                },
            },
            "set_mix_mute": {
                "label": "Set Mix Mute",
                "params": {
                    "mix": _child_param("mix"),
                    "mute": {"type": "boolean", "required": True},
                },
            },
            "bump_mix_gain": {
                "label": "Bump Mix Gain",
                "params": {
                    "mix": _child_param("mix"),
                    "delta_db": {"type": "number", "required": True},
                },
            },
            "set_group_gain": {
                "label": "Set Group Gain",
                "params": {
                    "group": _child_param("group"),
                    "gain_db": {
                        "type": "number",
                        "required": True,
                        "min": -80,
                        "max": 0,
                    },
                },
            },
            "set_group_mute": {
                "label": "Set Group Mute",
                "params": {
                    "group": _child_param("group"),
                    "mute": {"type": "boolean", "required": True},
                },
            },
            "bump_group_gain": {
                "label": "Bump Group Gain",
                "params": {
                    "group": _child_param("group"),
                    "delta_db": {"type": "number", "required": True},
                },
            },
            "set_group_source": {
                "label": "Select Group Source",
                "params": {
                    "group": _child_param("group"),
                    "source": _child_param("source"),
                },
                "help": (
                    "Select which source feeds a group. Use Clear Group "
                    "Source to select none."
                ),
            },
            "clear_group_source": {
                "label": "Clear Group Source",
                "params": {
                    "group": _child_param("group"),
                },
                "help": "Select no source for a group.",
            },
            "set_group_active": {
                "label": "Combine / Uncombine Group",
                "params": {
                    "group": _child_param("group"),
                    "active": {
                        "type": "boolean",
                        "required": True,
                        "help": "True = combine zones, False = uncombine.",
                    },
                },
                "help": (
                    "Activate (combine) or deactivate (uncombine) the zones "
                    "in a group, matching the Combine button in the Zones "
                    "tab of the web UI."
                ),
            },
            "play_message": {
                "label": "Play Message",
                "params": {
                    "message": _child_param("message"),
                },
                "help": "Trigger playback of a stored message.",
            },
            "recall_routine": {
                "label": "Recall Routine",
                "params": {
                    "routine": _child_param("routine"),
                },
            },
            "recall_scene": {
                "label": "Recall Scene",
                "params": {
                    "scene": _child_param("scene"),
                },
            },
            "recall_gpo_preset": {
                "label": "Recall GPO Preset",
                "params": {
                    "preset": _child_param("gpo_preset"),
                },
                "help": (
                    "Recall a GPO preset. This is the only way to drive "
                    "GPO pins over third-party control — pin states are "
                    "read-only."
                ),
            },
            "set_todays_bell_schedule": {
                "label": "Set Today's Bell Schedule",
                "params": {
                    "schedule": _child_param("bell_schedule"),
                },
            },
        },
        "quick_actions": ["play_message", "recall_routine", "recall_scene"],
        "actions": [
            {"id": "play_message", "kind": "command", "icon": "megaphone"},
            {"id": "recall_routine", "kind": "command", "icon": "workflow"},
            {"id": "recall_scene", "kind": "command", "icon": "layers"},
        ],
    }

    HEALTH_FAULT_MESSAGE = (
        "Connected, but the device stopped answering keep-alive probes "
        "(get KeepAlive)."
    )

    # ── Lifecycle ──

    def __init__(self, device_id: str, config: dict[str, Any], state, events):
        self._line_buffer = b""
        self._next_msg_id = 1
        # Resolved when a KeepAlive getResp arrives (see _liveness_probe).
        self._probe_fut: asyncio.Future[None] | None = None
        # Configured roster sizes, used to gate incoming param updates.
        self._counts: dict[str, int] = {
            ctype: max(0, int(config.get(key, default)))
            for ctype, key, default, _ in _ROSTERS
        }
        super().__init__(device_id, config, state, events)

    async def _pre_connect(self) -> None:
        if not self.config.get("host", ""):
            raise ConnectionError(
                f"[{self.device_id}] Atmosphere host is required"
            )

    def _transport_kwargs(
        self, transport_type: str, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        # The device streams newline-delimited JSON; on_data_received does
        # its own line buffering, so take the raw byte stream.
        kwargs["delimiter"] = None
        return kwargs

    async def _initial_sync(self) -> None:
        self._register_topology()
        # Subscribe to all configured params. Out-of-range subscribes
        # are silently ignored by the device, so over-sizing is harmless.
        try:
            await self._request_all("sub")
            await self._send_get("FirmwareVersion", "str")
            await self._send_get("TodaysBellSchedule", "val")
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Initial subscribe failed")

    async def _close_session(self) -> None:
        # Runs on every teardown path: drop any half-received line and
        # unblock a keep-alive probe still waiting on a dead link.
        self._line_buffer = b""
        fut = self._probe_fut
        if fut is not None and not fut.done():
            fut.set_exception(ConnectionError("connection closed"))
        self._probe_fut = None

    # ── Topology ──

    def _register_topology(self) -> None:
        """Register the config-sized rosters as children. Safe to re-run —
        register_child is idempotent, and a driver-seeded placeholder label
        never overrides one the user set in the project. Once the name
        subscription answers, the device's own entity name displays via
        ``label_field``."""
        for ctype, _key, _default, label in _ROSTERS:
            count = self._counts[ctype]
            for n in range(count):
                initial = None
                project = self._project_child_entities.get(
                    ctype, {}).get(f"{n:02d}")
                if not (project and project.get("label")):
                    initial = {"label": f"{label} {n}"}
                self.register_child(ctype, n, initial_state=initial)
            # Reconcile a shrunk count: children beyond the roster are
            # leftovers from an earlier, larger configuration.
            for existing in list(self.list_children(ctype)):
                if not (0 <= existing < count):
                    self.deregister_child(ctype, existing)

    async def refresh_children(self) -> dict[str, Any]:
        """IDE 'Refresh from Device': re-read every subscribed param."""
        if not (self.transport and self.transport.connected):
            raise ConnectionError(f"[{self.device_id}] Not connected")
        self._register_topology()
        await self._request_all("get")
        await self._send_get("FirmwareVersion", "str")
        await self._send_get("TodaysBellSchedule", "val")
        return dict(self._counts)

    # ── Sending ──

    def _next_id(self) -> int:
        n = self._next_msg_id
        self._next_msg_id += 1
        return n

    async def _send_json(self, payload: dict[str, Any]) -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        line = json.dumps(payload, separators=(",", ":")) + "\n"
        await self.transport.send(line.encode("utf-8"))

    async def _send_set(self, param: str, key: str, value: Any) -> None:
        # key is 'val', 'pct', or 'str' — picks how value is interpreted.
        await self._send_json({
            "jsonrpc": "2.0",
            "method": "set",
            "params": {"param": param, key: value},
        })

    async def _send_bump(self, param: str, key: str, value: Any) -> None:
        await self._send_json({
            "jsonrpc": "2.0",
            "method": "bmp",
            "params": {"param": param, key: value},
        })

    async def _send_sub(self, param: str, fmt: str) -> None:
        await self._send_json({
            "jsonrpc": "2.0",
            "method": "sub",
            "params": {"param": param, "fmt": fmt},
        })

    async def _send_get(self, param: str, fmt: str) -> None:
        await self._send_json({
            "jsonrpc": "2.0",
            "method": "get",
            "params": {"param": param, "fmt": fmt},
        })

    async def _request_all(self, method: str) -> None:
        """Send `sub` (or `get`, for a refresh) for every rostered param."""
        send = self._send_sub if method == "sub" else self._send_get
        for ctype, _key, _default, _label in _ROSTERS:
            for prefix, fmt in _TYPE_SUBS[ctype]:
                for n in range(self._counts[ctype]):
                    await send(f"{prefix}_{n}", fmt)
        if method == "sub":
            await send("TodaysBellSchedule", "val")
            await send("FirmwareVersion", "str")

    async def _liveness_probe(self) -> None:
        """Send `get KeepAlive` and await the getResp (ATS006993 §2).

        Correlation is by param name: _dispatch_param_update resolves the
        future when a KeepAlive reply arrives. Subscription updates for
        other params do NOT satisfy it.
        """
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._probe_fut = fut
        try:
            await self._send_get("KeepAlive", "str")
            await fut
        finally:
            self._probe_fut = None

    # ── Commands ──

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = params or {}

        if command == "set_source_gain":
            await self._send_set(
                f"SourceGain_{int(params['source'])}", "val", float(params["gain_db"])
            )
        elif command == "set_source_mute":
            await self._send_set(
                f"SourceMute_{int(params['source'])}",
                "val",
                1 if params["mute"] else 0,
            )
        elif command == "bump_source_gain":
            await self._send_bump(
                f"SourceGain_{int(params['source'])}",
                "val",
                float(params["delta_db"]),
            )
        elif command == "set_zone_gain":
            await self._send_set(
                f"ZoneGain_{int(params['zone'])}", "val", float(params["gain_db"])
            )
        elif command == "set_zone_mute":
            await self._send_set(
                f"ZoneMute_{int(params['zone'])}", "val", 1 if params["mute"] else 0
            )
        elif command == "bump_zone_gain":
            await self._send_bump(
                f"ZoneGain_{int(params['zone'])}", "val", float(params["delta_db"])
            )
        elif command == "set_zone_source":
            await self._send_set(
                f"ZoneSource_{int(params['zone'])}", "val", int(params["source"])
            )
        elif command == "clear_zone_source":
            await self._send_set(
                f"ZoneSource_{int(params['zone'])}", "val", -1
            )
        elif command == "set_mix_gain":
            await self._send_set(
                f"MixGain_{int(params['mix'])}", "val", float(params["gain_db"])
            )
        elif command == "set_mix_mute":
            await self._send_set(
                f"MixMute_{int(params['mix'])}", "val", 1 if params["mute"] else 0
            )
        elif command == "bump_mix_gain":
            await self._send_bump(
                f"MixGain_{int(params['mix'])}", "val", float(params["delta_db"])
            )
        elif command == "set_group_gain":
            await self._send_set(
                f"GroupGain_{int(params['group'])}", "val", float(params["gain_db"])
            )
        elif command == "set_group_mute":
            await self._send_set(
                f"GroupMute_{int(params['group'])}", "val", 1 if params["mute"] else 0
            )
        elif command == "bump_group_gain":
            await self._send_bump(
                f"GroupGain_{int(params['group'])}", "val", float(params["delta_db"])
            )
        elif command == "set_group_source":
            await self._send_set(
                f"GroupSource_{int(params['group'])}", "val", int(params["source"])
            )
        elif command == "clear_group_source":
            await self._send_set(
                f"GroupSource_{int(params['group'])}", "val", -1
            )
        elif command == "set_group_active":
            await self._send_set(
                f"GroupActive_{int(params['group'])}",
                "val",
                1 if params["active"] else 0,
            )
        elif command == "play_message":
            await self._send_set(
                f"PlayMessage_{int(params['message'])}", "val", 1
            )
        elif command == "recall_routine":
            await self._send_set(
                f"RecallRoutine_{int(params['routine'])}", "val", 1
            )
        elif command == "recall_scene":
            await self._send_set(
                f"RecallScene_{int(params['scene'])}", "val", 1
            )
        elif command == "recall_gpo_preset":
            await self._send_set(
                f"RecallGpoPreset_{int(params['preset'])}", "val", 1
            )
        elif command == "set_todays_bell_schedule":
            await self._send_set(
                "TodaysBellSchedule", "val", int(params["schedule"])
            )
        else:
            log.warning(f"[{self.device_id}] Unknown command: {command}")

    # ── Receiving ──

    async def on_data_received(self, data: bytes) -> None:
        self._line_buffer += data
        while b"\n" in self._line_buffer:
            line_bytes, self._line_buffer = self._line_buffer.split(b"\n", 1)
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if line:
                self._handle_line(line)

    def _handle_line(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log.debug(f"[{self.device_id}] Non-JSON line: {line!r}")
            return

        if not isinstance(msg, dict):
            return

        # Responses to messages with "id" come back as {"jsonrpc":"2.0","result":"OK","id":N}
        # — no method, no params. Skip.
        if "method" not in msg:
            return

        method = msg.get("method")
        if method == "error":
            log.warning(f"[{self.device_id}] Device error: {msg}")
            return
        if method not in ("update", "getResp"):
            return

        params = msg.get("params")
        # params may be a single dict or a list of dicts
        if isinstance(params, dict):
            self._dispatch_param_update(params)
        elif isinstance(params, list):
            for item in params:
                if isinstance(item, dict):
                    self._dispatch_param_update(item)

    def _dispatch_param_update(self, item: dict[str, Any]) -> None:
        param = item.get("param", "")
        if not param:
            return

        # Pick whichever value field is present. The device echoes back
        # the same fmt we asked for in the subscribe.
        if "val" in item:
            value: Any = item["val"]
        elif "str" in item:
            value = item["str"]
        elif "pct" in item:
            value = item["pct"]
        else:
            return

        # KeepAlive responses arrive as {"param":"KeepAlive","str":"OK"} —
        # nothing to expose, but the reply is the liveness signal the
        # health loop's probe awaits.
        if param == "KeepAlive":
            fut = self._probe_fut
            if fut is not None and not fut.done():
                fut.set_result(None)
            return

        if param == "FirmwareVersion":
            self.set_state("firmware_version", str(value))
            return

        if param == "TodaysBellSchedule":
            self.set_state("todays_bell_schedule", _to_int(value))
            return

        # SourceGain_3 / ZoneMute_0 / GroupActive_2 / etc. — route the
        # wire param into the matching child prop.
        prefix, _, idx_str = param.rpartition("_")
        if not prefix or not idx_str.isdigit():
            return
        mapped = _PARAM_MAP.get(prefix)
        if not mapped:
            return
        ctype, prop, conv = mapped

        idx = int(idx_str)
        if not (0 <= idx < self._counts.get(ctype, 0)):
            return

        if conv == "float":
            converted: Any = _to_float(value)
        elif conv == "int":
            converted = _to_int(value)
        elif conv == "bool":
            converted = _to_bool(value)
        else:
            converted = str(value)
        if converted is None:
            return

        self.set_child_state(ctype, idx, prop, converted)


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False
