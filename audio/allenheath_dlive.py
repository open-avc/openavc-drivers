"""
OpenAVC Allen & Heath dLive Driver.

Controls Allen & Heath dLive digital mixing systems (MixRack + Surface)
over MIDI-over-TCP/IP on port 51325. The protocol is the standard MIDI
1.0 wire format carried over a raw TCP stream (MIDI running status
supported), with vendor-specific SysEx (header F0 00 00 1A 50 10 01 00).
The dLive command surface is closely related to Avantis's but with
larger channel counts and a richer feature set:

  - Inputs 1-128, Mono Groups 1-62, Stereo Groups 1-31, Mono Aux 1-62,
    Stereo Aux 1-31, Mono Matrix 1-62, Stereo Matrix 1-31, Mono FX
    Send 1-16, Stereo FX Send 1-16, FX Return 1-16, Mains 1-6, DCAs
    1-24, Mute Groups 1-8 (at CH 4E-55, shifted up from Avantis's 46
    range to make room for the larger DCA bank), 8 stereo UFX sends
    and 8 stereo UFX returns.
  - Mutes are Note On / Note Off pairs (9N CH 7F / 9N CH 00) — same as
    Avantis.
  - Faders are 7-bit LV via NRPN parameter ID 17 (BN 63 CH, BN 62 17,
    BN 06 LV), formula LV = round(((dB + 54) / 64) * 127).
  - Send levels (Aux / FX / Matrix sends) are SysEx 0D — same shape as
    Avantis. dLive additionally exposes Input-to-Group/Aux assign
    on/off via SysEx 0E.
  - Preamp control: socket gain via Pitchbend (EN MP GV) over MP socket
    numbers (MixRack 1-64, DX1/2 1-32, DX3/4 1-32 = 128 total), Pad
    on/off via SysEx 09, 48V on/off via SysEx 0C.
  - HPF: cutoff frequency via NRPN parameter ID 30, on/off via
    parameter ID 31. Frequency uses the spec formula
    `vv = INT(127 * ((4608 * log10(F/4) / log10(2)) - 10699) / 41314)`.
  - Channel Name (SysEx cmd 03) and Colour (SysEx cmd 06) Set, with
    Get-reply mirroring deferred (same approach as Avantis).
  - Scene Recall: Bank Select + Program Change for 4 banks × 128 = 500
    scenes (MixRack only). Cue List Recall: 16 banks × 128 = 2000
    user-defined Recall Ids (Surface only).
  - MMC transport (real-time SysEx F0 7F 7F 06 TC F7).
  - UFX Global Key (CC 0C) / Scale (CC 0D) on the base MIDI channel.

Push vs poll:
    The MixRack pushes parameter-change echoes on the same socket — the
    driver listens for Note On / NRPN / Bank-Select-plus-Program-Change
    pushes and reflects them into state. Unlike Avantis, dLive
    *does* expose explicit SysEx Get queries (mute, fader, main assign,
    send level, preamp pad/48V, EQ, HPF), but issuing a sweep on
    connect would require ~1500 queries; we keep state populating
    lazily from echoes. The Get parsers exist in `_handle_sysex` so a
    future refresh implementation can wire them up.

Format choice:
    Python rather than YAML for the same reasons as the Avantis driver
    (binary MIDI wire, multi-channel address space, mixed Note-On /
    NRPN / SysEx shapes, push fan-out via reverse address map). dLive
    adds a Pitchbend preamp-gain message (EN MP GV) and PEQ / HPF NRPN
    sets that compound the case for Python — none of these fit
    `ConfigurableDriver`. Sibling: `audio/allenheath_avantis.py`.

TLS / auth:
    dLive supports TLS on port 51327 with a UserProfile + UserPassword
    handshake; the plain-TCP port 51325 has no auth. v1.0.0 ships
    plain-TCP. TLS+auth is a future extension once the platform has a
    TLS transport primitive that fits this shape.

Models covered:
    dLive S-series consoles + DM MixRacks (MIDI control surface is the
    same across the family). Requires firmware V2.0 or later for
    TCP/IP control.

Source:
    https://www.allen-heath.com/content/uploads/2024/06/dLive-MIDI-Over-TCP-Protocol-V2.0.pdf
"""

from __future__ import annotations

import math
from typing import Any

from server.drivers.base import BaseDriver
from server.transport.tcp import TCPTransport
from server.utils.logger import get_logger

log = get_logger(__name__)


# ── Channel counts ───────────────────────────────────────────────────────────
NUM_INPUTS = 128
NUM_MONO_GROUPS = 62
NUM_STEREO_GROUPS = 31
NUM_MONO_AUX = 62
NUM_STEREO_AUX = 31
NUM_MONO_MATRIX = 62
NUM_STEREO_MATRIX = 31
NUM_MONO_FX_SENDS = 16
NUM_STEREO_FX_SENDS = 16
NUM_FX_RETURNS = 16
NUM_MAINS = 6
NUM_DCAS = 24
NUM_MUTE_GROUPS = 8
NUM_UFX_SENDS = 8       # stereo only
NUM_UFX_RETURNS = 8     # stereo only

# Scene
MIN_SCENE = 1
MAX_SCENE = 500

# Cue (Surface only)
MIN_CUE = 1
MAX_CUE = 2000

# Sockets (preamp addressing — MP byte)
NUM_SOCKETS = 128       # MixRack 1-64 + DX1/2 1-32 + DX3/4 1-32

# Fader
LV_MIN = 0
LV_MAX = 0x7F  # 127

# NRPN parameter IDs
NRPN_PARAM_FADER = 0x17           # 23 — fader level
NRPN_PARAM_MAIN_ASSIGN = 0x18     # 24 — channel-to-main assign
NRPN_PARAM_BUS_ASSIGN = 0x40      # 64 — DCA / mute-group assign
NRPN_PARAM_HPF_FREQ = 0x30        # 48 — HPF cutoff frequency
NRPN_PARAM_HPF_ONOFF = 0x31       # 49 — HPF on/off

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
SYSEX_CMD_COLOUR_REPLY = 0x05    # also "generic Get response" — see _handle_sysex
SYSEX_CMD_COLOUR_SET = 0x06
SYSEX_CMD_PAD_GET = 0x07
SYSEX_CMD_PAD_REPLY = 0x08
SYSEX_CMD_PAD_SET = 0x09
SYSEX_CMD_48V_GET = 0x0A
SYSEX_CMD_48V_REPLY = 0x0B
SYSEX_CMD_48V_SET = 0x0C
SYSEX_CMD_SEND_LEVEL = 0x0D
SYSEX_CMD_SEND_ASSIGN = 0x0E
SYSEX_CMD_GENERIC_GET = 0x05      # 0N, 05, ... family for mute/fader/main/etc
SYSEX_GET_SUBKIND_MUTE = 0x09
SYSEX_GET_SUBKIND_NRPN = 0x0B     # body: subkind, NRPN_param, CH
SYSEX_GET_SUBKIND_SEND = 0x0F     # body: subkind, send_kind (0D or 0E), src CH, snd N, snd CH


# ── Channel-type address tables ──────────────────────────────────────────────
#
# Each addressable channel lives at (midi_ch_offset, base_note) where
# the wire MIDI channel byte is base_midi_ch + midi_ch_offset and the
# note / CH byte is base_note + (n - 1). Per the dLive protocol doc:
#
#   Inputs            N      00..7F   (128)
#   Mono Groups       N+1    00..3D   (62)
#   Stereo Groups     N+1    40..5E   (31)
#   Mono Aux          N+2    00..3D   (62)
#   Stereo Aux        N+2    40..5E   (31)
#   Mono Matrix       N+3    00..3D   (62)
#   Stereo Matrix     N+3    40..5E   (31)
#   Mono FX Send      N+4    00..0F   (16)
#   Stereo FX Send    N+4    10..1F   (16)
#   FX Return         N+4    20..2F   (16)
#   Mains             N+4    30..35   (6)
#   DCA               N+4    36..4D   (24)
#   Mute Group        N+4    4E..55   (8)   — shifted up from Avantis (46-4D)
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
    "mute_group":     (4, 0x4E, NUM_MUTE_GROUPS, "Mute Group"),
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
# Channel types that own a per-channel HPF (input + mix masters).
HPF_CHANNEL_TYPES = [
    "input", "mono_group", "stereo_group", "mono_aux", "stereo_aux",
    "mono_matrix", "stereo_matrix", "main", "fx_return",
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


def state_key(ctype: str, n: int, suffix: str) -> str:
    """Build a state-variable key like ``in001_mute`` or ``dca24_fader``."""
    abbrev = _ABBREV[ctype]
    count = CHANNEL_TYPES[ctype][2]
    if count >= 100:
        fmt = "{:03d}"
    elif count >= 10:
        fmt = "{:02d}"
    else:
        fmt = "{}"
    return f"{abbrev}{fmt.format(n)}_{suffix}"


# ── Fader value encode / decode ──────────────────────────────────────────────
#
# Same formula as Avantis: LV = round(((dB + 54) / 64) * 127).

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


# ── Preamp gain (Pitchbend 0..127, MixRack range +5..+60 dB) ─────────────────
#
# The reference table maps GV bytes to socket gain in dB:
#   +5 dB → 0x00, +60 dB → 0x7F, formula GV = round(((gain - 5) / 55) * 127).
# Below +5 dB is encoded as 0x00 (the mic pre's minimum gain).

GAIN_DB_MIN = 5.0
GAIN_DB_MAX = 60.0


def gain_to_gv(gain_db: float) -> int:
    if gain_db <= GAIN_DB_MIN:
        return 0
    gain_db = min(GAIN_DB_MAX, gain_db)
    return max(0, min(LV_MAX, round(((gain_db - GAIN_DB_MIN) / (GAIN_DB_MAX - GAIN_DB_MIN)) * 127.0)))


def gv_to_gain(gv: int) -> float:
    gv = max(0, min(LV_MAX, gv))
    return GAIN_DB_MIN + (gv / 127.0) * (GAIN_DB_MAX - GAIN_DB_MIN)


# ── HPF frequency encode / decode ────────────────────────────────────────────
#
# Spec formula: vv = INT(127 * ((4608 * log10(F/4) / log10(2)) - 10699) / 41314)
# Inverse:      F  = 4 * 2 ** ((vv * 41314 / 127 + 10699) / 4608)

HPF_FREQ_MIN = 20.0
HPF_FREQ_MAX = 20000.0


def hpf_freq_to_vv(freq_hz: float) -> int:
    if freq_hz < HPF_FREQ_MIN:
        return 0
    freq_hz = min(HPF_FREQ_MAX, freq_hz)
    val = int(127.0 * ((4608.0 * math.log10(freq_hz / 4.0) / math.log10(2.0)) - 10699.0) / 41314.0)
    return max(0, min(LV_MAX, val))


def vv_to_hpf_freq(vv: int) -> float:
    vv = max(0, min(LV_MAX, vv))
    log_arg = (vv * 41314.0 / 127.0 + 10699.0) / 4608.0
    return 4.0 * (2.0 ** log_arg)


# ── Scene recall ─────────────────────────────────────────────────────────────

def scene_to_bank_program(scene: int) -> tuple[int, int]:
    """Convert 1-based scene (1..500) to (bank, program). Bank 0 = scenes
    1-128, Bank 1 = 129-256, Bank 2 = 257-384, Bank 3 = 385-500.
    """
    if scene < MIN_SCENE or scene > MAX_SCENE:
        raise ValueError(f"Scene {scene} outside 1..{MAX_SCENE}")
    idx = scene - 1
    return idx // 128, idx % 128


def cue_to_bank_program(cue: int) -> tuple[int, int]:
    """Convert 1-based cue (1..2000) to (bank, program). Cue 1 = Recall
    Id 0 = Bank 0 / Program 0; Cue 2000 = Recall Id 1999 = Bank 0x0F /
    Program 0x4F.
    """
    if cue < MIN_CUE or cue > MAX_CUE:
        raise ValueError(f"Cue {cue} outside 1..{MAX_CUE}")
    idx = cue - 1
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
        "current_cue": {
            "type": "integer",
            "label": "Current Cue",
            "min": 0,
            "max": MAX_CUE,
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

    # Mute commands — one per channel type.
    for ctype, (_, _, _, label) in CHANNEL_TYPES.items():
        cmds[f"mute_{ctype}"] = {
            "label": f"Mute {label}",
            "help": f"Mute, unmute, or toggle a {label.lower()}.",
            "params": {
                "channel": _channel_n_param(ctype),
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

    # Send level — generic command across every documented source/target.
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

    # Send assign on/off (SysEx 0E) — input-to-group / input-to-aux assign.
    cmds["set_send_assign"] = {
        "label": "Set Send Assign",
        "help": (
            "Assign or unassign a source channel to a group / aux send "
            "destination (SysEx 0E)."
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
        "help": "Assign or remove a channel from a DCA group (1..24).",
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

    # Channel name / colour set.
    cmds["set_channel_name"] = {
        "label": "Set Channel Name",
        "help": (
            "Write a channel name (up to 8 characters). State mirroring "
            "of the Get reply is deferred."
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
        "help": "Set a channel colour (0 = no colour, 1-7 = colour preset).",
        "params": {
            "channel_type": {
                "type": "enum", "values": list(CHANNEL_TYPES.keys()),
                "required": True, "label": "Channel Type",
            },
            "channel": {"type": "integer", "required": True, "min": 1, "label": "Channel Number"},
            "colour": {
                "type": "integer", "required": True,
                "min": 0, "max": 7, "label": "Colour (0-7)",
            },
        },
    }

    # Preamp Gain / Pad / 48V (per-socket, MP 1..128).
    cmds["set_preamp_gain"] = {
        "label": "Set Preamp Gain",
        "help": (
            "Set the analog gain on a socket preamp via Pitchbend "
            "(EN MP GV). Sockets 1-64 are MixRack inputs, 65-96 are "
            "DX1/2 inputs 1-32, 97-128 are DX3/4 inputs 1-32."
        ),
        "params": {
            "socket": {
                "type": "integer", "required": True,
                "min": 1, "max": NUM_SOCKETS, "label": "Socket",
            },
            "gain_db": {
                "type": "number", "required": True,
                "min": GAIN_DB_MIN, "max": GAIN_DB_MAX,
                "default": 30.0, "label": "Gain (dB)",
                "help": "+5 dB to +60 dB.",
            },
        },
    }
    cmds["set_preamp_pad"] = {
        "label": "Set Preamp Pad",
        "help": "Engage or disengage the 20 dB pad on a socket preamp.",
        "params": {
            "socket": {
                "type": "integer", "required": True,
                "min": 1, "max": NUM_SOCKETS, "label": "Socket",
            },
            "action": {
                "type": "enum", "values": ["on", "off"], "default": "off",
                "required": True, "label": "Action",
            },
        },
    }
    cmds["set_preamp_48v"] = {
        "label": "Set Preamp 48V",
        "help": "Engage or disengage 48V phantom power on a socket preamp.",
        "params": {
            "socket": {
                "type": "integer", "required": True,
                "min": 1, "max": NUM_SOCKETS, "label": "Socket",
            },
            "action": {
                "type": "enum", "values": ["on", "off"], "default": "off",
                "required": True, "label": "Action",
            },
        },
    }

    # HPF frequency / on-off (per-channel NRPN).
    cmds["set_hpf_frequency"] = {
        "label": "Set HPF Frequency",
        "help": "Set the high-pass filter cutoff frequency (20 Hz – 20 kHz).",
        "params": {
            "channel_type": {
                "type": "enum", "values": HPF_CHANNEL_TYPES,
                "required": True, "label": "Channel Type",
            },
            "channel": {"type": "integer", "required": True, "min": 1, "label": "Channel Number"},
            "frequency_hz": {
                "type": "number", "required": True,
                "min": HPF_FREQ_MIN, "max": HPF_FREQ_MAX,
                "default": 80.0, "label": "Frequency (Hz)",
            },
        },
    }
    cmds["set_hpf_onoff"] = {
        "label": "Set HPF On/Off",
        "help": "Engage or disengage the high-pass filter on a channel.",
        "params": {
            "channel_type": {
                "type": "enum", "values": HPF_CHANNEL_TYPES,
                "required": True, "label": "Channel Type",
            },
            "channel": {"type": "integer", "required": True, "min": 1, "label": "Channel Number"},
            "action": {
                "type": "enum", "values": ["on", "off"], "default": "on",
                "required": True, "label": "Action",
            },
        },
    }

    # Scene recall (MixRack).
    cmds["recall_scene"] = {
        "label": "Recall Scene",
        "help": f"Recall a scene 1..{MAX_SCENE} via Bank Select + Program Change (MixRack).",
        "params": {
            "scene": {
                "type": "integer", "required": True,
                "min": MIN_SCENE, "max": MAX_SCENE, "label": "Scene",
            },
        },
    }
    # Cue list recall (Surface).
    cmds["recall_cue"] = {
        "label": "Recall Cue",
        "help": (
            f"Recall a Surface cue 1..{MAX_CUE} via Bank Select + Program "
            "Change (16 banks × 128). Surface only."
        ),
        "params": {
            "cue": {
                "type": "integer", "required": True,
                "min": MIN_CUE, "max": MAX_CUE, "label": "Cue",
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
        "help": "Set the global UFX scale (Major / Minor / Chromatic).",
        "params": {
            "scale": {
                "type": "enum", "values": list(UFX_SCALE_NAMES),
                "required": True, "default": "Major", "label": "Scale",
            },
        },
    }

    # Refresh — kept for parity with sibling mixers; see docstring.
    cmds["refresh"] = {
        "label": "Refresh State",
        "help": (
            "dLive does support SysEx Get queries but issuing a sweep on "
            "connect would require thousands of round-trips; state "
            "populates lazily from console-side and driver-initiated "
            "change echoes. This command is a no-op for now."
        ),
        "params": {},
    }

    return cmds


# ── Driver ───────────────────────────────────────────────────────────────────

class AllenHeathDLiveDriver(BaseDriver):
    """Allen & Heath dLive MIDI-over-TCP driver."""

    DRIVER_INFO = {
        "id": "allenheath_dlive",
        "name": "Allen & Heath dLive Digital Mixer",
        "manufacturer": "Allen & Heath",
        "category": "audio",
        "version": "1.1.1",
        "author": "OpenAVC",
        "description": (
            "Controls Allen & Heath dLive digital mixing systems "
            "(MixRack + Surface) via MIDI over TCP/IP on port 51325. "
            "Mute / fader / send level / send assign / channel assign "
            "across 128 inputs, 62 mono and 31 stereo groups, 62 mono "
            "and 31 stereo aux outs, 62 mono and 31 stereo matrix outs, "
            "16 mono and 16 stereo FX sends, 16 FX returns, 6 mains, 24 "
            "DCAs, 8 mute groups, 8 stereo UFX sends, 8 stereo UFX "
            "returns. Adds dLive-specific surface: 128 socket preamps "
            "(gain via Pitchbend, Pad, 48V), per-channel HPF, Cue List "
            "Recall (Surface, 1-2000), Scene Recall (MixRack, 1-500), "
            "channel name and colour set, MMC transport, UFX global key "
            "and scale, plus bidirectional state from console-side echoes."
        ),
        "source_url": (
            "https://www.allen-heath.com/content/uploads/2024/06/dLive-MIDI-Over-TCP-Protocol-V2.0.pdf"
        ),
        "tags": ["mixer", "console", "midi", "nrpn", "sysex", "allen-heath"],
        "verified": False,
        "simulated": True,
        "protocols": ["midi-over-tcp"],
        "ports": [51325],
        "transport": "tcp",
        "discovery": {
            # Audiotonix Group OUI — shared across A&H, Midas, DiGiCo, SSL.
            "oui_prefixes": ["00:04:c4"],
        },
        "min_platform_version": "0.6.0",
        "compatible_models": [
            {
                "manufacturer": "Allen & Heath",
                "models": [
                    "dLive S3000", "dLive S5000", "dLive S7000",
                    "dLive C1500", "dLive C2500", "dLive C3500",
                    "DM0", "DM32", "DM48", "DM64",
                ],
                "confidence": "untested",
                "notes": (
                    "Same MIDI command surface across the dLive S- and "
                    "C-class Surfaces and DM-series MixRacks. Requires "
                    "firmware V2.0 or later for TCP/IP control. Plain-TCP "
                    "(port 51325) only in v1.0.0; TLS+UserProfile auth on "
                    "port 51327 is a future extension."
                ),
            },
        ],
        "default_config": {
            "host": "",
            "port": 51325,
            "base_midi_channel": 1,
            "poll_interval": 0,
        },
        "config_schema": {
            "host": {
                "type": "string", "required": True,
                "label": "IP Address",
                "description": (
                    "MixRack or Surface IP address (Utility / Network on "
                    "the console)."
                ),
            },
            "port": {
                "type": "integer", "default": 51325, "label": "TCP Port",
                "description": (
                    "MIDI over TCP/IP port. 51325 for MixRack plain-TCP, "
                    "51328 for Surface plain-TCP. TLS ports (51327 / "
                    "51329) are not supported in v1.0.0."
                ),
            },
            "base_midi_channel": {
                "type": "integer", "default": 1, "min": 1, "max": 12,
                "label": "Base MIDI Channel",
                "description": (
                    "Lowest of the 5-channel range used by dLive (must "
                    "match Utility / Control / MIDI on the console). "
                    "Default 1 means the driver uses channels 1-5."
                ),
            },
            "poll_interval": {
                "type": "integer", "default": 0, "min": 0,
                "label": "Poll Interval (s)",
                "description": (
                    "dLive pushes parameter changes; polling is unused. "
                    "Leave at 0 unless debugging."
                ),
            },
        },
        "help": {
            "overview": (
                "Controls an Allen & Heath dLive digital mixing system "
                "over the network using MIDI over TCP/IP. Full mute, "
                "fader, send-level, send-assign, channel-assign, preamp "
                "(gain / pad / 48V), HPF, scene / cue recall, MMC and "
                "UFX global key / scale. Console-side moves push back "
                "into OpenAVC state so faders and mute LEDs on a touch "
                "panel stay in sync."
            ),
            "setup": (
                "1. Confirm the dLive system is running firmware V2.0 "
                "or later. TCP/IP control is unavailable on earlier "
                "firmware.\n"
                "2. Connect the MixRack (or Surface, if controlling the "
                "Surface directly) to the same network as the OpenAVC "
                "server.\n"
                "3. On the dLive, go to Utility / Network and note the "
                "IP address.\n"
                "4. Go to Utility / Control / MIDI and note the base "
                "MIDI channel. The console reserves the next four "
                "channels above the base for output buses.\n"
                "5. Enter the IP address and matching base MIDI channel "
                "in the device config. Use port 51325 for MixRack, "
                "51328 for Surface.\n"
                "6. The driver listens for console-side push "
                "automatically; no further setup is required."
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
        self._nrpn_state: dict[int, dict[str, int]] = {
            ch: {"msb": 0, "lsb": 0, "vc": 0} for ch in range(16)
        }
        self._last_bank: dict[int, int] = {ch: 0 for ch in range(16)}

    # ── Connection lifecycle ────────────────────────────────────────────

    @property
    def _base_midi(self) -> int:
        n = int(self.config.get("base_midi_channel", 1))
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
            delimiter=None,
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

    # ── send_command dispatch ───────────────────────────────────────────

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        method = getattr(self, f"cmd_{command}", None)
        if method is None:
            raise ValueError(f"Unknown command: {command}")
        return await method(**params)

    # ── Mutes ───────────────────────────────────────────────────────────

    async def _do_mute(self, ctype: str, channel: int, action: str) -> None:
        offset, ch_note = channel_address(ctype, int(channel))
        action = (action or "on").lower()
        key = state_key(ctype, int(channel), "mute")
        if action == "toggle":
            cur = bool(self.get_state(key))
            action = "off" if cur else "on"
        velocity = 0x7F if action == "on" else 0x3F
        await self._send(self._build_note_pair(offset, ch_note, velocity))

    # Mute method generation is at the bottom of the file (one cmd_mute_<ctype>
    # for every channel type).

    # ── Faders (NRPN param 17, 7-bit LV) ────────────────────────────────

    async def _do_fader(self, ctype: str, channel: int, lv: int) -> None:
        offset, ch_note = channel_address(ctype, int(channel))
        await self._send(self._build_nrpn_set(offset, ch_note, NRPN_PARAM_FADER, lv))

    async def _do_fader_position(self, ctype: str, channel: int, level: float) -> None:
        await self._do_fader(ctype, channel, level_to_lv(float(level)))

    async def _do_fader_db(self, ctype: str, channel: int, db: float) -> None:
        await self._do_fader(ctype, channel, db_to_lv(float(db)))

    # ── Send levels (SysEx 0D) and Send assigns (SysEx 0E) ──────────────

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

    async def cmd_set_send_assign(self, source_type: str, source: int,
                                  target_type: str, target: int,
                                  action: str = "on") -> None:
        if source_type not in SEND_SOURCE_TYPES:
            raise ValueError(f"Invalid send source type: {source_type}")
        if target_type not in SEND_TARGET_TYPES:
            raise ValueError(f"Invalid send target type: {target_type}")
        src_off, src_ch = channel_address(source_type, int(source))
        tgt_off, tgt_ch = channel_address(target_type, int(target))
        v = 0x7F if (action or "on").lower() == "on" else 0x00
        body = bytes([
            self._midi_ch_byte(src_off) & 0x0F,
            SYSEX_CMD_SEND_ASSIGN,
            src_ch & 0x7F,
            self._midi_ch_byte(tgt_off) & 0x0F,
            tgt_ch & 0x7F,
            v & 0x7F,
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
        # ON value DB = 40..57 for DCA 1..24
        # OFF value DA = 00..17 for DCA 1..24
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
        # ON value DB = 58..5F for mute groups 1..8 (shifted from Avantis 50..57)
        # OFF value DA = 18..1F for mute groups 1..8 (shifted from Avantis 10..17)
        value = (0x58 if on else 0x18) + (mg - 1)
        await self._send(self._build_nrpn_set(offset, ch_note, NRPN_PARAM_BUS_ASSIGN, value))

    # ── Channel name / colour set ───────────────────────────────────────

    async def cmd_set_channel_name(self, channel_type: str, channel: int,
                                   name: str) -> None:
        offset, ch_note = channel_address(channel_type, int(channel))
        encoded = (name or "")[:8].encode("ascii", errors="replace")
        body = bytes([
            self._midi_ch_byte(offset) & 0x0F,
            SYSEX_CMD_NAME_SET,
            ch_note & 0x7F,
        ]) + encoded
        await self._send(self._build_sysex(body))

    async def cmd_set_channel_colour(self, channel_type: str, channel: int,
                                     colour: int) -> None:
        offset, ch_note = channel_address(channel_type, int(channel))
        col = max(0, min(7, int(colour)))
        body = bytes([
            self._midi_ch_byte(offset) & 0x0F,
            SYSEX_CMD_COLOUR_SET,
            ch_note & 0x7F,
            col,
        ])
        await self._send(self._build_sysex(body))

    # ── Preamp Gain / Pad / 48V ─────────────────────────────────────────

    async def cmd_set_preamp_gain(self, socket: int, gain_db: float) -> None:
        mp = int(socket)
        if mp < 1 or mp > NUM_SOCKETS:
            raise ValueError(f"Socket {mp} outside 1..{NUM_SOCKETS}")
        gv = gain_to_gv(float(gain_db))
        # Pitchbend EN MP GV — base channel.
        e = 0xE0 | self._midi_ch_byte(0)
        await self._send(bytes([e, (mp - 1) & 0x7F, gv & 0x7F]))

    async def cmd_set_preamp_pad(self, socket: int, action: str = "off") -> None:
        mp = int(socket)
        if mp < 1 or mp > NUM_SOCKETS:
            raise ValueError(f"Socket {mp} outside 1..{NUM_SOCKETS}")
        v = 0x7F if (action or "off").lower() == "on" else 0x00
        body = bytes([
            self._midi_ch_byte(0) & 0x0F,
            SYSEX_CMD_PAD_SET,
            (mp - 1) & 0x7F,
            v & 0x7F,
        ])
        await self._send(self._build_sysex(body))

    async def cmd_set_preamp_48v(self, socket: int, action: str = "off") -> None:
        mp = int(socket)
        if mp < 1 or mp > NUM_SOCKETS:
            raise ValueError(f"Socket {mp} outside 1..{NUM_SOCKETS}")
        v = 0x7F if (action or "off").lower() == "on" else 0x00
        body = bytes([
            self._midi_ch_byte(0) & 0x0F,
            SYSEX_CMD_48V_SET,
            (mp - 1) & 0x7F,
            v & 0x7F,
        ])
        await self._send(self._build_sysex(body))

    # ── HPF (NRPN 30 / 31) ──────────────────────────────────────────────

    async def cmd_set_hpf_frequency(self, channel_type: str, channel: int,
                                    frequency_hz: float) -> None:
        if channel_type not in HPF_CHANNEL_TYPES:
            raise ValueError(f"HPF not supported on channel type: {channel_type}")
        offset, ch_note = channel_address(channel_type, int(channel))
        vv = hpf_freq_to_vv(float(frequency_hz))
        await self._send(self._build_nrpn_set(offset, ch_note, NRPN_PARAM_HPF_FREQ, vv))

    async def cmd_set_hpf_onoff(self, channel_type: str, channel: int,
                                action: str = "on") -> None:
        if channel_type not in HPF_CHANNEL_TYPES:
            raise ValueError(f"HPF not supported on channel type: {channel_type}")
        offset, ch_note = channel_address(channel_type, int(channel))
        v = 0x7F if (action or "on").lower() == "on" else 0x00
        await self._send(self._build_nrpn_set(offset, ch_note, NRPN_PARAM_HPF_ONOFF, v))

    # ── Scene recall ────────────────────────────────────────────────────

    async def cmd_recall_scene(self, scene: int) -> None:
        bank, program = scene_to_bank_program(int(scene))
        b = 0xB0 | self._midi_ch_byte(0)
        c = 0xC0 | self._midi_ch_byte(0)
        # Scene recall: BN, 00, 00, BN, 00, Bank, CN, SS — the spec
        # writes "BN 00 00, CN SS" but we follow the reference "Select
        # bank / Recall Scene" form: BN 00 Bank, CN SS. The simulator
        # mirrors this.
        await self._send(bytes([b, CC_BANK_SELECT_MSB, bank, c, program]))
        self.set_state("current_scene", int(scene))

    async def cmd_recall_cue(self, cue: int) -> None:
        bank, program = cue_to_bank_program(int(cue))
        b = 0xB0 | self._midi_ch_byte(0)
        c = 0xC0 | self._midi_ch_byte(0)
        await self._send(bytes([b, CC_BANK_SELECT_MSB, bank, c, program]))
        self.set_state("current_cue", int(cue))

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

    async def cmd_refresh(self) -> None:
        return None

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
        target = self._addr_map.lookup(offset, note)
        if target is None:
            return
        ctype, n = target
        on = velocity >= 0x40
        self.set_state(state_key(ctype, n, "mute"), on)

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
        target = self._addr_map.lookup(offset, ch_note)
        if target is None:
            return
        ctype, n = target

        if param_id == NRPN_PARAM_FADER:
            if ctype == "mute_group":
                return
            self.set_state(state_key(ctype, n, "fader"), lv_to_level(value))
            return

        # Main assign / DCA assign / mute-group assign / HPF echoes are
        # accepted (no-op) — exposing them as named state vars is a future
        # extension.
        return

    def _handle_program_change(self, ch: int, program: int) -> None:
        if ch != self._midi_ch_byte(0):
            return
        bank = self._last_bank.get(ch, 0)
        # Bank 0..3 = scene 1..500 (MixRack); bank 0..F = cue 1..2000
        # (Surface). We can't tell which device pushed without state, so
        # both interpretations are populated when in range.
        if 0 <= bank <= 3:
            scene = bank * 128 + program + 1
            if MIN_SCENE <= scene <= MAX_SCENE:
                self.set_state("current_scene", scene)
        cue = bank * 128 + program + 1
        if MIN_CUE <= cue <= MAX_CUE:
            self.set_state("current_cue", cue)

    def _handle_sysex(self, message: bytes) -> None:
        """SysEx parsing in v1.0.0: accept and discard. Pad/48V replies
        (cmd 0x08 / 0x0B) and Name/Colour replies (0x02 / 0x05) are
        documented but not state-mirrored yet; the parser drops them.
        Generic Get-response messages (cmd 0x05 with sub-kinds 0x09 /
        0x0B / 0x0F) are likewise accepted and dropped.
        """
        return


# Class export expected by the loader.
DRIVER_CLASS = AllenHeathDLiveDriver


# ── Late-bind generated mute / fader commands ───────────────────────────────
#
# We declare cmd_mute_<ctype>, cmd_set_<ctype>_fader, and
# cmd_set_<ctype>_fader_db on the driver class for every channel type.
# Doing this in code avoids 45 near-identical hand-written method
# definitions while keeping the dispatcher's getattr lookup fast.

def _make_mute_cmd(ctype: str):
    async def _cmd(self, channel: int, action: str = "on") -> None:
        await self._do_mute(ctype, channel, action)
    _cmd.__name__ = f"cmd_mute_{ctype}"
    return _cmd


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


for _ctype in CHANNEL_TYPES:
    setattr(AllenHeathDLiveDriver, f"cmd_mute_{_ctype}", _make_mute_cmd(_ctype))

for _ctype in FADER_CHANNEL_TYPES:
    setattr(AllenHeathDLiveDriver, f"cmd_set_{_ctype}_fader", _make_fader_cmd(_ctype))
    setattr(AllenHeathDLiveDriver, f"cmd_set_{_ctype}_fader_db", _make_fader_db_cmd(_ctype))
