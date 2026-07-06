"""
OpenAVC Allen & Heath Avantis Driver — first-class child-entity edition.

Controls Allen & Heath Avantis digital mixing consoles over MIDI-over-
TCP/IP on port 51325. The protocol is the standard MIDI 1.0 wire format
carried over a raw TCP stream (MIDI running status supported), with
vendor-specific SysEx (header F0 00 00 1A 50 10 01 00).

What makes this driver first-class
-----------------------------------
* **Channels as typed child entities.** Every input, group, aux, matrix,
  FX send/return, main, DCA, mute group and UFX send/return is a child
  with live mute / fader / name / colour state. Per-channel commands
  take a channel picker instead of a free-typed number, and console-side
  moves push into child state so panels stay in sync.
* **Console names as labels.** Avantis exposes SysEx Get/Reply for
  channel name and colour; the driver sweeps them on connect and mirrors
  them into per-child ``name`` / ``colour`` props (``label_field: name``
  — the console's own channel names become the display labels in
  OpenAVC).
* **Get-watchdog liveness.** Avantis answers SysEx name Gets; the driver
  probes the Input 1 name on a timer and awaits the reply. A console
  that vanished without closing the socket stops answering, and after
  two misses the driver tears the connection down with a typed
  ``no_response`` fault so the platform reconnects and the device card
  shows the real cause.

Push vs poll:
    Hybrid. When a console-side control moves the Avantis emits the
    matching Note-On / NRPN message, which the driver fans into child
    state immediately. On connect the driver sweeps SysEx name + colour
    Gets across every channel (~840 queries, pipelined with periodic
    yields); a slow polling re-sweep (default 60 s) is the backstop for
    updates missed during transient drops.

    Unlike dLive, the Avantis protocol documents **no Get for mute or
    fader** — name and colour are its only queryable parameters. Mute
    and fader child state therefore starts at defaults and syncs as
    console-side moves push in or the driver writes; it is never
    fabricated from a query that doesn't exist.

    Writes apply optimistically: sibling Qu hardware demonstrated that
    A&H consoles do NOT echo changes they receive over MIDI (only
    surface-side moves are transmitted), so after sending a set the
    driver mirrors the commanded value into its own state. Mute toggle
    flips the driver's last-known state; with no mute Get in the
    protocol there is no read-back to correct staleness, so the
    optimistic mirror plus console-side pushes are the state model.

Format choice:
    Python rather than YAML: binary MIDI wire format, a five-MIDI-
    channel address space, mixed Note-On / NRPN / SysEx message shapes,
    and push fan-out via a reverse address map — none of which fit
    `ConfigurableDriver`. Sibling: `audio/allenheath_dlive.py` (same
    wire family; dLive adds preamp sockets, HPF and a SysEx Get-status
    family that Avantis does not document).

Scope notes:
    - Send levels, main / DCA / mute-group assigns remain commands;
      send-level matrix cells are not mirrored as state (~thousands of
      keys with little practical value), and the Avantis doc documents
      no send-assign SysEx (dLive's 0E) or send-level Get.
    - UFX Global Scale is Major / Minor only (the message spec says
      value 00-01; the article's reference table lists Chromatic 02,
      mirroring the same doc quirk dLive has — the message spec wins).
      v1.x offered Chromatic here, sending an undocumented 02.
    - The channel colour VALUE table is missing from the Avantis
      article ("refer to table" with no table); values follow the dLive
      V2.0 protocol's documented table (same SysEx family, same 00-07
      range, "7 colours or no colour"): Off 00, Red 01, Green 02,
      Yellow 03, Blue 04, Purple 05, Lt Blue 06, White 07.
    - MIDI Strips / Soft-Rotary DAW-control CC traffic and the
      per-UFX-unit MIDI channel are end-user surface assignments
      configured on the console, not integrator-addressable functions.

Models covered:
    Avantis / Avantis Solo (same MIDI command surface). Requires
    console firmware V2.0 or later for TCP/IP control.

Source:
    https://support.allen-heath.com/hc/en-gb/articles/45146889174801-Avantis-MIDI-TCP-IP-Protocol
"""

from __future__ import annotations

import asyncio
from typing import Any

from server.drivers.base import BaseDriver
from server.transport.tcp import TCPTransport
from server.utils.logger import get_logger

log = get_logger(__name__)


# ── Channel counts ───────────────────────────────────────────────────────────
NUM_INPUTS = 96
NUM_MONO_GROUPS = 54
NUM_STEREO_GROUPS = 27
NUM_MONO_AUX = 54
NUM_STEREO_AUX = 27
NUM_MONO_MATRIX = 54
NUM_STEREO_MATRIX = 27
NUM_MONO_FX_SENDS = 12
NUM_STEREO_FX_SENDS = 12
NUM_FX_RETURNS = 12
NUM_MAINS = 3
NUM_DCAS = 16
NUM_MUTE_GROUPS = 8
NUM_UFX_SENDS = 8       # stereo only
NUM_UFX_RETURNS = 8     # stereo only

# Scene
MIN_SCENE = 1
MAX_SCENE = 500

# Fader
LV_MIN = 0
LV_MAX = 0x7F  # 127

# NRPN parameter IDs
NRPN_PARAM_FADER = 0x17           # 23 — fader level
NRPN_PARAM_MAIN_ASSIGN = 0x18     # 24 — channel-to-main assign
NRPN_PARAM_BUS_ASSIGN = 0x40      # 64 — DCA / mute-group assign

# CC numbers used outside NRPN
CC_BANK_SELECT_MSB = 0x00
CC_NRPN_MSB = 0x63
CC_NRPN_LSB = 0x62
CC_DATA_ENTRY_MSB = 0x06
CC_UFX_GLOBAL_KEY = 0x0C
CC_UFX_GLOBAL_SCALE = 0x0D

# SysEx command bytes (after the vendor header + 0N MIDI-channel byte).
# Name and colour are the ONLY documented Get/Reply pairs — Avantis has
# no dLive-style Get-status family for mute / fader / preamp.
SYSEX_HEADER = bytes([0x00, 0x00, 0x1A, 0x50, 0x10, 0x01, 0x00])
SYSEX_CMD_NAME_GET = 0x01
SYSEX_CMD_NAME_REPLY = 0x02
SYSEX_CMD_NAME_SET = 0x03
SYSEX_CMD_COLOUR_GET = 0x04
SYSEX_CMD_COLOUR_REPLY = 0x05
SYSEX_CMD_COLOUR_SET = 0x06
SYSEX_CMD_SEND_LEVEL = 0x0D


# ── Channel-type address tables ──────────────────────────────────────────────
#
# Each addressable channel lives at (midi_ch_offset, base_note) where
# the wire MIDI channel byte is base_midi_ch + midi_ch_offset and the
# note / CH byte is base_note + (n - 1). Per the Avantis protocol doc:
#
#   Inputs            N      00..5F   (96)
#   Mono Groups       N+1    00..35   (54)
#   Stereo Groups     N+1    40..5A   (27)
#   Mono Aux          N+2    00..35   (54)
#   Stereo Aux        N+2    40..5A   (27)
#   Mono Matrix       N+3    00..35   (54)
#   Stereo Matrix     N+3    40..5A   (27)
#   Mono FX Send      N+4    00..0B   (12)
#   Stereo FX Send    N+4    10..1B   (12)
#   FX Return         N+4    20..2B   (12)
#   Mains             N+4    30..32   (3)
#   DCA               N+4    36..45   (16)
#   Mute Group        N+4    46..4D   (8)   — dLive's sit at 4E..55
#   Stereo UFX Send   N+4    56..5D   (8)
#   Stereo UFX Return N+4    5E..65   (8)

CHANNEL_TYPES: dict[str, tuple[int, int, int, str]] = {
    # ctype → (midi_ch_offset, base_note, count, label)
    "input":          (0, 0x00, NUM_INPUTS, "Input"),
    "mono_group":     (1, 0x00, NUM_MONO_GROUPS, "Mono Group"),
    "stereo_group":   (1, 0x40, NUM_STEREO_GROUPS, "Stereo Group"),
    "mono_aux":       (2, 0x00, NUM_MONO_AUX, "Mono Aux"),
    "stereo_aux":     (2, 0x40, NUM_STEREO_AUX, "Stereo Aux"),
    "mono_matrix":    (3, 0x00, NUM_MONO_MATRIX, "Mono Matrix"),
    "stereo_matrix":  (3, 0x40, NUM_STEREO_MATRIX, "Stereo Matrix"),
    "mono_fx_send":   (4, 0x00, NUM_MONO_FX_SENDS, "Mono FX Send"),
    "stereo_fx_send": (4, 0x10, NUM_STEREO_FX_SENDS, "Stereo FX Send"),
    "fx_return":      (4, 0x20, NUM_FX_RETURNS, "FX Return"),
    "main":           (4, 0x30, NUM_MAINS, "Main"),
    "dca":            (4, 0x36, NUM_DCAS, "DCA"),
    "mute_group":     (4, 0x46, NUM_MUTE_GROUPS, "Mute Group"),
    "ufx_send":       (4, 0x56, NUM_UFX_SENDS, "UFX Send"),
    "ufx_return":     (4, 0x5E, NUM_UFX_RETURNS, "UFX Return"),
}

_PLURAL_LABELS = {
    "input": "Inputs",
    "mono_group": "Mono Groups",
    "stereo_group": "Stereo Groups",
    "mono_aux": "Mono Auxes",
    "stereo_aux": "Stereo Auxes",
    "mono_matrix": "Mono Matrices",
    "stereo_matrix": "Stereo Matrices",
    "mono_fx_send": "Mono FX Sends",
    "stereo_fx_send": "Stereo FX Sends",
    "fx_return": "FX Returns",
    "main": "Mains",
    "dca": "DCAs",
    "mute_group": "Mute Groups",
    "ufx_send": "UFX Sends",
    "ufx_return": "UFX Returns",
}

# Channels that have a fader (everything except mute_group).
FADER_CHANNEL_TYPES = [t for t in CHANNEL_TYPES if t != "mute_group"]

# Source channel types valid for SysEx send-level commands.
SEND_SOURCE_TYPES = [
    "input", "mono_group", "stereo_group", "fx_return",
]
# Target channel types valid for SysEx send-level commands.
SEND_TARGET_TYPES = [
    "mono_aux", "stereo_aux", "mono_matrix", "stereo_matrix",
    "mono_fx_send", "stereo_fx_send", "ufx_send",
]
# Source channel types that can be assigned to DCAs / mute groups / main.
ASSIGN_SOURCE_TYPES = [
    "input", "mono_group", "stereo_group", "fx_return",
]


def channel_address(ctype: str, n: int) -> tuple[int, int]:
    """Return (midi_ch_offset, ch_note) for the given channel type and
    1-based channel number. Raises ValueError if out of range.
    """
    spec = CHANNEL_TYPES.get(ctype)
    if spec is None:
        raise ValueError(f"Unknown channel type: {ctype}")
    offset, base_note, count, _ = spec
    if n < 1 or n > count:
        raise ValueError(f"{ctype} {n} outside 1..{count}")
    return offset, base_note + (n - 1)


_ABBREV = {
    "input": "in",
    "mono_group": "mgrp",
    "stereo_group": "sgrp",
    "mono_aux": "maux",
    "stereo_aux": "saux",
    "mono_matrix": "mmtx",
    "stereo_matrix": "smtx",
    "mono_fx_send": "mfxs",
    "stereo_fx_send": "sfxs",
    "fx_return": "fxr",
    "main": "main",
    "dca": "dca",
    "mute_group": "mtgrp",
    "ufx_send": "ufxs",
    "ufx_return": "ufxr",
}


def child_sid(ctype: str, n: int) -> str:
    """Build a child local-id like ``in01`` or ``dca16`` (zero-padded to
    the type's count width, matching the v1 flat state-key convention)."""
    abbrev = _ABBREV[ctype]
    count = CHANNEL_TYPES[ctype][2]
    if count >= 100:
        fmt = "{:03d}"
    elif count >= 10:
        fmt = "{:02d}"
    else:
        fmt = "{}"
    return f"{abbrev}{fmt.format(n)}"


# ── Fader value encode / decode ──────────────────────────────────────────────
#
# LV = round(((dB + 54) / 64) * 127) — matches the spec lookup table to
# +/-1 LSB at every documented point (+10 dB = 7F, 0 dB = 6B, -inf = 00).

LEVEL_MIN = 0.0
LEVEL_MAX = 1.0

DB_MIN = -54.0
DB_MAX = 10.0


def level_to_lv(level: float) -> int:
    level = max(LEVEL_MIN, min(LEVEL_MAX, level))
    return round(level * LV_MAX)


def lv_to_level(lv: int) -> float:
    return max(0, min(LV_MAX, lv)) / LV_MAX


def db_to_lv(db: float) -> int:
    if db <= DB_MIN:
        return 0
    db = min(DB_MAX, db)
    return max(0, min(LV_MAX, round(((db + 54.0) / 64.0) * 127.0)))


def lv_to_db(lv: int) -> float:
    if lv <= 0:
        return float("-inf")
    return (lv / 127.0) * 64.0 - 54.0


# ── Scene banking ────────────────────────────────────────────────────────────

def scene_to_bank_program(scene: int) -> tuple[int, int]:
    """Convert 1-based scene (1..500) to (bank, program). Bank 0 = scenes
    1-128, Bank 1 = 129-256, Bank 2 = 257-384, Bank 3 = 385-500.
    """
    if scene < MIN_SCENE or scene > MAX_SCENE:
        raise ValueError(f"Scene {scene} outside 1..{MAX_SCENE}")
    idx = scene - 1
    return idx // 128, idx % 128


# ── Channel colours ──────────────────────────────────────────────────────────
#
# Off 00, Red 01, Green 02, Yellow 03, Blue 04, Purple 05, Lt Blue 06,
# White 07 — per the dLive V2.0 protocol's colour table (the Avantis
# article says "Col = 00 to 07, refer to table" but omits the table; see
# the module docstring).

COLOUR_NAMES = [
    "off", "red", "green", "yellow", "blue", "purple", "light_blue", "white",
]


def colour_to_value(name: str) -> int:
    try:
        return COLOUR_NAMES.index((name or "").lower())
    except ValueError as e:
        raise ValueError(f"Unknown colour: {name}") from e


def value_to_colour(value: int) -> str:
    if 0 <= value < len(COLOUR_NAMES):
        return COLOUR_NAMES[value]
    return str(value)


# ── UFX Global Key / Scale ───────────────────────────────────────────────────
#
# Global Scale is 00-01 (Major / Minor) per the message spec — the
# article's reference table lists Chromatic 02, but only for the
# per-UFX-unit CC parameter scaling, matching the same doc quirk dLive
# has. v1.x offered Chromatic here (an undocumented 02 on the wire).

UFX_KEY_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
UFX_SCALE_NAMES = ["Major", "Minor"]


def ufx_key_to_value(name: str) -> int:
    try:
        return UFX_KEY_NAMES.index(name)
    except ValueError as e:
        raise ValueError(f"Unknown UFX key: {name}") from e


def ufx_scale_to_value(name: str) -> int:
    try:
        return UFX_SCALE_NAMES.index(name)
    except ValueError as e:
        raise ValueError(f"Unknown UFX scale: {name}") from e


# ── MMC transport codes (real-time SysEx F0 7F 7F 06 TC F7) ──────────────────
MMC_CODES = {
    "stop": 0x01,
    "play": 0x02,
    "ff": 0x04,
    "rewind": 0x05,
    "record": 0x06,
    "pause": 0x09,
}


# ── Child entity types ───────────────────────────────────────────────────────
#
# Mutes relay at high cloud priority (a muted program feed is an
# incident); continuous faders and cosmetic name / colour are low.

def _build_child_types() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for ctype, (_, _, _, label) in CHANNEL_TYPES.items():
        svars: dict[str, Any] = {
            "mute": {"type": "boolean", "label": "Mute",
                     "cloud_priority": "high"},
        }
        if ctype != "mute_group":
            svars["fader"] = {"type": "number", "label": "Fader",
                              "min": LEVEL_MIN, "max": LEVEL_MAX,
                              "cloud_priority": "low"}
        svars["name"] = {"type": "string", "label": "Name",
                         "cloud_priority": "low"}
        svars["colour"] = {"type": "string", "label": "Colour",
                           "cloud_priority": "low"}
        summary = ["mute"] if ctype == "mute_group" else ["mute", "fader"]
        out[ctype] = {
            "label": label,
            "label_plural": _PLURAL_LABELS[ctype],
            "state_variables": svars,
            "summary_fields": summary,
            # The console's own channel name (swept + pushed via SysEx)
            # is the display label.
            "label_field": "name",
            "id_format": {"type": "string", "max_length": 64},
        }
    return out


CHILD_ENTITY_TYPES: dict[str, dict[str, Any]] = _build_child_types()


# ── Reverse address map (incoming MIDI → child) ──────────────────────────────

class _AddressMap:
    """Maps wire (midi_ch_offset, ch_note) to (ctype, n)."""

    def __init__(self) -> None:
        self._lookup: dict[tuple[int, int], tuple[str, int]] = {}
        for ctype, (offset, base_note, count, _) in CHANNEL_TYPES.items():
            for n in range(1, count + 1):
                self._lookup[(offset, base_note + (n - 1))] = (ctype, n)

    def lookup(self, midi_ch_offset: int, ch_note: int) -> tuple[str, int] | None:
        return self._lookup.get((midi_ch_offset, ch_note))


# ── Command dictionary builder ───────────────────────────────────────────────

_ACTION_PARAM = {
    "type": "enum",
    "values": ["on", "off", "toggle"],
    "default": "on",
    "required": True,
    "label": "Action",
    "help": "on, off, or toggle (toggle flips the driver's last-known "
            "state — the Avantis protocol has no mute query to read the "
            "result back).",
}

_LEVEL_PARAM = {
    "type": "number",
    "min": LEVEL_MIN,
    "max": LEVEL_MAX,
    "default": 0.75,
    "required": True,
    "label": "Level",
    "help": "Fader position 0.0 (-inf) to 1.0 (+10 dB).",
}

_DB_PARAM = {
    "type": "number",
    "max": DB_MAX,
    "default": 0.0,
    "required": True,
    "label": "Level (dB)",
    "help": "-54 dB to +10 dB. Anything at or below -54 dB maps to silence (-inf), so no lower bound is enforced.",
}


def _child_param(ctype: str, label: str | None = None) -> dict[str, Any]:
    return {"type": "child_id", "child_type": ctype, "required": True,
            "label": label or CHILD_ENTITY_TYPES[ctype]["label"]}


def _build_commands() -> dict[str, dict[str, Any]]:
    cmds: dict[str, dict[str, Any]] = {}

    # Mute commands — one per channel type, channel picker.
    for ctype, (_, _, _, label) in CHANNEL_TYPES.items():
        cmds[f"mute_{ctype}"] = {
            "label": f"Mute {label}",
            "help": f"Mute, unmute, or toggle a {label.lower()}.",
            "params": {
                "channel": _child_param(ctype),
                "action": dict(_ACTION_PARAM),
            },
        }

    # Fader commands — set absolute fader by position (0..1) or dB.
    for ctype in FADER_CHANNEL_TYPES:
        _, _, _, label = CHANNEL_TYPES[ctype]
        cmds[f"set_{ctype}_fader"] = {
            "label": f"Set {label} Fader",
            "help": f"Set the fader position for a {label.lower()}.",
            "params": {
                "channel": _child_param(ctype),
                "level": dict(_LEVEL_PARAM),
            },
        }
        cmds[f"set_{ctype}_fader_db"] = {
            "label": f"Set {label} Fader (dB)",
            "help": f"Set the {label.lower()} fader using a dB value.",
            "params": {
                "channel": _child_param(ctype),
                "db": dict(_DB_PARAM),
            },
        }

    cmds["mute_all_inputs"] = {
        "label": "Mute All Inputs",
        "help": "Mute every input channel.",
        "params": {},
    }
    cmds["unmute_all_inputs"] = {
        "label": "Unmute All Inputs",
        "help": "Unmute every input channel.",
        "params": {},
    }

    # Send level — generic command across every documented source/target.
    # A send cell is a relationship between two channels, not a child, so
    # these keep type + integer params.
    cmds["set_send_level"] = {
        "label": "Set Send Level",
        "help": (
            "Set a SysEx send level from any source channel to any send "
            "destination (Aux / Matrix / FX Send / UFX Send)."
        ),
        "params": {
            "source_type": {
                "type": "enum",
                "values": SEND_SOURCE_TYPES,
                "required": True,
                "label": "Source Type",
            },
            "source": {
                "type": "integer", "required": True, "min": 1,
                "label": "Source Number",
                "help": "1-based channel number within the source type.",
            },
            "target_type": {
                "type": "enum",
                "values": SEND_TARGET_TYPES,
                "required": True,
                "label": "Target Type",
            },
            "target": {
                "type": "integer", "required": True, "min": 1,
                "label": "Target Number",
                "help": "1-based send destination number within the target type.",
            },
            "level": dict(_LEVEL_PARAM),
        },
    }

    # Channel-to-Main assign (NRPN 18).
    cmds["set_channel_to_main_assign"] = {
        "label": "Channel → Main Assign",
        "help": "Assign or unassign a channel from the Main Mix.",
        "params": {
            "source_type": {
                "type": "enum", "values": ASSIGN_SOURCE_TYPES,
                "required": True, "label": "Source Type",
            },
            "source": {"type": "integer", "required": True, "min": 1, "label": "Source Number"},
            "action": {
                "type": "enum", "values": ["on", "off"], "default": "on",
                "required": True, "label": "Action",
            },
        },
    }

    # DCA assign.
    cmds["set_dca_assign"] = {
        "label": "Channel → DCA Assign",
        "help": "Assign or remove a channel from a DCA group (1..16).",
        "params": {
            "source_type": {
                "type": "enum", "values": ASSIGN_SOURCE_TYPES,
                "required": True, "label": "Source Type",
            },
            "source": {"type": "integer", "required": True, "min": 1, "label": "Source Number"},
            "dca": {
                "type": "integer", "required": True,
                "min": 1, "max": NUM_DCAS, "label": "DCA",
            },
            "action": {
                "type": "enum", "values": ["on", "off"], "default": "on",
                "required": True, "label": "Action",
            },
        },
    }

    # Mute-group assign.
    cmds["set_mute_group_assign"] = {
        "label": "Channel → Mute Group Assign",
        "help": "Assign or remove a channel from a mute group (1..8).",
        "params": {
            "source_type": {
                "type": "enum", "values": ASSIGN_SOURCE_TYPES,
                "required": True, "label": "Source Type",
            },
            "source": {"type": "integer", "required": True, "min": 1, "label": "Source Number"},
            "mute_group": {
                "type": "integer", "required": True,
                "min": 1, "max": NUM_MUTE_GROUPS, "label": "Mute Group",
            },
            "action": {
                "type": "enum", "values": ["on", "off"], "default": "on",
                "required": True, "label": "Action",
            },
        },
    }

    # Channel name / colour set. These take a channel type + number pair
    # because one command spans all fifteen channel types (a child picker
    # binds to a single child type).
    cmds["set_channel_name"] = {
        "label": "Set Channel Name",
        "help": (
            "Write a channel name (up to 8 characters). The name is "
            "mirrored into the channel's `name` child prop."
        ),
        "params": {
            "channel_type": {
                "type": "enum", "values": list(CHANNEL_TYPES.keys()),
                "required": True, "label": "Channel Type",
            },
            "channel": {"type": "integer", "required": True, "min": 1, "label": "Channel Number"},
            "name": {
                "type": "string", "required": True, "label": "Name",
                "help": "Up to 8 characters. Only printable ASCII.",
            },
        },
    }
    cmds["set_channel_colour"] = {
        "label": "Set Channel Colour",
        "help": "Set a channel display colour.",
        "params": {
            "channel_type": {
                "type": "enum", "values": list(CHANNEL_TYPES.keys()),
                "required": True, "label": "Channel Type",
            },
            "channel": {"type": "integer", "required": True, "min": 1, "label": "Channel Number"},
            "colour": {
                "type": "enum", "values": list(COLOUR_NAMES),
                "default": "off", "required": True, "label": "Colour",
            },
        },
    }

    # Scene recall.
    cmds["recall_scene"] = {
        "label": "Recall Scene",
        "help": f"Recall a scene 1..{MAX_SCENE} via Bank Select + Program Change.",
        "params": {
            "scene": {
                "type": "integer", "required": True,
                "min": MIN_SCENE, "max": MAX_SCENE, "label": "Scene",
            },
        },
    }

    # MMC transport.
    for action in MMC_CODES:
        cmds[f"mmc_{action}"] = {
            "label": f"MMC {action.title()}",
            "help": f"Send MIDI Machine Control {action.upper()} to the console.",
            "params": {},
        }

    # UFX Global Key / Scale.
    cmds["set_ufx_global_key"] = {
        "label": "Set UFX Global Key",
        "help": "Set the global UFX musical key (C..B).",
        "params": {
            "key": {
                "type": "enum", "values": list(UFX_KEY_NAMES),
                "required": True, "default": "C", "label": "Key",
            },
        },
    }
    cmds["set_ufx_global_scale"] = {
        "label": "Set UFX Global Scale",
        "help": "Set the global UFX scale (Major / Minor).",
        "params": {
            "scale": {
                "type": "enum", "values": list(UFX_SCALE_NAMES),
                "required": True, "default": "Major", "label": "Scale",
            },
        },
    }

    # Refresh — full SysEx name/colour re-sweep.
    cmds["refresh"] = {
        "label": "Refresh Names & Colours",
        "help": (
            "Re-query every channel's name and colour from the console "
            "(the only parameters the Avantis protocol can read back)."
        ),
        "params": {},
    }

    return cmds


# ── Driver ───────────────────────────────────────────────────────────────────

class AllenHeathAvantisDriver(BaseDriver):
    """Allen & Heath Avantis MIDI-over-TCP driver."""

    # Liveness: a SysEx name Get for Input 1, awaited by channel address.
    # Two missed replies force a typed no_response reconnect.
    HEALTH_FAULT_MESSAGE = (
        "Connected, but the Avantis stopped answering Get requests — "
        "connection lost."
    )

    DRIVER_INFO = {
        "id": "allenheath_avantis",
        "name": "Allen & Heath Avantis Digital Mixer",
        "manufacturer": "Allen & Heath",
        "category": "audio",
        "version": "2.0.0",
        "author": "OpenAVC",
        "description": (
            "Controls Allen & Heath Avantis digital mixing consoles via "
            "MIDI over TCP/IP on port 51325. Every input, group, aux, "
            "matrix, FX send/return, main, DCA, mute group and UFX "
            "send/return is a child entity with live mute / fader / "
            "name / colour state — the console's own channel names "
            "become the labels in OpenAVC. Send levels, main / DCA / "
            "mute-group assigns, Scene Recall (1-500), MMC transport "
            "and UFX global key / scale, with bidirectional state from "
            "console-side moves and a Get-probe watchdog that flips the "
            "device offline if the console vanishes."
        ),
        "source_url": (
            "https://support.allen-heath.com/hc/en-gb/articles/45146889174801-Avantis-MIDI-TCP-IP-Protocol"
        ),
        "tags": ["mixer", "console", "midi", "nrpn", "sysex", "allen-heath"],
        "verified": False,
        "simulated": True,
        "protocols": ["midi-over-tcp"],
        "ports": [51325],
        "transport": "tcp",
        "discovery": {
            # A&H consoles use the AHNet announcement protocol on UDP
            # 51320 (1 Hz unsolicited broadcast carrying name/type/sw),
            # but the exact wire-format header bytes and field offsets
            # aren't publicly documented — A&H IT Manager PDF only
            # specifies the cadence + payload-size envelope, not the
            # byte layout. A declarative udp_probe needs a PCAP from
            # real hardware to lock down. Hint-only via the Audiotonix
            # Group OUI (00:04:c4 is registered to Audiotonix Group
            # Limited per IEEE — A&H's parent company, also covers
            # DiGiCo / SSL / Calrec consoles, which is fine for
            # first-pass narrowing).
            #   Refs:
            #     allen-heath.com/content/uploads/2023/11/AH-dLive-for-IT-managers.pdf
            #     support.allen-heath.com/hc/en-gb/articles/37287399691409
            "oui": ["00:04:c4"],
            "port_open": [51325],
            "manufacturer_alias": [
                "allen & heath", "allen and heath", "a&h",
                "allen-heath", "audiotonix",
            ],
        },
        # Child entities + cloud_priority tiers + the liveness watchdog hook.
        "min_platform_version": "0.22.0",
        "compatible_models": [
            {
                "manufacturer": "Allen & Heath",
                "models": ["Avantis", "Avantis Solo"],
                "confidence": "untested",
                "notes": (
                    "Same MIDI command surface across the Avantis family. "
                    "Requires console firmware V2.0 or later for TCP/IP "
                    "control."
                ),
            },
        ],
        "child_entity_types": CHILD_ENTITY_TYPES,
        "default_config": {
            "host": "",
            "port": 51325,
            "base_midi_channel": 12,
            "poll_interval": 60,
        },
        "config_schema": {
            "host": {
                "type": "string", "required": True,
                "label": "IP Address",
                "description": (
                    "Avantis network IP (Utility / Network on the console)."
                ),
            },
            "port": {
                "type": "integer", "default": 51325, "label": "TCP Port",
                "description": "MIDI over TCP/IP port. Always 51325 on Avantis.",
            },
            "base_midi_channel": {
                "type": "integer", "default": 12, "min": 1, "max": 12,
                "label": "Base MIDI Channel",
                "description": (
                    "Lowest of the 5-channel range used by Avantis (must "
                    "match Utility / Control / MIDI on the console). "
                    "Default 12 means the driver uses channels 12-16. "
                    "Cannot exceed 12."
                ),
            },
            "poll_interval": {
                "type": "integer", "default": 60, "min": 0,
                "label": "Poll Interval (s)",
                "description": (
                    "Periodic name/colour re-sweep backstop. Push keeps "
                    "mute / fader state live; the sweep catches label "
                    "updates missed during transient drops. 0 disables it."
                ),
            },
        },
        "quick_actions": [
            "recall_scene", "mute_all_inputs", "unmute_all_inputs", "refresh",
        ],
        "actions": [
            {"id": "recall_scene", "kind": "command", "icon": "layers"},
            {"id": "mute_all_inputs", "kind": "command", "icon": "volume-x"},
            {"id": "unmute_all_inputs", "kind": "command", "icon": "volume-2"},
            {"id": "refresh", "kind": "command", "icon": "refresh-cw"},
        ],
        "help": {
            "overview": (
                "Controls an Allen & Heath Avantis digital mixing console "
                "over the network using MIDI over TCP/IP. Every channel "
                "strip is listed as a child entity with live state — bind "
                "child state (e.g. device.<id>.input.in01.mute) to panel "
                "elements and drive mutes, faders and scene recall from "
                "macros and buttons. The console's channel names and "
                "colours are read on connect and become the labels shown "
                "in OpenAVC; console-side moves push straight back into "
                "state."
            ),
            "setup": (
                "1. Confirm the console is running firmware V2.0 or "
                "later. TCP/IP control is unavailable on earlier "
                "firmware.\n"
                "2. Connect the Avantis to the same network as the "
                "OpenAVC server.\n"
                "3. On the Avantis, go to Utility / Network and note the "
                "IP address.\n"
                "4. Go to Utility / Control / MIDI and note the base MIDI "
                "channel (default 12). The console reserves the next four "
                "channels above the base for output buses, so the base "
                "cannot exceed 12.\n"
                "5. Enter the IP address and matching base MIDI channel "
                "in the device config. The default port (51325) is "
                "correct.\n"
                "6. Save. The channels appear under the device with live "
                "state; channel names and colours sync from the console "
                "automatically."
            ),
        },
        "state_variables": {
            "current_scene": {
                "type": "integer",
                "label": "Current Scene",
                "min": 0,
                "max": MAX_SCENE,
            },
        },
        "commands": _build_commands(),
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._addr_map = _AddressMap()
        self._rx_buf = bytearray()
        self._running_status = 0
        self._nrpn_state: dict[int, dict[str, int]] = {
            ch: {"msb": 0, "lsb": 0, "vc": 0} for ch in range(16)
        }
        self._last_bank: dict[int, int] = {ch: 0 for ch in range(16)}

        # Child routing, built by _register_topology():
        #   _child_sid: (ctype, n) -> child local id
        #   _child_num: (ctype, sid) -> 1-based channel number
        self._child_sid: dict[tuple[str, int], str] = {}
        self._child_num: dict[tuple[str, str], int] = {}

        # Liveness probe bookkeeping (see _liveness_probe): the probed
        # channel address + a future resolved by the matching name reply.
        self._probe_addr = channel_address("input", 1)
        self._probe_fut: asyncio.Future[None] | None = None

    # ── Connection lifecycle ────────────────────────────────────────────

    @property
    def _base_midi(self) -> int:
        n = int(self.config.get("base_midi_channel", 12))
        return max(0, min(11, n - 1))

    async def connect(self) -> None:
        host = self.config.get("host", "")
        port = int(self.config.get("port", 51325))
        if not host:
            raise ValueError("host is required")

        self._probe_fut = None
        self.transport = await TCPTransport.create(
            host=host,
            port=port,
            on_data=self.on_data_received,
            on_disconnect=self._on_disconnect,
            delimiter=None,                 # raw MIDI byte stream, not line-framed
            name=self.device_id,
        )
        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.{self.device_id}.connected", {})
        log.info("[%s] connected to %s:%d", self.device_id, host, port)

        # Register the (static) child roster, then sweep names + colours.
        self._register_topology()
        await asyncio.sleep(0.1)
        await self._refresh_all()

        # This connect() doesn't run BaseDriver.connect(), so start the
        # polling backstop and the liveness watchdog ourselves.
        poll_interval = float(self.config.get("poll_interval", 60) or 0)
        if poll_interval > 0:
            await self.start_polling(poll_interval)
        self._start_health_loop()

    async def disconnect(self) -> None:
        self._stop_health_loop()
        await self.stop_polling()
        if self.transport:
            try:
                await self.transport.close()
            except Exception:  # noqa: BLE001
                pass
            self.transport = None
        self._connected = False
        self.set_state("connected", False)

    def _on_disconnect(self) -> None:
        self._connected = False
        self.set_state("connected", False)
        # BaseDriver's bookkeeping stops the health loop / polling and
        # schedules the reconnect.
        super()._handle_transport_disconnect()

    # ── Topology registration ───────────────────────────────────────────

    def _register_topology(self) -> None:
        """Register the fixed channel roster as children. Safe to re-run —
        register_child is idempotent, and a driver-seeded label never
        overrides one the user set in the project. The generic label is a
        pre-sweep placeholder; once the name sweep answers, the console's
        own channel name displays via ``label_field``."""
        self._child_sid = {}
        self._child_num = {}

        for ctype, (_, _, count, label) in CHANNEL_TYPES.items():
            for n in range(1, count + 1):
                sid = child_sid(ctype, n)
                self._child_sid[(ctype, n)] = sid
                self._child_num[(ctype, sid)] = n
                initial = None
                project = self._project_child_entities.get(ctype, {}).get(sid)
                if not (project and project.get("label")):
                    initial = {"label": f"{label} {n}"}
                self.register_child(ctype, sid, initial_state=initial)

    async def refresh_children(self) -> dict[str, Any]:
        """IDE 'Refresh from Device' + the Refresh quick action: re-sweep
        every channel name and colour."""
        if not (self.transport and self._connected):
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self._refresh_all()
        return {"channels": len(self._child_num)}

    # ── Polling backstop ────────────────────────────────────────────────

    async def poll(self) -> None:
        """Periodic name/colour re-sweep. Push covers the live mute /
        fader case; this catches renames missed during transient drops.
        """
        if not self.connected:
            return
        await self._refresh_all()

    # ── Send helpers ────────────────────────────────────────────────────

    async def _send(self, data: bytes) -> None:
        if not self.transport:
            return
        await self.transport.send(data)

    def _midi_ch_byte(self, offset: int) -> int:
        return (self._base_midi + offset) & 0x0F

    def _build_nrpn_set(self, midi_ch_offset: int, ch_note: int,
                        param_id: int, value: int) -> bytes:
        b = 0xB0 | self._midi_ch_byte(midi_ch_offset)
        return bytes([
            b, CC_NRPN_MSB, ch_note & 0x7F,
            b, CC_NRPN_LSB, param_id & 0x7F,
            b, CC_DATA_ENTRY_MSB, value & 0x7F,
        ])

    def _build_note_pair(self, midi_ch_offset: int, ch_note: int, velocity: int) -> bytes:
        n = 0x90 | self._midi_ch_byte(midi_ch_offset)
        return bytes([n, ch_note & 0x7F, velocity & 0x7F, n, ch_note & 0x7F, 0x00])

    def _build_sysex(self, body: bytes) -> bytes:
        return bytes([0xF0]) + SYSEX_HEADER + body + bytes([0xF7])

    # SysEx Get builders — name (0N 01 CH) and colour (0N 04 CH) are the
    # only Get requests the Avantis protocol documents.

    def _build_get_name(self, midi_ch_offset: int, ch_note: int) -> bytes:
        return self._build_sysex(bytes([
            self._midi_ch_byte(midi_ch_offset), SYSEX_CMD_NAME_GET,
            ch_note & 0x7F,
        ]))

    def _build_get_colour(self, midi_ch_offset: int, ch_note: int) -> bytes:
        return self._build_sysex(bytes([
            self._midi_ch_byte(midi_ch_offset), SYSEX_CMD_COLOUR_GET,
            ch_note & 0x7F,
        ]))

    # ── Liveness watchdog (BaseDriver health loop) ──────────────────────

    async def _liveness_probe(self) -> None:
        """Send a SysEx name Get for Input 1 and await the reply.

        Pushes only happen when a console-side control moves, and sweep
        Gets are fire-and-forget, so an Avantis that vanished without a
        FIN would otherwise stay "online" forever. Replies aren't tagged,
        so the probe correlates by channel address: any Name Reply for
        the probed channel resolves it (the Get reply — or a reply
        triggered by another client's Get, which equally proves the
        console is alive). A mute / fader / colour push, or another
        channel's name, does NOT satisfy it.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        self._probe_fut = fut
        try:
            offset, note = self._probe_addr
            await self._send(self._build_get_name(offset, note))
            await fut
        finally:
            self._probe_fut = None

    # ── send_command dispatch ───────────────────────────────────────────

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        method = getattr(self, f"cmd_{command}", None)
        if method is None:
            raise ValueError(f"Unknown command: {command}")
        return await method(**params)

    # ── Channel resolution ──────────────────────────────────────────────

    def _resolve_n(self, ctype: str, sid: Any) -> int:
        n = self._child_num.get((ctype, str(sid)))
        if n is None:
            raise ValueError(f"Unknown {ctype}: {sid!r}")
        return n

    # ── Mutes ───────────────────────────────────────────────────────────

    async def _do_mute(self, ctype: str, channel: Any, action: str) -> None:
        n = self._resolve_n(ctype, channel)
        offset, ch_note = channel_address(ctype, n)
        action = (action or "on").lower()
        if action == "toggle":
            # No mute Get exists on Avantis, so toggle flips the driver's
            # last-known state (kept live by console-side pushes) with no
            # read-back to correct staleness.
            cur = bool(self.get_child_state(ctype, str(channel)).get("mute"))
            on = not cur
        else:
            on = action == "on"
        await self._send(self._build_note_pair(offset, ch_note, 0x7F if on else 0x3F))
        # Optimistic: mirror the commanded value (A&H consoles don't echo
        # changes they receive over MIDI — confirmed on Qu hardware).
        self._apply_mute(offset, ch_note, on)

    async def _do_mute_all_inputs(self, on: bool) -> None:
        for n in range(1, NUM_INPUTS + 1):
            offset, ch_note = channel_address("input", n)
            await self._send(self._build_note_pair(offset, ch_note, 0x7F if on else 0x3F))
            self._apply_mute(offset, ch_note, on)
            if n % 16 == 0:
                await asyncio.sleep(0.01)

    async def cmd_mute_all_inputs(self) -> None:
        await self._do_mute_all_inputs(True)

    async def cmd_unmute_all_inputs(self) -> None:
        await self._do_mute_all_inputs(False)

    # Mute / fader method generation is at the bottom of the file (one
    # cmd_mute_<ctype> / cmd_set_<ctype>_fader[_db] per channel type).

    # ── Faders (NRPN param 17, 7-bit LV) ────────────────────────────────

    async def _do_fader(self, ctype: str, channel: Any, lv: int) -> None:
        n = self._resolve_n(ctype, channel)
        offset, ch_note = channel_address(ctype, n)
        await self._send(self._build_nrpn_set(offset, ch_note, NRPN_PARAM_FADER, lv))
        self._apply_fader(offset, ch_note, lv)

    async def _do_fader_position(self, ctype: str, channel: Any, level: float) -> None:
        await self._do_fader(ctype, channel, level_to_lv(float(level)))

    async def _do_fader_db(self, ctype: str, channel: Any, db: float) -> None:
        await self._do_fader(ctype, channel, db_to_lv(float(db)))

    # ── Send levels (SysEx 0D) ──────────────────────────────────────────

    async def cmd_set_send_level(self, source_type: str, source: int,
                                 target_type: str, target: int,
                                 level: float) -> None:
        if source_type not in SEND_SOURCE_TYPES:
            raise ValueError(f"Invalid send source type: {source_type}")
        if target_type not in SEND_TARGET_TYPES:
            raise ValueError(f"Invalid send target type: {target_type}")
        src_off, src_ch = channel_address(source_type, int(source))
        tgt_off, tgt_ch = channel_address(target_type, int(target))
        lv = level_to_lv(float(level))
        body = bytes([
            self._midi_ch_byte(src_off) & 0x0F,
            SYSEX_CMD_SEND_LEVEL,
            src_ch & 0x7F,
            self._midi_ch_byte(tgt_off) & 0x0F,
            tgt_ch & 0x7F,
            lv & 0x7F,
        ])
        await self._send(self._build_sysex(body))

    # ── Channel-to-Main assign (NRPN 18) ────────────────────────────────

    async def cmd_set_channel_to_main_assign(self, source_type: str,
                                             source: int, action: str = "on") -> None:
        if source_type not in ASSIGN_SOURCE_TYPES:
            raise ValueError(f"Invalid assign source type: {source_type}")
        offset, ch_note = channel_address(source_type, int(source))
        value = 0x7F if (action or "on").lower() == "on" else 0x3F
        await self._send(self._build_nrpn_set(offset, ch_note, NRPN_PARAM_MAIN_ASSIGN, value))

    # ── DCA assign (NRPN 40) ────────────────────────────────────────────

    async def cmd_set_dca_assign(self, source_type: str, source: int,
                                 dca: int, action: str = "on") -> None:
        if source_type not in ASSIGN_SOURCE_TYPES:
            raise ValueError(f"Invalid assign source type: {source_type}")
        offset, ch_note = channel_address(source_type, int(source))
        dca_idx = int(dca)
        if dca_idx < 1 or dca_idx > NUM_DCAS:
            raise ValueError(f"DCA {dca_idx} outside 1..{NUM_DCAS}")
        on = (action or "on").lower() == "on"
        # ON value DB = 40..4F for DCA 1..16
        # OFF value DA = 00..0F for DCA 1..16
        value = (0x40 if on else 0x00) + (dca_idx - 1)
        await self._send(self._build_nrpn_set(offset, ch_note, NRPN_PARAM_BUS_ASSIGN, value))

    # ── Mute-group assign (NRPN 40) ─────────────────────────────────────

    async def cmd_set_mute_group_assign(self, source_type: str, source: int,
                                        mute_group: int, action: str = "on") -> None:
        if source_type not in ASSIGN_SOURCE_TYPES:
            raise ValueError(f"Invalid assign source type: {source_type}")
        offset, ch_note = channel_address(source_type, int(source))
        mg = int(mute_group)
        if mg < 1 or mg > NUM_MUTE_GROUPS:
            raise ValueError(f"Mute group {mg} outside 1..{NUM_MUTE_GROUPS}")
        on = (action or "on").lower() == "on"
        # ON value DB = 50..57 for mute groups 1..8 (dLive's sit at 58..5F)
        # OFF value DA = 10..17 for mute groups 1..8 (dLive's at 18..1F)
        value = (0x50 if on else 0x10) + (mg - 1)
        await self._send(self._build_nrpn_set(offset, ch_note, NRPN_PARAM_BUS_ASSIGN, value))

    # ── Channel name / colour set ───────────────────────────────────────

    async def cmd_set_channel_name(self, channel_type: str, channel: int,
                                   name: str) -> None:
        offset, ch_note = channel_address(channel_type, int(channel))
        cleaned = (name or "")[:8]
        encoded = cleaned.encode("ascii", errors="replace")
        body = bytes([
            self._midi_ch_byte(offset) & 0x0F,
            SYSEX_CMD_NAME_SET,
            ch_note & 0x7F,
        ]) + encoded
        await self._send(self._build_sysex(body))
        self._apply_name(offset, ch_note, cleaned)

    async def cmd_set_channel_colour(self, channel_type: str, channel: int,
                                     colour: str) -> None:
        offset, ch_note = channel_address(channel_type, int(channel))
        col = colour_to_value(colour)
        body = bytes([
            self._midi_ch_byte(offset) & 0x0F,
            SYSEX_CMD_COLOUR_SET,
            ch_note & 0x7F,
            col,
        ])
        await self._send(self._build_sysex(body))
        self._apply_colour(offset, ch_note, col)

    # ── Scene recall ────────────────────────────────────────────────────

    async def cmd_recall_scene(self, scene: int) -> None:
        bank, program = scene_to_bank_program(int(scene))
        b = 0xB0 | self._midi_ch_byte(0)
        c = 0xC0 | self._midi_ch_byte(0)
        await self._send(bytes([b, CC_BANK_SELECT_MSB, bank, c, program]))
        self.set_state("current_scene", int(scene))

    # ── MMC transport (real-time SysEx) ─────────────────────────────────

    async def _send_mmc(self, code: int) -> None:
        await self._send(bytes([0xF0, 0x7F, 0x7F, 0x06, code & 0x7F, 0xF7]))

    async def cmd_mmc_play(self) -> None:    await self._send_mmc(MMC_CODES["play"])
    async def cmd_mmc_stop(self) -> None:    await self._send_mmc(MMC_CODES["stop"])
    async def cmd_mmc_pause(self) -> None:   await self._send_mmc(MMC_CODES["pause"])
    async def cmd_mmc_record(self) -> None:  await self._send_mmc(MMC_CODES["record"])
    async def cmd_mmc_ff(self) -> None:      await self._send_mmc(MMC_CODES["ff"])
    async def cmd_mmc_rewind(self) -> None:  await self._send_mmc(MMC_CODES["rewind"])

    # ── UFX Global Key / Scale ──────────────────────────────────────────

    async def cmd_set_ufx_global_key(self, key: str) -> None:
        value = ufx_key_to_value(key)
        b = 0xB0 | self._midi_ch_byte(0)
        await self._send(bytes([b, CC_UFX_GLOBAL_KEY, value & 0x7F]))

    async def cmd_set_ufx_global_scale(self, scale: str) -> None:
        value = ufx_scale_to_value(scale)
        b = 0xB0 | self._midi_ch_byte(0)
        await self._send(bytes([b, CC_UFX_GLOBAL_SCALE, value & 0x7F]))

    # ── Refresh (SysEx name/colour sweep) ───────────────────────────────

    async def cmd_refresh(self) -> None:
        """Manually re-sweep names + colours. Same as the on-connect query."""
        await self._refresh_all()

    async def _refresh_all(self) -> None:
        """Issue SysEx Get queries for every parameter the protocol can
        read back — name and colour per channel (~840 queries). Pipelined
        with a yield every 16 messages to keep the socket flowing on a
        fresh connect. Mute / fader have no Get on Avantis.
        """
        if not self.connected:
            return

        queries: list[bytes] = []
        for ctype, (_, _, count, _) in CHANNEL_TYPES.items():
            for n in range(1, count + 1):
                offset, ch_note = channel_address(ctype, n)
                queries.append(self._build_get_name(offset, ch_note))
                queries.append(self._build_get_colour(offset, ch_note))

        for i, q in enumerate(queries):
            try:
                await self._send(q)
            except Exception:  # noqa: BLE001
                break
            if i % 16 == 15:
                await asyncio.sleep(0.01)

    # ── Incoming MIDI parser ────────────────────────────────────────────

    def on_data_received(self, data: bytes) -> None:
        if not data:
            return
        self._rx_buf.extend(data)
        self._parse()

    def _parse(self) -> None:
        buf = self._rx_buf
        running_status = self._running_status

        i = 0
        while i < len(buf):
            b = buf[i]

            if 0xF8 <= b <= 0xFF:
                i += 1
                continue

            if b & 0x80:
                if 0xF0 <= b <= 0xF7:
                    if b == 0xF0:
                        end = buf.find(0xF7, i + 1)
                        if end == -1:
                            break  # incomplete
                        self._handle_sysex(bytes(buf[i:end + 1]))
                        i = end + 1
                        running_status = 0
                        continue
                    i += 1
                    running_status = 0
                    continue
                running_status = b
                i += 1
                continue

            if not running_status:
                i += 1
                continue

            high = running_status & 0xF0
            ch = running_status & 0x0F

            if high in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
                if i + 1 >= len(buf):
                    break  # incomplete
                d1 = buf[i]
                d2 = buf[i + 1]
                i += 2
                if d1 & 0x80 or d2 & 0x80:
                    running_status = 0
                    continue
                if high == 0x90:
                    self._handle_note_on(ch, d1, d2)
                elif high == 0xB0:
                    self._handle_cc(ch, d1, d2)
            elif high in (0xC0, 0xD0):
                if i >= len(buf):
                    break
                d1 = buf[i]
                i += 1
                if d1 & 0x80:
                    running_status = 0
                    continue
                if high == 0xC0:
                    self._handle_program_change(ch, d1)
            else:
                i += 1

        self._running_status = running_status
        del buf[:i]

    # ── MIDI handlers ───────────────────────────────────────────────────

    def _midi_offset_for_ch(self, ch: int) -> int | None:
        offset = (ch - self._base_midi) & 0x0F
        if 0 <= offset <= 4:
            return offset
        return None

    def _handle_note_on(self, ch: int, note: int, velocity: int) -> None:
        if velocity == 0x00:
            return  # trailing Note-Off of a mute pair
        offset = self._midi_offset_for_ch(ch)
        if offset is None:
            return
        self._apply_mute(offset, note, velocity >= 0x40)

    def _handle_cc(self, ch: int, controller: int, value: int) -> None:
        if controller == CC_BANK_SELECT_MSB:
            self._last_bank[ch] = value
            return

        nrpn = self._nrpn_state[ch]
        if controller == CC_NRPN_MSB:
            nrpn["msb"] = value
        elif controller == CC_NRPN_LSB:
            nrpn["lsb"] = value
        elif controller == CC_DATA_ENTRY_MSB:
            self._dispatch_nrpn(ch, nrpn["msb"], nrpn["lsb"], value)

    def _dispatch_nrpn(self, ch: int, ch_note: int, param_id: int, value: int) -> None:
        offset = self._midi_offset_for_ch(ch)
        if offset is None:
            return

        if param_id == NRPN_PARAM_FADER:
            self._apply_fader(offset, ch_note, value)
            return

        # Main assign / DCA assign / mute-group assign echoes are
        # accepted (no-op) — those aren't mirrored as state.
        return

    def _handle_program_change(self, ch: int, program: int) -> None:
        if ch != self._midi_ch_byte(0):
            return
        bank = self._last_bank.get(ch, 0)
        if 0 <= bank <= 3:
            scene = bank * 128 + program + 1
            if MIN_SCENE <= scene <= MAX_SCENE:
                self.set_state("current_scene", scene)

    def _handle_sysex(self, message: bytes) -> None:
        """Parse Get replies into child state. The console only sends the
        reply forms — the request forms (name/colour Get) originate from
        the controller, so a body starting 0N 05 on the receive side is
        always a Colour Reply (0N 05 CH Col). Send-level frames (0D) may
        arrive as console-side move echoes; send cells aren't mirrored as
        state, so they are accepted and dropped.
        """
        if len(message) < 2 or message[0] != 0xF0 or message[-1] != 0xF7:
            return
        body = message[1:-1]
        if not body.startswith(SYSEX_HEADER):
            return
        body = body[len(SYSEX_HEADER):]
        if len(body) < 3:
            return

        midi_ch = body[0] & 0x0F
        cmd = body[1]
        rest = body[2:]
        offset = self._midi_offset_for_ch(midi_ch)
        if offset is None:
            return

        if cmd == SYSEX_CMD_NAME_REPLY:
            # 0N 02 CH <name ASCII...>
            ch_note = rest[0] & 0x7F
            # Resolve the liveness probe on a name reply for the probed
            # channel (see _liveness_probe).
            if (self._probe_fut is not None and not self._probe_fut.done()
                    and (offset, ch_note) == self._probe_addr):
                self._probe_fut.set_result(None)
            name = rest[1:].decode("ascii", errors="replace").rstrip(" \x00")
            self._apply_name(offset, ch_note, name)
            return

        if cmd == SYSEX_CMD_COLOUR_REPLY and len(rest) == 2:
            # 0N 05 CH Col
            self._apply_colour(offset, rest[0] & 0x7F, rest[1] & 0x7F)
            return

        # Send level echoes (0D) and anything else: drop.
        return

    # ── State fan-out ───────────────────────────────────────────────────
    #
    # Shared by the inbound parser AND the optimistic write path, so a
    # sent value and a received value land in state identically.

    def _child_at(self, offset: int, ch_note: int) -> tuple[str, str] | None:
        target = self._addr_map.lookup(offset, ch_note)
        if target is None:
            return None
        ctype, n = target
        sid = self._child_sid.get((ctype, n))
        if sid is None:
            return None
        return ctype, sid

    def _set_child(self, ctype: str, sid: str, prop: str, value: Any) -> None:
        try:
            self.set_child_state_batch(ctype, sid, {prop: value})
        except Exception:  # noqa: BLE001
            pass

    def _apply_mute(self, offset: int, ch_note: int, on: bool) -> None:
        child = self._child_at(offset, ch_note)
        if child:
            self._set_child(*child, "mute", bool(on))

    def _apply_fader(self, offset: int, ch_note: int, lv: int) -> None:
        child = self._child_at(offset, ch_note)
        if child and child[0] != "mute_group":
            self._set_child(*child, "fader", lv_to_level(lv))

    def _apply_name(self, offset: int, ch_note: int, name: str) -> None:
        child = self._child_at(offset, ch_note)
        if child:
            self._set_child(*child, "name", name)

    def _apply_colour(self, offset: int, ch_note: int, col: int) -> None:
        child = self._child_at(offset, ch_note)
        if child:
            self._set_child(*child, "colour", value_to_colour(col))


# Class export expected by the loader.
DRIVER_CLASS = AllenHeathAvantisDriver


# ── Late-bind generated mute / fader commands ───────────────────────────────
#
# We declare cmd_mute_<ctype>, cmd_set_<ctype>_fader, and
# cmd_set_<ctype>_fader_db on the driver class for every channel type.
# Doing this in code avoids 43 near-identical hand-written method
# definitions while keeping the dispatcher's getattr lookup fast.

def _make_mute_cmd(ctype: str):
    async def _cmd(self, channel: Any, action: str = "on") -> None:
        await self._do_mute(ctype, channel, action)
    _cmd.__name__ = f"cmd_mute_{ctype}"
    return _cmd


def _make_fader_cmd(ctype: str):
    async def _cmd(self, channel: Any, level: float) -> None:
        await self._do_fader_position(ctype, channel, level)
    _cmd.__name__ = f"cmd_set_{ctype}_fader"
    return _cmd


def _make_fader_db_cmd(ctype: str):
    async def _cmd(self, channel: Any, db: float) -> None:
        await self._do_fader_db(ctype, channel, db)
    _cmd.__name__ = f"cmd_set_{ctype}_fader_db"
    return _cmd


for _ctype in CHANNEL_TYPES:
    setattr(AllenHeathAvantisDriver, f"cmd_mute_{_ctype}", _make_mute_cmd(_ctype))

for _ctype in FADER_CHANNEL_TYPES:
    setattr(AllenHeathAvantisDriver, f"cmd_set_{_ctype}_fader", _make_fader_cmd(_ctype))
    setattr(AllenHeathAvantisDriver, f"cmd_set_{_ctype}_fader_db", _make_fader_db_cmd(_ctype))
