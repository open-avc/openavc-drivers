"""
OpenAVC Allen & Heath Avantis Driver.

Controls Allen & Heath Avantis digital mixing consoles over MIDI-over-
TCP/IP on port 51325. The protocol is the standard MIDI 1.0 wire format
carried over a raw TCP stream (MIDI running status supported). Avantis
is a bigger console than the SQ family and uses a noticeably different
control surface:

  - Mutes are Note On / Note Off (9N CH 7F / 9N CH 00) rather than
    NRPN — applies across Inputs, Mix masters, FX sends, FX returns,
    DCAs and Mute Groups.
  - Faders are a 7-bit single-byte LV value carried via NRPN parameter
    ID 17 (BN 63 CH, BN 62 17, BN 06 LV) with a documented dB lookup
    table.
  - Send levels (Aux / FX / Matrix sends) are SysEx with the Allen &
    Heath header F0 00 00 1A 50 10 01 00, then 0N 0D CH SndN SndCH LV F7.
  - Channel selection uses a 5-MIDI-channel range (N..N+4): inputs at
    N, mono/stereo groups at N+1, mono/stereo aux at N+2, mono/stereo
    matrix at N+3, mono/stereo FX, FX returns, mains, DCAs, mute
    groups, UFX sends/returns at N+4. Channel number within type is
    the Note number / CH byte.
  - Channel Name and Colour are SysEx Get / Reply / Set messages
    addressable per channel. Set is exposed as a command in v1.0.0;
    state-mirrored Get/Reply parsing is deferred.
  - Scene recall is Bank Select + Program Change (4 banks × 128 = 512,
    of which 500 are usable).
  - UFX Global Key and Scale are CC 0x0C and 0x0D.

Push vs poll:
    Avantis pushes scene-recall messages whenever a scene is recalled
    from the console screen. The protocol doc only documents Set forms
    for fader / mute / send-level / assigns (no Get form), but real-
    world behaviour on the dLive / Avantis family is that the console
    also echoes parameter changes — both the controller's own writes
    and surface moves — back on the same socket. The driver listens
    for those echoes and reflects them into state. There is no initial
    sweep on connect because there is no documented Get form for
    fader / mute / assign / send-level values; state populates lazily
    from the first console-side or driver-initiated change.

Format choice:
    Python rather than YAML because (a) the wire format is binary
    (MIDI bytes, NRPN sequences, SysEx with vendor header), (b) the
    addressable surface spans 5 MIDI channels with type-specific note
    ranges that are best computed programmatically rather than
    enumerated, (c) the SysEx send-level shape (multi-byte body with
    two source/destination address pairs) is outside the Configurable
    Driver text/HTTP grammar, and (d) push handling requires fanning
    incoming NRPN absolute sequences plus Note On / Off messages to
    the right state key based on a reverse address map. Same general
    category as the SQ driver (MIDI / NRPN console), so reuses the
    parser shape and aggregator pattern.

Models covered:
    Avantis   — 96 inputs, 54 mono / 27 stereo groups, 54 mono / 27
                stereo aux, 54 mono / 27 stereo matrix, 12 mono / 12
                stereo FX sends, 12 FX returns, 3 mains, 16 DCAs, 8
                mute groups, 8 stereo UFX sends, 8 stereo UFX returns.
    Avantis Solo — same MIDI command surface, smaller physical control
                surface; covered by the same driver.

Source:
    https://help.allen-heath.com/hc/en-us/articles/4423402911377-Avantis-MIDI-Protocol
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

# SysEx
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
# note / CH byte is base_note + (n - 1). Per the protocol doc:
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
#   Mute Group        N+4    46..4D   (8)
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


def state_key(ctype: str, n: int, suffix: str) -> str:
    """Build a state-variable key like ``in01_mute`` or ``dca16_fader``."""
    abbrev = {
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
    }[ctype]
    count = CHANNEL_TYPES[ctype][2]
    fmt = "{:02d}" if count >= 10 else "{}"
    return f"{abbrev}{fmt.format(n)}_{suffix}"


# ── Fader value encode / decode ──────────────────────────────────────────────
#
# Per the Avantis spec table:
#   LV = round(((dB + 54) / 64) * 127), clipped to 0..127.
# The published lookup uses 0x6B (107) for 0 dB, 0x7F (127) for +10 dB,
# 0x00 (0) for -inf. We expose both fader-position (0.0..1.0) and dB
# helpers. Position is the more useful surface for touch panels;
# scripts that want dB precision can use the dB form.

LEVEL_MIN = 0.0
LEVEL_MAX = 1.0

DB_MIN = -54.0   # -inf is encoded as LV=0; we map 0.0 position to LV=0
DB_MAX = 10.0


def level_to_lv(level: float) -> int:
    """Map fader position 0.0..1.0 to LV byte 0..127 (linear)."""
    level = max(LEVEL_MIN, min(LEVEL_MAX, level))
    return round(level * LV_MAX)


def lv_to_level(lv: int) -> float:
    return max(0, min(LV_MAX, lv)) / LV_MAX


def db_to_lv(db: float) -> int:
    """Map dB (-inf..+10) to LV byte 0..127 using the spec formula
    LV = round(((dB + 54) / 64) * 127), clipped to 0..127.
    """
    if db <= DB_MIN:
        return 0
    db = min(DB_MAX, db)
    return max(0, min(LV_MAX, round(((db + 54.0) / 64.0) * 127.0)))


def lv_to_db(lv: int) -> float:
    if lv <= 0:
        return float("-inf")
    return (lv / 127.0) * 64.0 - 54.0


# ── Scene recall ─────────────────────────────────────────────────────────────

def scene_to_bank_program(scene: int) -> tuple[int, int]:
    """Convert 1-based scene (1..500) to (bank, program) per the spec
    table: Bank 0 = scenes 1-128, Bank 1 = 129-256, Bank 2 = 257-384,
    Bank 3 = 385-500.
    """
    if scene < MIN_SCENE or scene > MAX_SCENE:
        raise ValueError(f"Scene {scene} outside 1..{MAX_SCENE}")
    idx = scene - 1
    return idx // 128, idx % 128


# ── UFX Global Key / Scale ───────────────────────────────────────────────────

UFX_KEY_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
UFX_SCALE_NAMES = ["Major", "Minor", "Chromatic"]


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


# ── State-variable construction ──────────────────────────────────────────────

def _build_state_vars() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {
        "current_scene": {
            "type": "integer",
            "label": "Current Scene",
            "min": 0,
            "max": MAX_SCENE,
        },
    }
    for ctype, (_, _, count, label) in CHANNEL_TYPES.items():
        for n in range(1, count + 1):
            out[state_key(ctype, n, "mute")] = {
                "type": "boolean",
                "label": f"{label} {n} Mute",
            }
            if ctype == "mute_group":
                continue
            out[state_key(ctype, n, "fader")] = {
                "type": "number",
                "label": f"{label} {n} Fader",
                "min": LEVEL_MIN,
                "max": LEVEL_MAX,
            }
    return out


# ── Reverse address map (incoming MIDI → state key) ──────────────────────────

class _AddressMap:
    """Maps wire (midi_ch_offset, ch_note) to the channel type / number /
    state keys that address picks up. Built once at construction.
    """

    def __init__(self) -> None:
        # (offset, ch_note) → (ctype, n)
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
    "help": "on, off, or toggle (toggle uses the driver's last-known state).",
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
    "min": DB_MIN,
    "max": DB_MAX,
    "default": 0.0,
    "required": True,
    "label": "Level (dB)",
    "help": "-54 dB to +10 dB. Below -54 dB is silence.",
}


def _channel_n_param(ctype: str) -> dict[str, Any]:
    _, _, count, label = CHANNEL_TYPES[ctype]
    return {
        "type": "integer",
        "min": 1,
        "max": count,
        "required": True,
        "label": label,
        "help": f"{label} number (1..{count}).",
    }


def _build_commands() -> dict[str, dict[str, Any]]:
    cmds: dict[str, dict[str, Any]] = {}

    # Mute commands — one per channel type (action enum chooses on/off/toggle).
    for ctype, (_, _, _, label) in CHANNEL_TYPES.items():
        cmd_id = f"mute_{ctype}"
        cmds[cmd_id] = {
            "label": f"Mute {label}",
            "help": f"Mute, unmute, or toggle a {label.lower()}.",
            "params": {
                "channel": _channel_n_param(ctype),
                "action": dict(_ACTION_PARAM),
            },
        }

    # Fader commands — set absolute fader by position (0..1).
    for ctype in FADER_CHANNEL_TYPES:
        _, _, _, label = CHANNEL_TYPES[ctype]
        cmds[f"set_{ctype}_fader"] = {
            "label": f"Set {label} Fader",
            "help": f"Set the fader position for a {label.lower()}.",
            "params": {
                "channel": _channel_n_param(ctype),
                "level": dict(_LEVEL_PARAM),
            },
        }
        cmds[f"set_{ctype}_fader_db"] = {
            "label": f"Set {label} Fader (dB)",
            "help": f"Set the {label.lower()} fader using a dB value.",
            "params": {
                "channel": _channel_n_param(ctype),
                "db": dict(_DB_PARAM),
            },
        }

    # Send level — generic command covers every documented source/target combo.
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
                "type": "integer",
                "required": True,
                "min": 1,
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
                "type": "integer",
                "required": True,
                "min": 1,
                "label": "Target Number",
                "help": "1-based send destination number within the target type.",
            },
            "level": dict(_LEVEL_PARAM),
        },
    }

    # Channel-to-Main assign (NRPN param 18).
    cmds["set_channel_to_main_assign"] = {
        "label": "Channel → Main Assign",
        "help": "Assign or unassign a channel from the Main Mix.",
        "params": {
            "source_type": {
                "type": "enum",
                "values": ASSIGN_SOURCE_TYPES,
                "required": True,
                "label": "Source Type",
            },
            "source": {
                "type": "integer",
                "required": True,
                "min": 1,
                "label": "Source Number",
            },
            "action": {
                "type": "enum",
                "values": ["on", "off"],
                "default": "on",
                "required": True,
                "label": "Action",
                "help": "on assigns the channel to the Main Mix; off removes it.",
            },
        },
    }

    # DCA assign.
    cmds["set_dca_assign"] = {
        "label": "Channel → DCA Assign",
        "help": "Assign or remove a channel from a DCA group.",
        "params": {
            "source_type": {
                "type": "enum",
                "values": ASSIGN_SOURCE_TYPES,
                "required": True,
                "label": "Source Type",
            },
            "source": {
                "type": "integer",
                "required": True,
                "min": 1,
                "label": "Source Number",
            },
            "dca": {
                "type": "integer",
                "required": True,
                "min": 1,
                "max": NUM_DCAS,
                "label": "DCA",
            },
            "action": {
                "type": "enum",
                "values": ["on", "off"],
                "default": "on",
                "required": True,
                "label": "Action",
            },
        },
    }

    # Mute-group assign.
    cmds["set_mute_group_assign"] = {
        "label": "Channel → Mute Group Assign",
        "help": "Assign or remove a channel from a mute group.",
        "params": {
            "source_type": {
                "type": "enum",
                "values": ASSIGN_SOURCE_TYPES,
                "required": True,
                "label": "Source Type",
            },
            "source": {
                "type": "integer",
                "required": True,
                "min": 1,
                "label": "Source Number",
            },
            "mute_group": {
                "type": "integer",
                "required": True,
                "min": 1,
                "max": NUM_MUTE_GROUPS,
                "label": "Mute Group",
            },
            "action": {
                "type": "enum",
                "values": ["on", "off"],
                "default": "on",
                "required": True,
                "label": "Action",
            },
        },
    }

    # Channel name / colour set.
    cmds["set_channel_name"] = {
        "label": "Set Channel Name",
        "help": (
            "Write a channel name (up to 8 characters). State mirroring of "
            "the reply is not yet implemented — call get_channel_name in a "
            "future build to pull it back."
        ),
        "params": {
            "channel_type": {
                "type": "enum",
                "values": list(CHANNEL_TYPES.keys()),
                "required": True,
                "label": "Channel Type",
            },
            "channel": {
                "type": "integer",
                "required": True,
                "min": 1,
                "label": "Channel Number",
            },
            "name": {
                "type": "string",
                "required": True,
                "label": "Name",
                "help": "Up to 8 characters. Only printable ASCII.",
            },
        },
    }
    cmds["set_channel_colour"] = {
        "label": "Set Channel Colour",
        "help": "Set a channel colour (0 = no colour, 1-7 = colour preset).",
        "params": {
            "channel_type": {
                "type": "enum",
                "values": list(CHANNEL_TYPES.keys()),
                "required": True,
                "label": "Channel Type",
            },
            "channel": {
                "type": "integer",
                "required": True,
                "min": 1,
                "label": "Channel Number",
            },
            "colour": {
                "type": "integer",
                "required": True,
                "min": 0,
                "max": 7,
                "label": "Colour (0-7)",
            },
        },
    }

    # Scene recall.
    cmds["recall_scene"] = {
        "label": "Recall Scene",
        "help": f"Recall a scene 1..{MAX_SCENE} via Bank Select + Program Change.",
        "params": {
            "scene": {
                "type": "integer",
                "required": True,
                "min": MIN_SCENE,
                "max": MAX_SCENE,
                "label": "Scene",
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
                "type": "enum",
                "values": list(UFX_KEY_NAMES),
                "required": True,
                "default": "C",
                "label": "Key",
            },
        },
    }
    cmds["set_ufx_global_scale"] = {
        "label": "Set UFX Global Scale",
        "help": "Set the global UFX scale (Major / Minor / Chromatic).",
        "params": {
            "scale": {
                "type": "enum",
                "values": list(UFX_SCALE_NAMES),
                "required": True,
                "default": "Major",
                "label": "Scale",
            },
        },
    }

    # Refresh — kept for parity with sibling mixers; the protocol has no
    # documented Get for fader / mute, so this re-pings the connection
    # rather than re-sweeping every parameter.
    cmds["refresh"] = {
        "label": "Refresh State",
        "help": (
            "Avantis has no documented Get form for fader, mute, send "
            "level or assign values; state populates from console-side "
            "and driver-initiated change echoes. This command is a no-op "
            "kept for parity with other console drivers."
        ),
        "params": {},
    }

    return cmds


# ── Driver ───────────────────────────────────────────────────────────────────

class AllenHeathAvantisDriver(BaseDriver):
    """Allen & Heath Avantis MIDI-over-TCP driver."""

    DRIVER_INFO = {
        "id": "allenheath_avantis",
        "name": "Allen & Heath Avantis Digital Mixer",
        "manufacturer": "Allen & Heath",
        "category": "audio",
        "version": "1.2.0",
        "author": "OpenAVC",
        "description": (
            "Controls Allen & Heath Avantis digital mixing consoles via "
            "MIDI over TCP/IP on port 51325. Mute / fader / send level / "
            "assign control across all 96 inputs, 54 mono and 27 stereo "
            "groups, 54 mono and 27 stereo aux outs, 54 mono and 27 stereo "
            "matrix outs, 12 mono and 12 stereo FX sends, 12 FX returns, "
            "3 mains, 16 DCAs, 8 mute groups, plus 8 stereo UFX sends and "
            "returns. Scene recall (1-500), channel name and colour set, "
            "MIDI Machine Control transport, UFX global key and scale, and "
            "bidirectional state from console-side change echoes."
        ),
        "source_url": (
            "https://help.allen-heath.com/hc/en-us/articles/4423402911377-Avantis-MIDI-Protocol"
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
            # byte layout. A declarative udp_broadcast_probe needs a
            # PCAP from real hardware to lock down. Soft-only via the
            # Audiotonix Group OUI (00:04:c4 is registered to
            # Audiotonix Group Limited per IEEE — A&H's parent company,
            # also covers DiGiCo / SSL / Calrec consoles, which is fine
            # for first-pass narrowing).
            #   Refs:
            #     allen-heath.com/content/uploads/2023/11/AH-dLive-for-IT-managers.pdf
            #     support.allen-heath.com/hc/en-gb/articles/37287399691409
            "oui_prefixes": ["00:04:c4"],
            "open_ports": [51325],
            "vendor_aliases": [
                "allen & heath", "allen and heath", "a&h",
                "allen-heath", "audiotonix",
            ],
        },
        "min_platform_version": "0.6.0",
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
        "default_config": {
            "host": "",
            "port": 51325,
            "base_midi_channel": 12,
            "poll_interval": 0,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
                "description": "Avantis network IP (Utility / Network on the console).",
            },
            "port": {
                "type": "integer",
                "default": 51325,
                "label": "TCP Port",
                "description": "MIDI over TCP/IP port. Always 51325 on Avantis.",
            },
            "base_midi_channel": {
                "type": "integer",
                "default": 12,
                "min": 1,
                "max": 12,
                "label": "Base MIDI Channel",
                "description": (
                    "Lowest of the 5-channel range used by Avantis (must "
                    "match Utility / Control / MIDI on the console). "
                    "Default 12 means the driver uses channels 12-16. "
                    "Cannot exceed 12."
                ),
            },
            "poll_interval": {
                "type": "integer",
                "default": 0,
                "min": 0,
                "label": "Poll Interval (s)",
                "description": (
                    "Avantis pushes parameter changes; polling is unused. "
                    "Leave at 0 unless debugging."
                ),
            },
        },
        "help": {
            "overview": (
                "Controls an Allen & Heath Avantis digital mixing console "
                "over the network using MIDI over TCP/IP. Full mute, fader, "
                "send-level and assign control plus scene recall, MIDI "
                "Machine Control, channel name and colour, and UFX global "
                "key and scale. Console-side moves push back into OpenAVC "
                "state so faders and mute LEDs on a touch panel stay in sync."
            ),
            "setup": (
                "1. Confirm the console is running firmware V2.0 or later. "
                "TCP/IP control is unavailable on earlier firmware.\n"
                "2. Connect the Avantis to the same network as the OpenAVC "
                "server.\n"
                "3. On the Avantis, go to Utility / Network and note the IP "
                "address.\n"
                "4. Go to Utility / Control / MIDI and note the base MIDI "
                "channel (default 12). The console reserves the next four "
                "channels above the base for output buses, so the base "
                "cannot exceed 12.\n"
                "5. Enter the IP address and matching base MIDI channel in "
                "the device config. The default port (51325) is correct.\n"
                "6. The driver listens for console-side push automatically; "
                "no further setup is required."
            ),
        },
        "state_variables": _build_state_vars(),
        "commands": _build_commands(),
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._addr_map = _AddressMap()
        self._rx_buf = bytearray()
        self._running_status = 0
        # NRPN aggregator per MIDI channel (0..15).
        self._nrpn_state: dict[int, dict[str, int]] = {
            ch: {"msb": 0, "lsb": 0, "vc": 0} for ch in range(16)
        }
        self._last_bank: dict[int, int] = {ch: 0 for ch in range(16)}
        # Mute Note-On pair tracker per channel — a pair of messages on
        # the same (ch, note) arrives back-to-back; we only act on the
        # first (the velocity-bearing one).
        self._note_armed: dict[tuple[int, int], bool] = {}

    # ── Connection lifecycle ────────────────────────────────────────────

    @property
    def _base_midi(self) -> int:
        """0-based base MIDI channel from config (1-12 → 0-11)."""
        n = int(self.config.get("base_midi_channel", 12))
        return max(0, min(11, n - 1))

    async def connect(self) -> None:
        host = self.config.get("host", "")
        port = int(self.config.get("port", 51325))
        if not host:
            raise ValueError("host is required")

        self.transport = await TCPTransport.create(
            host=host,
            port=port,
            on_data=self.on_data_received,
            on_disconnect=self._on_disconnect,
            delimiter=None,                       # raw MIDI byte stream
            name=self.device_id,
        )
        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.{self.device_id}.connected", {})
        log.info("[%s] connected to %s:%d", self.device_id, host, port)

    async def disconnect(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        if self.transport:
            try:
                await self.transport.close()
            except Exception:  # noqa: BLE001
                pass
            self.transport = None
        self._connected = False
        self.set_state("connected", False)

    async def _on_disconnect(self) -> None:
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.{self.device_id}.disconnected", {})

    # ── Send helpers ────────────────────────────────────────────────────

    async def _send(self, data: bytes) -> None:
        if not self.transport:
            return
        await self.transport.send(data)

    def _midi_ch_byte(self, offset: int) -> int:
        """Wire MIDI channel byte (0-15) for a given type offset (0..4)."""
        return (self._base_midi + offset) & 0x0F

    def _build_nrpn_set(self, midi_ch_offset: int, ch_note: int,
                        param_id: int, value: int) -> bytes:
        """3-message NRPN absolute set: BN 63 CH BN 62 PARAM BN 06 VALUE."""
        b = 0xB0 | self._midi_ch_byte(midi_ch_offset)
        return bytes([
            b, CC_NRPN_MSB, ch_note & 0x7F,
            b, CC_NRPN_LSB, param_id & 0x7F,
            b, CC_DATA_ENTRY_MSB, value & 0x7F,
        ])

    def _build_note_pair(self, midi_ch_offset: int, ch_note: int, velocity: int) -> bytes:
        """Mute Note-On / Note-Off pair: 9N CH vel, 9N CH 00."""
        n = 0x90 | self._midi_ch_byte(midi_ch_offset)
        return bytes([n, ch_note & 0x7F, velocity & 0x7F, n, ch_note & 0x7F, 0x00])

    def _build_sysex(self, body: bytes) -> bytes:
        return bytes([0xF0]) + SYSEX_HEADER + body + bytes([0xF7])

    # ── send_command dispatch ───────────────────────────────────────────

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        method = getattr(self, f"cmd_{command}", None)
        if method is None:
            raise ValueError(f"Unknown command: {command}")
        return await method(**params)

    # ── Mutes ───────────────────────────────────────────────────────────
    #
    # The protocol uses Note On with velocity > 0x40 for mute-on and
    # Note On with velocity < 0x40 for mute-off, each followed by a
    # Note On with velocity 0 (= Note Off). Toggle is not in the spec
    # so we reflect the driver's last-known state and send the inverse.

    async def _do_mute(self, ctype: str, channel: int, action: str) -> None:
        offset, ch_note = channel_address(ctype, int(channel))
        action = (action or "on").lower()
        key = state_key(ctype, int(channel), "mute")
        if action == "toggle":
            cur = bool(self.get_state(key))
            action = "off" if cur else "on"
        velocity = 0x7F if action == "on" else 0x3F
        await self._send(self._build_note_pair(offset, ch_note, velocity))

    async def cmd_mute_input(self, channel: int, action: str = "on") -> None:
        await self._do_mute("input", channel, action)

    async def cmd_mute_mono_group(self, channel: int, action: str = "on") -> None:
        await self._do_mute("mono_group", channel, action)

    async def cmd_mute_stereo_group(self, channel: int, action: str = "on") -> None:
        await self._do_mute("stereo_group", channel, action)

    async def cmd_mute_mono_aux(self, channel: int, action: str = "on") -> None:
        await self._do_mute("mono_aux", channel, action)

    async def cmd_mute_stereo_aux(self, channel: int, action: str = "on") -> None:
        await self._do_mute("stereo_aux", channel, action)

    async def cmd_mute_mono_matrix(self, channel: int, action: str = "on") -> None:
        await self._do_mute("mono_matrix", channel, action)

    async def cmd_mute_stereo_matrix(self, channel: int, action: str = "on") -> None:
        await self._do_mute("stereo_matrix", channel, action)

    async def cmd_mute_mono_fx_send(self, channel: int, action: str = "on") -> None:
        await self._do_mute("mono_fx_send", channel, action)

    async def cmd_mute_stereo_fx_send(self, channel: int, action: str = "on") -> None:
        await self._do_mute("stereo_fx_send", channel, action)

    async def cmd_mute_fx_return(self, channel: int, action: str = "on") -> None:
        await self._do_mute("fx_return", channel, action)

    async def cmd_mute_main(self, channel: int, action: str = "on") -> None:
        await self._do_mute("main", channel, action)

    async def cmd_mute_dca(self, channel: int, action: str = "on") -> None:
        await self._do_mute("dca", channel, action)

    async def cmd_mute_mute_group(self, channel: int, action: str = "on") -> None:
        await self._do_mute("mute_group", channel, action)

    async def cmd_mute_ufx_send(self, channel: int, action: str = "on") -> None:
        await self._do_mute("ufx_send", channel, action)

    async def cmd_mute_ufx_return(self, channel: int, action: str = "on") -> None:
        await self._do_mute("ufx_return", channel, action)

    # ── Faders (NRPN param 17, 7-bit LV) ────────────────────────────────

    async def _do_fader(self, ctype: str, channel: int, lv: int) -> None:
        offset, ch_note = channel_address(ctype, int(channel))
        await self._send(self._build_nrpn_set(offset, ch_note, NRPN_PARAM_FADER, lv))

    async def _do_fader_position(self, ctype: str, channel: int, level: float) -> None:
        await self._do_fader(ctype, channel, level_to_lv(float(level)))

    async def _do_fader_db(self, ctype: str, channel: int, db: float) -> None:
        await self._do_fader(ctype, channel, db_to_lv(float(db)))

    # Generated below at class-definition time so every fader-capable
    # channel type gets a `cmd_set_<ctype>_fader` and `cmd_set_<ctype>_fader_db`.

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
        # SysEx body after header: 0N 0D CH SndN SndCH LV
        # 0N is the source MIDI channel byte (0..F), SndN is the target
        # MIDI channel byte (0..F).
        body = bytes([
            self._midi_ch_byte(src_off) & 0x0F,
            SYSEX_CMD_SEND_LEVEL,
            src_ch & 0x7F,
            self._midi_ch_byte(tgt_off) & 0x0F,
            tgt_ch & 0x7F,
            lv & 0x7F,
        ])
        await self._send(self._build_sysex(body))

    # ── Channel-to-Main assign (NRPN param 18) ──────────────────────────

    async def cmd_set_channel_to_main_assign(self, source_type: str,
                                             source: int, action: str = "on") -> None:
        if source_type not in ASSIGN_SOURCE_TYPES:
            raise ValueError(f"Invalid assign source type: {source_type}")
        offset, ch_note = channel_address(source_type, int(source))
        value = 0x7F if (action or "on").lower() == "on" else 0x3F
        await self._send(self._build_nrpn_set(offset, ch_note, NRPN_PARAM_MAIN_ASSIGN, value))

    # ── DCA assign (NRPN param 40) ──────────────────────────────────────

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

    # ── Mute-group assign (NRPN param 40) ───────────────────────────────

    async def cmd_set_mute_group_assign(self, source_type: str, source: int,
                                        mute_group: int, action: str = "on") -> None:
        if source_type not in ASSIGN_SOURCE_TYPES:
            raise ValueError(f"Invalid assign source type: {source_type}")
        offset, ch_note = channel_address(source_type, int(source))
        mg = int(mute_group)
        if mg < 1 or mg > NUM_MUTE_GROUPS:
            raise ValueError(f"Mute group {mg} outside 1..{NUM_MUTE_GROUPS}")
        on = (action or "on").lower() == "on"
        # ON value DB = 50..57 for mute groups 1..8
        # OFF value DA = 10..17 for mute groups 1..8
        value = (0x50 if on else 0x10) + (mg - 1)
        await self._send(self._build_nrpn_set(offset, ch_note, NRPN_PARAM_BUS_ASSIGN, value))

    # ── Channel name / colour set ───────────────────────────────────────

    async def cmd_set_channel_name(self, channel_type: str, channel: int,
                                   name: str) -> None:
        offset, ch_note = channel_address(channel_type, int(channel))
        encoded = (name or "")[:8].encode("ascii", errors="replace")
        # 0N is the source MIDI channel byte.
        body = bytes([self._midi_ch_byte(offset) & 0x0F,
                      SYSEX_CMD_NAME_SET,
                      ch_note & 0x7F]) + encoded
        await self._send(self._build_sysex(body))

    async def cmd_set_channel_colour(self, channel_type: str, channel: int,
                                     colour: int) -> None:
        offset, ch_note = channel_address(channel_type, int(channel))
        col = max(0, min(7, int(colour)))
        body = bytes([self._midi_ch_byte(offset) & 0x0F,
                      SYSEX_CMD_COLOUR_SET,
                      ch_note & 0x7F,
                      col])
        await self._send(self._build_sysex(body))

    # ── Scene recall ────────────────────────────────────────────────────

    async def cmd_recall_scene(self, scene: int) -> None:
        bank, program = scene_to_bank_program(int(scene))
        b = 0xB0 | self._midi_ch_byte(0)   # Scene uses base channel
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

    # ── Refresh (no-op) ─────────────────────────────────────────────────

    async def cmd_refresh(self) -> None:
        return None

    # ── Incoming MIDI parser ────────────────────────────────────────────

    def on_data_received(self, data: bytes) -> None:
        """Called by the transport for every chunk of incoming bytes.

        Buffers and parses MIDI messages out byte-by-byte. NRPN sequences
        are aggregated across messages; running status is supported.
        """
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

            # System Real-Time bytes (0xF8-0xFF) — single-byte, no effect
            # on running status.
            if 0xF8 <= b <= 0xFF:
                i += 1
                continue

            if b & 0x80:
                # Status byte
                if 0xF0 <= b <= 0xF7:
                    if b == 0xF0:
                        end = buf.find(0xF7, i + 1)
                        if end == -1:
                            break  # incomplete
                        self._handle_sysex(bytes(buf[i:end + 1]))
                        i = end + 1
                        running_status = 0
                        continue
                    # Other system common — discard the status; running
                    # status clears.
                    i += 1
                    running_status = 0
                    continue
                running_status = b
                i += 1
                continue

            # Data byte — must have a status byte to apply to.
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
        """Return the channel-type offset (0..4) for an incoming MIDI
        channel byte, or None if it's outside our base..base+4 window.
        """
        offset = (ch - self._base_midi) & 0x0F
        if 0 <= offset <= 4:
            return offset
        return None

    def _handle_note_on(self, ch: int, note: int, velocity: int) -> None:
        """Mute messages arrive as a Note-On pair: vel ∈ {3F, 7F} then
        vel == 0. Velocity 0 is the trailing Note-Off — ignore. The
        spec also says velocities 0x01..0x3F = OFF, 0x40..0x7F = ON
        when received from the console, so any velocity-bearing Note-On
        on a known channel reflects a mute change.
        """
        if velocity == 0x00:
            return  # trailing Note-Off of a mute pair
        offset = self._midi_offset_for_ch(ch)
        if offset is None:
            return
        target = self._addr_map.lookup(offset, note)
        if target is None:
            return
        ctype, n = target
        on = velocity >= 0x40
        self.set_state(state_key(ctype, n, "mute"), on)

    def _handle_cc(self, ch: int, controller: int, value: int) -> None:
        """Aggregate NRPN building blocks and handle Bank Select MSB."""
        if controller == CC_BANK_SELECT_MSB:
            # Tracking the most recent Bank MSB on each MIDI channel for
            # use by the next Program Change.
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
        """Route an NRPN absolute set (we set MSB to CH and LSB to PARAM
        when sending; the Avantis echoes the same shape). Maps to a state
        update for fader / main-assign / DCA-assign / mute-group-assign.
        """
        offset = self._midi_offset_for_ch(ch)
        if offset is None:
            return
        target = self._addr_map.lookup(offset, ch_note)
        if target is None:
            return
        ctype, n = target

        if param_id == NRPN_PARAM_FADER:
            if ctype == "mute_group":
                return  # mute groups have no fader
            self.set_state(state_key(ctype, n, "fader"), lv_to_level(value))
            return

        # NRPN param 18 (main assign) and param 40 (DCA / mute-group
        # assign) don't drive named state vars in v1.0.0. Keep the path
        # open for future extension (e.g. exposing main-assign as
        # `<ctype><n>_main_assign` if a customer needs it) but ignore
        # for now — drives are still sent and acted on by the console.
        return

    def _handle_program_change(self, ch: int, program: int) -> None:
        """Scene recall echo. The console emits CC 00 (Bank MSB) on the
        base channel followed by Program Change with the in-bank index.
        """
        if ch != self._midi_ch_byte(0):
            return
        bank = self._last_bank.get(ch, 0)
        scene = bank * 128 + program + 1
        if MIN_SCENE <= scene <= MAX_SCENE:
            self.set_state("current_scene", scene)

    def _handle_sysex(self, message: bytes) -> None:
        """SysEx parsing in v1.0.0: we accept and discard. Channel name
        and colour replies (cmd 0x02 / 0x05) come back here; mirroring
        them into state is a future extension. Send-level pushes (cmd
        0x0D) also land here without driving state.
        """
        # Future hook: parse self._extract_sysex_body(message) and update
        # state for `<ctype><n>_name` / `<ctype><n>_colour`.
        return


# Class export expected by the loader.
DRIVER_CLASS = AllenHeathAvantisDriver


# ── Late-bind generated fader commands ──────────────────────────────────────
#
# We declare cmd_set_<ctype>_fader and cmd_set_<ctype>_fader_db on the
# driver class for every fader-capable channel type. Doing this in code
# avoids 28 near-identical hand-written method definitions while keeping
# the dispatcher's getattr lookup fast.

def _make_fader_cmd(ctype: str):
    async def _cmd(self, channel: int, level: float) -> None:
        await self._do_fader_position(ctype, channel, level)
    _cmd.__name__ = f"cmd_set_{ctype}_fader"
    return _cmd


def _make_fader_db_cmd(ctype: str):
    async def _cmd(self, channel: int, db: float) -> None:
        await self._do_fader_db(ctype, channel, db)
    _cmd.__name__ = f"cmd_set_{ctype}_fader_db"
    return _cmd


for _ctype in FADER_CHANNEL_TYPES:
    setattr(AllenHeathAvantisDriver, f"cmd_set_{_ctype}_fader", _make_fader_cmd(_ctype))
    setattr(AllenHeathAvantisDriver, f"cmd_set_{_ctype}_fader_db", _make_fader_db_cmd(_ctype))
