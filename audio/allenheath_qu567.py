"""
OpenAVC Allen & Heath Qu-5 / Qu-6 / Qu-7 Driver.

Controls the current Allen & Heath Qu generation (Qu-5, Qu-6, Qu-7 and the
Dante -5D / -6D / -7D variants) over MIDI-over-TCP/IP on port 51325. The wire
format is standard MIDI 1.0 carried on a raw TCP stream: mixer parameters are
NRPN sequences, scene recall is Bank Select + Program Change, and Soft Keys
are Note On/Off.

This is NOT the same protocol as the original Qu family (Qu-16 / 24 / 32 /
Pac / SB, driver ``allenheath_qu``). That generation identifies itself with an
Allen & Heath SysEx "All-Call" and floods its whole state on connect. A Qu-5
ignores that message completely and answers nothing, so the older driver
cannot drive one: it falls back to a Qu-16 channel map that does not exist
here and then drops the link on its own silence watchdog. The two drivers are
deliberately separate.

What makes this driver first-class
-----------------------------------
* **The desk enumerates itself.** The protocol carries no model identify and
  no channel names, so a static roster is the obvious approach and it is
  wrong: a Qu mix bus is configurable as an Aux *or* a Group, mixes and
  matrices can be mono or stereo pairs, and what is addressable changes with
  it. But a Get for a parameter the desk does not have is answered with
  *silence*, and one it does have always answers. So on connect the driver
  probes the roster-defining addresses and registers exactly the channels
  this desk really has. That covers all three models and every bus
  configuration without asking the integrator to describe their console.
* **Channels as typed child entities.** Every input, stereo input, mix,
  matrix, FX send/return, DCA and mute group is a child with live mute /
  level / dB / pan state. Per-channel commands take a channel picker rather
  than a free-typed number.
* **Writes are confirmed, not assumed.** A&H consoles do not echo changes
  they receive over MIDI, so the sibling SQ driver mirrors what it sent. This
  desk answers Gets reliably, so every write is followed by a Get and state
  reflects what the console actually took. That matters here because Audio
  Taper quantises a fader to 64-count steps: the value read back is often not
  the value sent, and mirroring would leave the panel a step off forever.
* **Get-probe liveness.** The console never speaks unprompted, so a desk that
  vanished without closing the socket would otherwise stay "online" forever.
  The driver probes the LR master on a timer and awaits the reply; two misses
  force a typed ``no_response`` fault and a reconnect.

Push vs poll:
    Hybrid. A console-side move transmits the matching NRPN and the driver
    fans it into child state -- verified on a Qu-5 by moving faders and mute
    keys on the surface: input sends, the LR master and channel mutes all
    arrived correctly addressed, under 100 ms, decoding to the right channel
    and the right dB. The console streams a moving fader at roughly 100
    updates a second, one per 64-count step.

    Polling is still the backstop rather than an afterthought, because this
    generation sends NOTHING unprompted -- no Active Sense, no idle chatter --
    so a missed window has nothing to correct it. The Get sweep on connect and
    the periodic re-sweep close that gap; push keeps state live between them.

Format choice:
    Python rather than YAML because the wire format is binary (MIDI bytes,
    NRPN sequences with running status), the roster is discovered at runtime
    by probing rather than declared, the parameter map is computed from
    source/destination offsets rather than enumerated, and each inbound NRPN
    has to fan out to the right child state key through a reverse lookup.
    None of that fits ConfigurableDriver.

Models covered:
    Qu-5, Qu-6, Qu-7 and the Dante -5D / -6D / -7D variants. All share the
    same processing and the same MIDI surface -- 38 inputs to mix, 12 mix
    buses, 4 matrices, 6 stereo FX engines -- and differ only in local I/O
    and fader count, which MIDI does not address. The roster probe means no
    model needs to be selected.

Scope notes:
    - **Fader law matters.** The desk's NRPN Fader Law (UTILITY > General >
      MIDI) changes how a level value maps to dB, and the protocol offers no
      way to read the setting, so it is a config field that must match the
      console. Position (0..1) round-trips correctly either way; only the dB
      readout depends on it. A&H recommend Linear Taper for third-party
      control and so do we -- it is finer-grained and its dB mapping is
      exact, where the published Audio Taper table is explicitly
      "Approximate" and was measured on hardware to disagree with the desk by
      up to one 64-count step (about 0.15 dB).
    - MIDI Show Control is supported by the console in Cue List mode, but the
      protocol document only says "standard MIDI Show Control messages" and
      specifies neither the device ID nor the command encoding. Rather than
      guess at it, this driver exposes scene recall (which works in every
      Scene Manager mode) and leaves MSC out. See the shipped record.
    - MIDI fader strips, Soft Rotaries, MMC transport and the DAW-control
      channel are end-user surface features rather than integrator-
      addressable mixer functions, and are intentionally not exposed.
    - The console has no channel-name access, so children carry positional
      labels ("Input 1") that the user renames in the project. A label the
      user sets is never overwritten.
    - Only ONE MIDI-over-TCP client at a time. The console accepts a second
      TCP connection and then immediately drops it (measured), so Qu-Pad or
      another control system holding the slot will keep OpenAVC out, and
      vice versa.

Source:
    https://www.allen-heath.com/content/uploads/2025/06/Qu567_MIDI_Protocol_Iss2.pdf
    (MIDI Protocol Issue 2, Qu-5/6/7 firmware V1.1 or later)
"""

from __future__ import annotations

import asyncio
from typing import Any

from openavc.drivers.base import BaseDriver
from openavc.utils.logger import get_logger

log = get_logger(__name__)


DEFAULT_PORT = 51325

# ── Channel counts (the addressable maximum; the roster probe narrows it) ────
NUM_INPUTS = 32          # Ip1..Ip32 mono
NUM_STEREO_INPUTS = 2    # ST1, ST2
NUM_GROUPS = 12          # Grp1..Grp12 (send sources; see _GROUP note below)
NUM_MIXES = 12           # MIX1..MIX12
NUM_MATRICES = 4         # Mtx1..Mtx4, addressed in stereo pairs (1 and 3)
NUM_FX_SENDS = 4
NUM_FX_RETURNS = 6
NUM_DCAS = 8
NUM_MUTE_GROUPS = 8
NUM_SOFTKEYS = 16        # 8 on the surface + 8 more through QuMixPad

MIN_SCENE = 1
MAX_SCENE = 300

VALUE_MAX = 0x3FFF       # 16383, the 14-bit NRPN ceiling

LEVEL_MIN, LEVEL_MAX = 0.0, 1.0
PAN_LEFT, PAN_CENTER, PAN_RIGHT = -1.0, 0.0, 1.0

# dB endpoints of both taper tables. -inf is represented by DB_MIN.
DB_MIN, DB_MAX = -90.0, 10.0


# ── Source offsets ───────────────────────────────────────────────────────────
#
# Every channel that can be a mute target OR a send source has one offset, and
# that single number drives all four planes. Verified address-by-address
# against a real Qu-5 (2026-09-01): a Get answers for an offset the desk has
# and is silent for one it does not.

def _src_input(n: int) -> int:
    return n - 1                       # Ip1..32   -> 0x00..0x1F


def _src_stereo(n: int) -> int:
    return 0x20 + (n - 1) * 2          # ST1 -> 0x20, ST2 -> 0x22 (right half unused)


SRC_USB = 0x24                         # USB stereo return


def _src_group(n: int) -> int:
    return 0x30 + (n - 1)              # Grp1..12  -> 0x30..0x3B


def _src_fx_return(n: int) -> int:
    return 0x3C + (n - 1)              # FX1..6Rtn -> 0x3C..0x41


SRC_LR = 0x44


def _src_mix(n: int) -> int:
    return 0x45 + (n - 1)              # MIX1..12  -> 0x45..0x50


def _src_fx_send(n: int) -> int:
    return 0x51 + (n - 1)              # FX1..4Snd -> 0x51..0x54


def _src_matrix(n: int) -> int:
    return 0x55 + (n - 1)              # Mtx1..4   -> 0x55..0x58


# ── Address planes ───────────────────────────────────────────────────────────
#
# MSB and LSB are two independent 7-bit bytes, not a packed 14-bit number, so a
# flat offset rolls the LSB past 0x7F into MSB+1. Four planes share one base
# map, separated by a constant MSB offset -- confirmed by sweeping the whole
# address space on hardware and finding the level, pan and assign planes
# structurally identical.

PLANE_MUTE = 0x00
PLANE_LEVEL = 0x40
PLANE_PAN = 0x50         # = PLANE_LEVEL + 0x10
PLANE_ASSIGN = 0x60      # = PLANE_LEVEL + 0x20

MSB_DCA_MUTE = 0x02
MSB_MGRP_MUTE = 0x04


def _addr(msb_base: int, offset: int) -> tuple[int, int]:
    """(MSB, LSB) for a base MSB plus a flat offset that may roll the LSB."""
    return (msb_base + (offset >> 7)) & 0x7F, offset & 0x7F


def mute_addr(src_offset: int) -> tuple[int, int]:
    """Mute for any channel that has one. Groups do not (see _GROUP note)."""
    return _addr(PLANE_MUTE, src_offset)


def mute_dca(n: int) -> tuple[int, int]:
    return _addr(MSB_DCA_MUTE, n - 1)              # DCA1..8   -> 02 00..07


def mute_mgrp(n: int) -> tuple[int, int]:
    return _addr(MSB_MGRP_MUTE, n - 1)             # MGRP1..8  -> 04 00..07


def to_lr(src_offset: int, plane: int = PLANE_LEVEL) -> tuple[int, int]:
    """A source's send to the LR mix.  Ip1 -> 40 00."""
    return _addr(plane, src_offset)


def to_mix(src_offset: int, mix: int, plane: int = PLANE_LEVEL) -> tuple[int, int]:
    """A source's send to MIX <mix>.  Ip1->MIX1 = 40 44, Grp1->MIX1 = 45 04,
    FX1Rtn->MIX1 = 46 14 -- one formula for every source class."""
    return _addr(plane, 0x44 + src_offset * NUM_MIXES + (mix - 1))


def to_fx_send(src_offset: int, fx: int, plane: int = PLANE_LEVEL) -> tuple[int, int]:
    """A source's send to FX<fx>Snd.  Ip1->FX1Snd = 4C 14, Grp1 = 4D 54,
    FX1Rtn = 4E 04."""
    return _addr(plane + 0x0C, 0x14 + src_offset * NUM_FX_SENDS + (fx - 1))


def to_matrix(mix_index: int, mtx: int, plane: int = PLANE_LEVEL) -> tuple[int, int]:
    """LR or a mix sending into a matrix. ``mix_index`` is 0 for LR and 1..12
    for MIX1..12.  LR->Mtx1 = 4E 24, MIX1->Mtx1 = 4E 27."""
    return _addr(plane + 0x0E, 0x24 + mix_index * 3 + (mtx - 1))


# Output-master offsets within the 0x4F / 0x5F plane.
_MASTER_LR = 0x00
_MASTER_MIX = 0x01       # MIX1..12  -> 0x01..0x0C
_MASTER_FX_SEND = 0x0D   # FX1..4Snd -> 0x0D..0x10
_MASTER_MATRIX = 0x11    # Mtx1..4   -> 0x11..0x14
_MASTER_DCA = 0x20       # DCA1..8   -> 0x20..0x27 (level only; no pan/assign)


def master_addr(offset: int, plane: int = PLANE_LEVEL) -> tuple[int, int]:
    return _addr(plane + 0x0F, offset)


def master_lr(plane: int = PLANE_LEVEL) -> tuple[int, int]:
    return master_addr(_MASTER_LR, plane)


def master_mix(n: int, plane: int = PLANE_LEVEL) -> tuple[int, int]:
    return master_addr(_MASTER_MIX + (n - 1), plane)


def master_fx_send(n: int, plane: int = PLANE_LEVEL) -> tuple[int, int]:
    return master_addr(_MASTER_FX_SEND + (n - 1), plane)


def master_matrix(n: int, plane: int = PLANE_LEVEL) -> tuple[int, int]:
    return master_addr(_MASTER_MATRIX + (n - 1), plane)


def master_dca(n: int) -> tuple[int, int]:
    """DCAs have a fader but no pan and no assign."""
    return master_addr(_MASTER_DCA + (n - 1), PLANE_LEVEL)


# ── Fader-law tables ─────────────────────────────────────────────────────────
#
# Straight from the protocol document's reference tables, written as the
# published (VC, VF) hex pairs so each row can be checked against the PDF by
# eye. Audio Taper is the document's own "Approximate" table; Linear Taper is
# exactly linear in dB (measured at ~118.7 counts/dB) and is the law A&H
# recommend for third-party control.

_AUDIO_TAPER: tuple[tuple[float, int, int], ...] = (
    (-89, 0x01, 0x40), (-85, 0x02, 0x00), (-80, 0x02, 0x40), (-75, 0x03, 0x40),
    (-70, 0x04, 0x00), (-65, 0x05, 0x00), (-60, 0x06, 0x00), (-55, 0x07, 0x00),
    (-50, 0x08, 0x00), (-45, 0x0C, 0x00), (-40, 0x0F, 0x40), (-38, 0x12, 0x40),
    (-36, 0x15, 0x40), (-35, 0x17, 0x00), (-34, 0x19, 0x00), (-33, 0x1A, 0x40),
    (-32, 0x1C, 0x00), (-31, 0x1D, 0x40), (-30, 0x1F, 0x00), (-29, 0x20, 0x40),
    (-28, 0x22, 0x00), (-27, 0x23, 0x40), (-26, 0x25, 0x00), (-25, 0x26, 0x40),
    (-24, 0x28, 0x40), (-23, 0x2A, 0x00), (-22, 0x2B, 0x40), (-21, 0x2D, 0x00),
    (-20, 0x2E, 0x40), (-19, 0x30, 0x00), (-18, 0x31, 0x40), (-17, 0x33, 0x00),
    (-16, 0x34, 0x40), (-15, 0x36, 0x00), (-14, 0x38, 0x00), (-13, 0x39, 0x40),
    (-12, 0x3B, 0x00), (-11, 0x3C, 0x40), (-10, 0x3E, 0x00), (-9, 0x41, 0x40),
    (-8, 0x44, 0x40), (-7, 0x48, 0x00), (-6, 0x4B, 0x00), (-5, 0x4E, 0x40),
    (-4, 0x52, 0x40), (-3, 0x56, 0x40), (-2, 0x5A, 0x00), (-1, 0x5E, 0x00),
    (0, 0x62, 0x00), (1, 0x65, 0x40), (2, 0x69, 0x00), (3, 0x6C, 0x40),
    (4, 0x70, 0x00), (5, 0x73, 0x40), (6, 0x75, 0x40), (7, 0x78, 0x00),
    (8, 0x7A, 0x40), (9, 0x7D, 0x00), (10, 0x7F, 0x40),
)

_LINEAR_TAPER: tuple[tuple[float, int, int], ...] = (
    (-89, 0x24, 0x16), (-85, 0x27, 0x71), (-80, 0x2C, 0x42), (-75, 0x31, 0x14),
    (-70, 0x35, 0x65), (-65, 0x3A, 0x37), (-60, 0x3F, 0x09), (-55, 0x43, 0x5A),
    (-50, 0x48, 0x2C), (-45, 0x4C, 0x7D), (-40, 0x51, 0x4F), (-38, 0x53, 0x3C),
    (-36, 0x55, 0x2A), (-35, 0x56, 0x21), (-34, 0x57, 0x17), (-33, 0x58, 0x0E),
    (-32, 0x59, 0x05), (-31, 0x59, 0x7C), (-30, 0x5A, 0x72), (-29, 0x5B, 0x69),
    (-28, 0x5C, 0x60), (-27, 0x5D, 0x56), (-26, 0x5E, 0x4D), (-25, 0x5F, 0x44),
    (-24, 0x60, 0x3B), (-23, 0x61, 0x31), (-22, 0x62, 0x28), (-21, 0x63, 0x1F),
    (-20, 0x64, 0x16), (-19, 0x65, 0x0C), (-18, 0x66, 0x03), (-17, 0x66, 0x7A),
    (-16, 0x67, 0x70), (-15, 0x68, 0x67), (-14, 0x69, 0x5E), (-13, 0x6A, 0x55),
    (-12, 0x6B, 0x4B), (-11, 0x6C, 0x42), (-10, 0x6D, 0x39), (-9, 0x6E, 0x2F),
    (-8, 0x6F, 0x26), (-7, 0x70, 0x1D), (-6, 0x71, 0x14), (-5, 0x72, 0x0A),
    (-4, 0x73, 0x01), (-3, 0x73, 0x78), (-2, 0x74, 0x6F), (-1, 0x75, 0x65),
    (0, 0x76, 0x5C), (1, 0x77, 0x53), (2, 0x78, 0x49), (3, 0x79, 0x40),
    (4, 0x7A, 0x37), (5, 0x7B, 0x2E), (6, 0x7C, 0x24), (7, 0x7D, 0x1B),
    (8, 0x7E, 0x12), (9, 0x7F, 0x08), (10, 0x7F, 0x7F),
)

FADER_LAWS = ("audio", "linear")


def _curve(law: str) -> tuple[tuple[float, int], ...]:
    table = _LINEAR_TAPER if law == "linear" else _AUDIO_TAPER
    return tuple((db, (vc << 7) | vf) for db, vc, vf in table)


_CURVES: dict[str, tuple[tuple[float, int], ...]] = {
    law: _curve(law) for law in FADER_LAWS
}


def db_to_raw(db: float, law: str = "audio") -> int:
    """dB -> 14-bit NRPN value, interpolating between the published points.

    Anything at or below DB_MIN is -inf, which the protocol represents as 0.
    """
    if db <= DB_MIN:
        return 0
    curve = _CURVES.get(law, _CURVES["audio"])
    if db <= curve[0][0]:
        return curve[0][1]
    if db >= curve[-1][0]:
        return curve[-1][1]
    for i in range(1, len(curve)):
        db1, raw1 = curve[i]
        if db <= db1:
            db0, raw0 = curve[i - 1]
            span = db1 - db0
            frac = 0.0 if span == 0 else (db - db0) / span
            return int(round(raw0 + frac * (raw1 - raw0)))
    return curve[-1][1]


def raw_to_db(raw: int, law: str = "audio") -> float:
    """14-bit NRPN value -> dB. 0 is -inf, reported as DB_MIN."""
    raw = max(0, min(VALUE_MAX, int(raw)))
    if raw <= 0:
        return DB_MIN
    curve = _CURVES.get(law, _CURVES["audio"])
    if raw <= curve[0][1]:
        return curve[0][0]
    if raw >= curve[-1][1]:
        return curve[-1][0]
    for i in range(1, len(curve)):
        db1, raw1 = curve[i]
        if raw <= raw1:
            db0, raw0 = curve[i - 1]
            span = raw1 - raw0
            frac = 0.0 if span == 0 else (raw - raw0) / span
            return round(db0 + frac * (db1 - db0), 1)
    return curve[-1][0]


# ── Value encoders / decoders ────────────────────────────────────────────────

def level_to_vcvf(level: float) -> tuple[int, int]:
    """Fader position 0.0..1.0 -> (VC, VF).

    Position is the raw 14-bit value normalised, so it round-trips exactly
    whichever fader law the console is set to. Only the dB reading depends on
    the law.
    """
    level = max(LEVEL_MIN, min(LEVEL_MAX, float(level)))
    value = round(level * VALUE_MAX)
    return (value >> 7) & 0x7F, value & 0x7F


def vcvf_to_level(vc: int, vf: int) -> float:
    return (((vc & 0x7F) << 7) | (vf & 0x7F)) / VALUE_MAX


def vcvf_to_raw(vc: int, vf: int) -> int:
    return ((vc & 0x7F) << 7) | (vf & 0x7F)


def raw_to_vcvf(raw: int) -> tuple[int, int]:
    raw = max(0, min(VALUE_MAX, int(raw)))
    return (raw >> 7) & 0x7F, raw & 0x7F


def pan_to_vcvf(pan: float) -> tuple[int, int]:
    """Pan -1.0 (full L) .. 0.0 (centre) .. +1.0 (full R) -> (VC, VF).

    The document's anchors are L100% = 00 00, CTR = 3F 7F (8191) and
    R100% = 7F 7F (16383); flooring hits all three, where rounding would put
    centre half an LSB right of the console's own value.
    """
    pan = max(PAN_LEFT, min(PAN_RIGHT, float(pan)))
    value = min(VALUE_MAX, int((pan + 1.0) / 2.0 * VALUE_MAX))
    return (value >> 7) & 0x7F, value & 0x7F


def vcvf_to_pan(vc: int, vf: int) -> float:
    return (vcvf_to_raw(vc, vf) / VALUE_MAX) * 2.0 - 1.0


def scene_to_bank_program(scene: int) -> tuple[int, int]:
    """Scene 1..300 -> (bank, program). Bank 0 = 1-128, 1 = 129-256, 2 = 257-300."""
    if scene < MIN_SCENE or scene > MAX_SCENE:
        raise ValueError(f"Scene {scene} outside {MIN_SCENE}..{MAX_SCENE}")
    idx = scene - 1
    return idx // 128, idx % 128


def softkey_to_note(sk: int) -> int:
    """Soft Key 1..16 -> MIDI note 0x30..0x3F."""
    if sk < 1 or sk > NUM_SOFTKEYS:
        raise ValueError(f"Soft Key {sk} outside 1..{NUM_SOFTKEYS}")
    return 0x30 + (sk - 1)


# ── Child entity types ───────────────────────────────────────────────────────
#
# Mutes relay at high cloud priority (a muted programme feed is an incident);
# continuous levels and pans are low (chatty, dashboard-grade).

def _mute_prop() -> dict[str, Any]:
    return {"type": "boolean", "label": "Mute", "cloud_priority": "high",
            "control": True}


def _level_prop(label: str) -> dict[str, Any]:
    # Normalised fader position, deliberately unitless -- it is a position,
    # not a measurement. The dB twin beside it carries the unit.
    return {"type": "number", "label": label, "min": LEVEL_MIN,
            "max": LEVEL_MAX, "cloud_priority": "low", "control": True}


def _db_prop(label: str) -> dict[str, Any]:
    return {"type": "number", "label": label, "min": DB_MIN, "max": DB_MAX,
            "unit": "dB", "cloud_priority": "low", "control": True}


def _pan_prop(label: str) -> dict[str, Any]:
    return {"type": "number", "label": label, "min": PAN_LEFT,
            "max": PAN_RIGHT, "cloud_priority": "low", "control": True}


def _send_channel() -> dict[str, dict[str, Any]]:
    """A channel whose level is a send into the LR mix."""
    return {
        "mute": _mute_prop(),
        "lr_level": _level_prop("Level (to LR)"),
        "lr_level_db": _db_prop("Level (to LR, dB)"),
        "lr_pan": _pan_prop("Pan (to LR)"),
    }


def _master_channel(fader_label: str = "Master Fader") -> dict[str, dict[str, Any]]:
    """A bus whose level is its own output master."""
    return {
        "mute": _mute_prop(),
        "fader": _level_prop(fader_label),
        "fader_db": _db_prop(f"{fader_label} (dB)"),
        "balance": _pan_prop("Balance"),
    }


CHILD_ENTITY_TYPES: dict[str, dict[str, Any]] = {
    "input": {
        "label": "Input", "label_plural": "Inputs",
        "state_variables": _send_channel(),
        "summary_fields": ["mute", "lr_level_db"],
    },
    "stereo_input": {
        "label": "Stereo Input", "label_plural": "Stereo Inputs",
        "state_variables": _send_channel(),
        "summary_fields": ["mute", "lr_level_db"],
    },
    "mix": {
        "label": "Mix", "label_plural": "Mixes",
        "state_variables": _master_channel(),
        "summary_fields": ["mute", "fader_db"],
    },
    "matrix": {
        "label": "Matrix", "label_plural": "Matrices",
        "state_variables": _master_channel(),
        "summary_fields": ["mute", "fader_db"],
    },
    "fx_send": {
        "label": "FX Send", "label_plural": "FX Sends",
        "state_variables": {
            "mute": _mute_prop(),
            "fader": _level_prop("Master Fader"),
            "fader_db": _db_prop("Master Fader (dB)"),
        },
        "summary_fields": ["mute", "fader_db"],
    },
    "fx_return": {
        "label": "FX Return", "label_plural": "FX Returns",
        "state_variables": _send_channel(),
        "summary_fields": ["mute", "lr_level_db"],
    },
    "dca": {
        "label": "DCA", "label_plural": "DCAs",
        "state_variables": {
            "mute": _mute_prop(),
            "fader": _level_prop("Fader"),
            "fader_db": _db_prop("Fader (dB)"),
        },
        "summary_fields": ["mute", "fader_db"],
    },
    "mute_group": {
        "label": "Mute Group", "label_plural": "Mute Groups",
        "state_variables": {"mute": _mute_prop()},
        "summary_fields": ["mute"],
    },
}

# Children are keyed by string local-ids (in01, mix03, dca1, ...). Declare a
# string id_format on each type; the platform otherwise defaults to integer ids
# and rejects strings at register_child.
for _ctype_def in CHILD_ENTITY_TYPES.values():
    _ctype_def["id_format"] = {"type": "string", "max_length": 64}


_MUTE_PROPS = {"mute"}
_PAN_PROPS = {"lr_pan", "balance"}
_DB_PROPS = {"lr_level_db", "fader_db"}

# Which state key carries the dB twin of each position key.
_DB_TWIN = {"lr_level": "lr_level_db", "fader": "fader_db"}


# ── The candidate roster ─────────────────────────────────────────────────────
#
# What the protocol *can* address. The connect-time probe intersects this with
# what the desk actually answers for, and only the intersection is registered.
#
# _GROUP note: on this generation a mix bus is configurable as an Aux or a
# Group, so "Grp1..12" in the protocol tables are send SOURCES into mixes and
# FX sends rather than buses of their own -- they have no mute and no LR send,
# which was confirmed by sweeping the address space on a real Qu-5. Their
# masters are the MIX masters. So groups are exposed as send sources on the
# send commands and are deliberately not registered as children: a child with
# no mute and no fader would be an empty row.

# (child type, local id, label, mute address, level address, pan address)
_Candidate = tuple[str, str, str, "tuple[int, int] | None",
                   "tuple[int, int] | None", "tuple[int, int] | None"]


def _build_candidates() -> list[_Candidate]:
    out: list[_Candidate] = []

    for n in range(1, NUM_INPUTS + 1):
        off = _src_input(n)
        out.append(("input", f"in{n:02d}", f"Input {n}", mute_addr(off),
                    to_lr(off), to_lr(off, PLANE_PAN)))

    for n in range(1, NUM_STEREO_INPUTS + 1):
        off = _src_stereo(n)
        out.append(("stereo_input", f"st{n}", f"Stereo {n}", mute_addr(off),
                    to_lr(off), to_lr(off, PLANE_PAN)))
    out.append(("stereo_input", "usb", "USB", mute_addr(SRC_USB),
                to_lr(SRC_USB), to_lr(SRC_USB, PLANE_PAN)))

    for n in range(1, NUM_FX_RETURNS + 1):
        off = _src_fx_return(n)
        out.append(("fx_return", f"fxr{n}", f"FX Return {n}", mute_addr(off),
                    to_lr(off), to_lr(off, PLANE_PAN)))

    for n in range(1, NUM_MIXES + 1):
        out.append(("mix", f"mix{n:02d}", f"Mix {n}", mute_addr(_src_mix(n)),
                    master_mix(n), master_mix(n, PLANE_PAN)))

    for n in range(1, NUM_FX_SENDS + 1):
        out.append(("fx_send", f"fxs{n}", f"FX Send {n}",
                    mute_addr(_src_fx_send(n)), master_fx_send(n), None))

    for n in range(1, NUM_MATRICES + 1):
        out.append(("matrix", f"mtx{n}", f"Matrix {n}",
                    mute_addr(_src_matrix(n)), master_matrix(n),
                    master_matrix(n, PLANE_PAN)))

    for n in range(1, NUM_DCAS + 1):
        out.append(("dca", f"dca{n}", f"DCA {n}", mute_dca(n), master_dca(n), None))

    for n in range(1, NUM_MUTE_GROUPS + 1):
        out.append(("mute_group", f"mg{n}", f"Mute Group {n}", mute_mgrp(n),
                    None, None))

    return out


CANDIDATES: list[_Candidate] = _build_candidates()


# ── Command catalog ──────────────────────────────────────────────────────────

_ACTION_PARAM = {
    "type": "enum", "values": ["on", "off", "toggle"], "default": "on",
    "required": True, "label": "Action",
    "help": "on, off, or toggle (toggle flips the console's current state and "
            "reads the result back).",
}

_DIRECTION_PARAM = {
    "type": "enum", "values": ["up", "down"], "default": "up",
    "required": True, "label": "Direction",
    "help": "Nudge the level one 1 dB step up or down.",
}

_LEVEL_PARAM = {
    "type": "number", "min": LEVEL_MIN, "max": LEVEL_MAX, "default": 0.75,
    "required": True, "label": "Level",
    "help": "Fader position, 0.0 (off) to 1.0 (top of the fader).",
}

_DB_PARAM = {
    "type": "number", "min": DB_MIN, "max": DB_MAX, "default": 0.0,
    "required": True, "label": "Level (dB)", "unit": "dB",
    "help": f"-inf..+10 dB. {DB_MIN} means off. Accuracy depends on the "
            "console's NRPN Fader Law matching this device's setting.",
}

_PAN_PARAM = {
    "type": "number", "min": PAN_LEFT, "max": PAN_RIGHT, "default": PAN_CENTER,
    "required": True, "label": "Pan",
    "help": "-1.0 full left, 0.0 centre, +1.0 full right.",
}


def _child_param(ctype: str, label: str | None = None) -> dict[str, Any]:
    return {"type": "child_id", "child_type": ctype, "required": True,
            "label": label or CHILD_ENTITY_TYPES[ctype]["label"]}


def _num(label: str, maximum: int, help_text: str | None = None) -> dict[str, Any]:
    return {"type": "integer", "required": True, "min": 1, "max": maximum,
            "label": label, "help": help_text or f"{label} number (1-{maximum})."}


# Channel types that carry a master fader, and the address function for it.
_MASTER_TYPES: dict[str, str] = {
    "mix": "Mix", "matrix": "Matrix", "fx_send": "FX Send", "dca": "DCA",
}
# Channel types whose level is a send into LR.
_SEND_TYPES: dict[str, str] = {
    "input": "Input", "stereo_input": "Stereo Input", "fx_return": "FX Return",
}


def _build_commands() -> dict[str, dict[str, Any]]:
    cmds: dict[str, dict[str, Any]] = {}

    def add(cid: str, label: str, help_text: str, params: dict[str, Any]) -> None:
        cmds[cid] = {"label": label, "help": help_text, "params": params}

    # Scene recall.
    add("recall_scene", "Recall Scene",
        f"Recall scene {MIN_SCENE}-{MAX_SCENE} via Bank Select + Program "
        "Change. The scene must already exist on the console; blank scenes "
        "cannot be recalled.",
        {"scene": {"type": "integer", "required": True, "min": MIN_SCENE,
                   "max": MAX_SCENE, "label": "Scene"}})

    # Soft Keys.
    sk = {"softkey": _num("Soft Key", NUM_SOFTKEYS,
                          "Soft Key number (1-16; 1-8 are the surface keys, "
                          "9-16 are the extra keys QuMixPad exposes).")}
    add("softkey_press", "Soft Key Press", "Press and hold a Soft Key.", dict(sk))
    add("softkey_release", "Soft Key Release", "Release a held Soft Key.", dict(sk))
    add("softkey_pulse", "Soft Key Pulse",
        "Press then release a Soft Key -- the usual one-shot trigger.", dict(sk))

    # Mutes, one per channel type, each with a channel picker.
    for ctype, label in list(_SEND_TYPES.items()) + list(_MASTER_TYPES.items()):
        add(f"mute_{ctype}", f"Mute {label}",
            f"Mute, unmute, or toggle {'an' if label[0] in 'AEIOU' else 'a'} "
            f"{label.lower()}.",
            {ctype: _child_param(ctype), "action": dict(_ACTION_PARAM)})
    add("mute_mute_group", "Fire Mute Group",
        "Activate, clear, or toggle a mute group.",
        {"mute_group": _child_param("mute_group"), "action": dict(_ACTION_PARAM)})
    add("mute_lr", "Mute LR Master",
        "Mute, unmute, or toggle the LR (main) master.",
        {"action": dict(_ACTION_PARAM)})
    add("mute_all_inputs", "Mute All Inputs",
        "Mute every input and stereo input on the console.", {})
    add("unmute_all_inputs", "Unmute All Inputs",
        "Unmute every input and stereo input on the console.", {})

    # Master faders, by position and by dB.
    for ctype, label in _MASTER_TYPES.items():
        add(f"set_{ctype}_fader", f"Set {label} Fader",
            f"Set a {label.lower()} master fader by position.",
            {ctype: _child_param(ctype), "level": dict(_LEVEL_PARAM)})
        add(f"set_{ctype}_fader_db", f"Set {label} Fader (dB)",
            f"Set a {label.lower()} master fader by dB value.",
            {ctype: _child_param(ctype), "db": dict(_DB_PARAM)})
        add(f"step_{ctype}_fader", f"Nudge {label} Fader",
            f"Step a {label.lower()} master fader 1 dB up or down.",
            {ctype: _child_param(ctype), "direction": dict(_DIRECTION_PARAM)})

    add("set_lr_fader", "Set LR Master Fader",
        "Set the LR (main) master fader by position.", {"level": dict(_LEVEL_PARAM)})
    add("set_lr_fader_db", "Set LR Master Fader (dB)",
        "Set the LR (main) master fader by dB value.", {"db": dict(_DB_PARAM)})
    add("step_lr_fader", "Nudge LR Master Fader",
        "Step the LR master fader 1 dB up or down.",
        {"direction": dict(_DIRECTION_PARAM)})

    # Channel sends into LR, by position and by dB.
    for ctype, label in _SEND_TYPES.items():
        add(f"set_{ctype}_lr_level", f"Set {label} -> LR Level",
            f"Set {'an' if label[0] in 'AEIOU' else 'a'} {label.lower()}'s "
            "send level into the LR mix, by position.",
            {ctype: _child_param(ctype), "level": dict(_LEVEL_PARAM)})
        add(f"set_{ctype}_lr_level_db", f"Set {label} -> LR Level (dB)",
            f"Set {'an' if label[0] in 'AEIOU' else 'a'} {label.lower()}'s "
            "send level into the LR mix, by dB value.",
            {ctype: _child_param(ctype), "db": dict(_DB_PARAM)})
        add(f"step_{ctype}_lr_level", f"Nudge {label} -> LR Level",
            f"Step {'an' if label[0] in 'AEIOU' else 'a'} {label.lower()}'s "
            "LR send 1 dB up or down.",
            {ctype: _child_param(ctype), "direction": dict(_DIRECTION_PARAM)})
        add(f"set_{ctype}_lr_pan", f"Set {label} -> LR Pan",
            f"Pan {'an' if label[0] in 'AEIOU' else 'a'} {label.lower()} "
            "within the LR mix.",
            {ctype: _child_param(ctype), "pan": dict(_PAN_PARAM)})

    # Aux sends -- the send matrix an integrator automates most.
    add("set_input_to_mix_level", "Set Input -> Mix Level",
        "Set an input's send level into a mix (aux send), by position.",
        {"input": _num("Input", NUM_INPUTS), "mix": _num("Mix", NUM_MIXES),
         "level": dict(_LEVEL_PARAM)})
    add("set_input_to_mix_level_db", "Set Input -> Mix Level (dB)",
        "Set an input's send level into a mix (aux send), by dB value.",
        {"input": _num("Input", NUM_INPUTS), "mix": _num("Mix", NUM_MIXES),
         "db": dict(_DB_PARAM)})
    add("set_input_to_mix_pan", "Set Input -> Mix Pan",
        "Pan an input within a stereo mix.",
        {"input": _num("Input", NUM_INPUTS), "mix": _num("Mix", NUM_MIXES),
         "pan": dict(_PAN_PARAM)})
    add("set_group_to_mix_level", "Set Group -> Mix Level",
        "Set a group's send level into a mix, by position.",
        {"group": _num("Group", NUM_GROUPS), "mix": _num("Mix", NUM_MIXES),
         "level": dict(_LEVEL_PARAM)})
    add("set_input_to_fx_send_level", "Set Input -> FX Send Level",
        "Set an input's send level into an FX send, by position.",
        {"input": _num("Input", NUM_INPUTS), "fx": _num("FX Send", NUM_FX_SENDS),
         "level": dict(_LEVEL_PARAM)})
    add("set_lr_to_matrix_level", "Set LR -> Matrix Level",
        "Set the LR mix's send level into a matrix, by position.",
        {"matrix": _num("Matrix", NUM_MATRICES), "level": dict(_LEVEL_PARAM)})
    add("set_mix_to_matrix_level", "Set Mix -> Matrix Level",
        "Set a mix's send level into a matrix, by position.",
        {"mix": _num("Mix", NUM_MIXES), "matrix": _num("Matrix", NUM_MATRICES),
         "level": dict(_LEVEL_PARAM)})

    # Master balances.
    add("set_lr_balance", "Set LR Master Balance",
        "Balance the LR (main) master output.", {"pan": dict(_PAN_PARAM)})
    for ctype in ("mix", "matrix"):
        label = _MASTER_TYPES[ctype]
        add(f"set_{ctype}_balance", f"Set {label} Balance",
            f"Balance a {label.lower()} master output.",
            {ctype: _child_param(ctype), "pan": dict(_PAN_PARAM)})

    # Assignments.
    add("assign_input_to_lr", "Assign Input -> LR",
        "Assign or unassign an input to the LR mix.",
        {"input": _num("Input", NUM_INPUTS), "action": dict(_ACTION_PARAM)})
    add("assign_input_to_mix", "Assign Input -> Mix",
        "Assign or unassign an input to a mix.",
        {"input": _num("Input", NUM_INPUTS), "mix": _num("Mix", NUM_MIXES),
         "action": dict(_ACTION_PARAM)})

    # Maintenance.
    add("refresh", "Refresh State",
        "Re-query every exposed parameter from the console.", {})
    add("rediscover", "Re-detect Channels",
        "Probe the console again and rebuild the channel list. Run this after "
        "reconfiguring the console's mix buses.", {})

    return cmds


# ── Driver ───────────────────────────────────────────────────────────────────

class AllenHeathQu567Driver(BaseDriver):
    """Allen & Heath Qu-5 / Qu-6 / Qu-7 MIDI-over-TCP driver."""

    HEALTH_FAULT_MESSAGE = (
        "Connected, but the Qu stopped answering Get requests -- "
        "connection lost."
    )

    DRIVER_INFO = {
        "id": "allenheath_qu567",
        "name": "Allen & Heath Qu-5/6/7 Digital Mixer",
        "manufacturer": "Allen & Heath",
        "category": "audio",
        "version": "1.0.1",
        "author": "OpenAVC",
        "description": (
            "Controls the current Allen & Heath Qu generation (Qu-5, Qu-6, "
            "Qu-7 and the Dante -5D/-6D/-7D variants) via MIDI over TCP/IP on "
            "port 51325. On connect the driver probes the console and "
            "registers exactly the channels it really has, so any model and "
            "any mix-bus configuration comes up correct with nothing to "
            "declare. Every input, stereo input, mix, matrix, FX send/return, "
            "DCA and mute group is a child entity with live mute / level / dB "
            "/ pan state. Mute, fader (position or dB), 1 dB nudges, pan and "
            "balance, aux and FX sends, mix assignments, scene recall (1-300) "
            "and Soft Key triggers -- with every write confirmed by reading "
            "the console back, and a Get-probe watchdog that flips the device "
            "offline if the console vanishes."
        ),
        "source_url": (
            "https://www.allen-heath.com/content/uploads/2025/06/"
            "Qu567_MIDI_Protocol_Iss2.pdf"
        ),
        "tags": ["mixer", "console", "midi", "nrpn", "allen-heath", "qu"],
        "verified": True,
        "simulated": True,
        "protocols": ["midi-over-tcp"],
        # Literal, not DEFAULT_PORT: the catalog builder reads the index
        # fields straight out of the source and takes only literal data.
        "ports": [51325],
        "transport": "tcp",
        "discovery": {
            # Identifying this console needs an ABSENCE, which a declarative
            # probe cannot express, so the fingerprint is a Python companion.
            # Every Allen & Heath console of this protocol generation answers a
            # Get for the LR master mute -- the SQ family included, because the
            # SQ shares the protocol deliberately -- and the Qu's parameter map
            # is a subset of the SQ's, so nothing a Qu has identifies it
            # positively. What separates them is that an SQ also answers for
            # Ip40 and a Qu is silent there. See the companion for the detail.
            "python": {"file": "allenheath_qu567_discovery.py"},
            "oui": ["00:04:c4"],
            "port_open": [51325],
            "manufacturer_alias": [
                "allen & heath", "allen and heath", "a&h", "allen-heath",
                "audiotonix",
            ],
        },
        "min_platform_version": "0.25.0",
        "compatible_models": [
            {
                "manufacturer": "Allen & Heath",
                "models": ["Qu-5", "Qu-5D"],
                "confidence": "full",
                "notes": (
                    "Verified end-to-end against a real Qu-5: the whole "
                    "parameter map swept address by address, channel roster "
                    "discovery, mute / level round-trips confirmed by reading "
                    "the console back, and surface moves on the desk arriving "
                    "as live state."
                ),
            },
            {
                "manufacturer": "Allen & Heath",
                "models": ["Qu-6", "Qu-6D", "Qu-7", "Qu-7D"],
                "confidence": "untested",
                "notes": (
                    "All three models share the same processing and the same "
                    "MIDI surface -- 38 inputs to mix, 12 mix buses, 4 "
                    "matrices, 6 stereo FX engines -- and differ only in local "
                    "I/O and fader count, which MIDI does not address. The "
                    "roster probe adapts to the console either way. Requires "
                    "console firmware V1.1 or later."
                ),
            },
        ],
        "child_entity_types": CHILD_ENTITY_TYPES,
        "default_config": {
            "host": "",
            "port": DEFAULT_PORT,
            "midi_channel": 1,
            "fader_law": "audio",
            "poll_interval": 60,
        },
        "config_schema": {
            "host": {
                "type": "string", "required": True, "label": "IP Address",
                "description": "Qu network IP address (SETUP > Network on the "
                               "console).",
            },
            "port": {
                "type": "integer", "default": DEFAULT_PORT, "label": "TCP Port",
                "min": 1, "max": 65535,
                "description": "MIDI over TCP/IP port. Always 51325 on Qu.",
            },
            "midi_channel": {
                "type": "integer", "default": 1, "min": 1, "max": 16,
                "label": "MIDI Channel",
                "description": "Must match the console's MIDI Channel "
                               "(UTILITY > General > MIDI). Default 1.",
            },
            "fader_law": {
                "type": "enum",
                "values": [
                    {"value": "audio", "label": "Audio Taper"},
                    {"value": "linear", "label": "Linear Taper"},
                ],
                "default": "audio", "label": "NRPN Fader Law",
                "description": (
                    "Must match the console's NRPN Fader Law (UTILITY > "
                    "General > MIDI). Only dB values depend on it -- fader "
                    "positions are correct either way. Linear Taper is finer "
                    "and its dB mapping is exact, so prefer it when the "
                    "console is yours to set."
                ),
            },
            "poll_interval": {
                "type": "integer", "default": 60, "min": 0,
                "label": "Poll Interval (s)", "advanced": True,
                "description": (
                    "Periodic re-sweep of every exposed parameter. The console "
                    "never speaks unprompted, so this is the guaranteed source "
                    "of truth. 0 disables it."
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
            {"id": "rediscover", "kind": "command", "icon": "radar"},
        ],
        "help": {
            "overview": (
                "Controls an Allen & Heath Qu-5, Qu-6 or Qu-7 over the network "
                "using MIDI over TCP/IP. On connect the driver asks the "
                "console which channels it has and lists each one as a child "
                "entity with live mute, level and pan state -- bind child "
                "state (e.g. device.<id>.input.in05.mute) to panel elements "
                "and drive mutes, faders, aux sends and scene recall from "
                "macros and buttons. Levels are published both as a fader "
                "position and in dB."
            ),
            "setup": (
                "1. Connect the Qu to the same network as the OpenAVC server. "
                "On a Qu-5D/6D/7D you can use the Dante port instead if "
                "Control Network Bridge is on.\n"
                "2. On the console, go to SETUP > Network and note the IP "
                "address.\n"
                "3. Go to UTILITY > General > MIDI and note the MIDI Channel "
                "and the NRPN Fader Law.\n"
                "4. Enter the IP address, matching MIDI channel and matching "
                "fader law here. The default port (51325) is correct.\n"
                "5. Save. Within a second or two the channels appear under the "
                "device with live state. Rename them in the project as you "
                "like -- the Qu MIDI protocol does not expose channel names, "
                "and a name you set is never overwritten.\n"
                "\n"
                "Notes:\n"
                "  * The console allows only ONE MIDI-over-TCP client at a "
                "time. Close Qu-Pad, QuMixPad or another control system before "
                "connecting, or they will keep OpenAVC out.\n"
                "  * Requires console firmware V1.1 or later.\n"
                "  * If you reconfigure the console's mix buses, run the "
                "Re-detect Channels action so the channel list follows.\n"
                "  * This driver is for the Qu-5/6/7 only. The original Qu-16, "
                "Qu-24, Qu-32, Qu-Pac and Qu-SB use a different protocol -- "
                "use the Allen & Heath Qu Digital Mixer driver for those."
            ),
        },
        "state_variables": {
            "current_scene": {"type": "integer", "label": "Current Scene",
                              "min": 0, "max": MAX_SCENE},
            "channel_count": {"type": "integer", "label": "Channels Detected"},
            "lr_mute": {"type": "boolean", "label": "LR Master Mute",
                        "cloud_priority": "high", "control": True},
            "lr_fader": {"type": "number", "label": "LR Master Fader",
                         "min": LEVEL_MIN, "max": LEVEL_MAX, "control": True},
            "lr_fader_db": {"type": "number", "label": "LR Master Fader (dB)",
                            "min": DB_MIN, "max": DB_MAX, "unit": "dB",
                            "control": True},
            "lr_balance": {"type": "number", "label": "LR Master Balance",
                           "min": PAN_LEFT, "max": PAN_RIGHT, "control": True},
        },
        "commands": _build_commands(),
    }

    # How long to wait for the roster probe's replies before deciding what the
    # console has. The desk answers in milliseconds on a quiet LAN; this is
    # generous so a busy network cannot amputate the channel list.
    DISCOVERY_WINDOW_S = 2.0
    # Delay between a write and the Get that confirms it.
    CONFIRM_DELAY_S = 0.12

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._rx_buf = bytearray()
        # NRPN aggregator: the last MSB/LSB/VC seen per MIDI channel, so a
        # 4-message sequence resolves to one parameter update.
        self._nrpn_state: dict[int, dict[str, int]] = {
            ch: {"msb": 0, "lsb": 0, "vc": 0} for ch in range(16)
        }
        self._running_status = 0
        self._last_bank = 0

        # Built by _discover_roster():
        #   _route: (MSB, LSB) -> (kind, ctype, sid, prop); ctype "lr" is flat.
        #   _child_num: (ctype, sid) -> 1-based channel number.
        self._route: dict[tuple[int, int], tuple[str, str, str, str]] = {}
        self._child_num: dict[tuple[str, str], int] = {}

        # Every parameter address the console has answered for since the last
        # reset. This is what turns "silence means absent" into a roster.
        self._seen: set[tuple[int, int]] = set()
        self._collecting = False

        self._probe_addr = master_lr()
        self._probe_fut: asyncio.Future[None] | None = None

    # ── Config accessors ────────────────────────────────────────────────

    @property
    def _ch(self) -> int:
        """0-based MIDI channel for status bytes (config is 1-based)."""
        try:
            return max(0, min(15, int(self.config.get("midi_channel", 1)) - 1))
        except (TypeError, ValueError):
            return 0

    @property
    def _law(self) -> str:
        law = str(self.config.get("fader_law", "audio")).lower()
        return law if law in FADER_LAWS else "audio"

    # ── Connection lifecycle ────────────────────────────────────────────

    async def _pre_connect(self) -> None:
        if not str(self.config.get("host", "")).strip():
            raise ValueError("host is required")
        self._probe_fut = None
        self._rx_buf.clear()
        self._running_status = 0

    def _transport_kwargs(
        self, transport_type: str, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        kwargs["host"] = str(kwargs["host"]).strip()
        kwargs["delimiter"] = None      # raw MIDI byte stream, not line-framed
        return kwargs

    async def _initial_sync(self) -> None:
        await self._discover_roster()
        await self._refresh_all()

    # ── Roster discovery ────────────────────────────────────────────────

    async def _discover_roster(self) -> int:
        """Ask the console which channels it has, and register those.

        A Get for a parameter the console does not have is answered with
        silence; one it does have always answers. So probing the mute and
        master-fader address of every candidate channel enumerates the desk,
        which is the only way to be right about a console whose mix buses are
        configurable and whose protocol carries no model identify.

        If the console answers nothing at all -- a firmware that does not
        implement Get, or a probe lost to a bad link -- the full documented
        roster is registered instead, so the driver degrades to the static
        behaviour rather than to no channels.
        """
        self._seen.clear()
        self._collecting = True
        try:
            probes: list[tuple[int, int]] = []
            for _ctype, _sid, _label, mute, level, pan in CANDIDATES:
                # Every address a child could own is asked about, pan
                # included: the pan plane mirrors the level plane on the
                # consoles measured, but inferring one from the other would be
                # a guess where asking costs one more message.
                probes.extend(a for a in (mute, level, pan) if a is not None)
            probes.append(master_lr())
            probes.append(mute_addr(SRC_LR))
            probes.append(master_lr(PLANE_PAN))

            for i, addr in enumerate(probes):
                try:
                    await self._send(self._nrpn_get(*addr))
                except (ConnectionError, OSError):
                    break
                if i % 16 == 15:
                    await asyncio.sleep(0.01)

            await asyncio.sleep(self.DISCOVERY_WINDOW_S)
            answered = set(self._seen)
        finally:
            self._collecting = False

        if not answered:
            log.warning(
                "[%s] the console answered no probes; registering the full "
                "documented channel list instead", self.device_id,
            )
        self._build_topology(answered or None)
        count = len(self._child_num)
        self.set_state("channel_count", count)
        log.info("[%s] detected %d channel(s) across %d type(s)",
                 self.device_id, count,
                 len({c for c, _ in self._child_num}))
        return count

    def _build_topology(self, answered: set[tuple[int, int]] | None) -> None:
        """Register the discovered children and build the inbound route map.

        ``answered`` is the set of addresses the console replied for, or None
        to accept every candidate. register_child is idempotent and a label the
        user set in the project is never overwritten.
        """
        self._route = {}
        self._child_num = {}
        counters: dict[str, int] = {}

        def present(addr: tuple[int, int] | None) -> bool:
            if addr is None:
                return False
            return True if answered is None else addr in answered

        for ctype, sid, label, mute, level, pan in CANDIDATES:
            # A channel counts as present if the console answered for anything
            # it owns. A mute-only child (a mute group) is decided by its mute.
            if not (present(mute) or present(level)):
                continue

            counters[ctype] = counters.get(ctype, 0) + 1
            self._child_num[(ctype, sid)] = counters[ctype]

            props = CHILD_ENTITY_TYPES[ctype]["state_variables"]
            level_key = "fader" if "fader" in props else "lr_level"
            pan_key = "balance" if "balance" in props else "lr_pan"

            if present(mute):
                self._route[mute] = ("mute", ctype, sid, "mute")
            if present(level) and level_key in props:
                self._route[level] = ("level", ctype, sid, level_key)
            if present(pan) and pan_key in props:
                self._route[pan] = ("pan", ctype, sid, pan_key)

            initial = None
            project = self._project_child_entities.get(ctype, {}).get(sid)
            if not (project and project.get("label")):
                initial = {"label": label}
            self.register_child(ctype, sid, initial_state=initial)

        # LR master routes to flat device state.
        self._route[mute_addr(SRC_LR)] = ("mute", "lr", "lr", "lr_mute")
        self._route[master_lr()] = ("level", "lr", "lr", "lr_fader")
        self._route[master_lr(PLANE_PAN)] = ("pan", "lr", "lr", "lr_balance")

    async def refresh_children(self) -> dict[str, Any]:
        """IDE 'Refresh from Device': re-probe the roster, then re-sweep."""
        self._require_connected()
        count = await self._discover_roster()
        await self._refresh_all()
        return {"channels": count}

    # ── Polling backstop ────────────────────────────────────────────────

    async def poll(self) -> None:
        if not self.connected:
            return
        await self._refresh_all()

    # ── Send helpers ────────────────────────────────────────────────────

    def _require_connected(self) -> None:
        if not (self.transport and self._connected):
            raise ConnectionError(f"[{self.device_id}] Not connected")

    async def _send(self, data: bytes) -> None:
        if not self.transport:
            return
        await self.transport.send(data)

    def _nrpn(self, msb: int, lsb: int, vc: int, vf: int) -> bytes:
        """Absolute set: BN 63 MSB  BN 62 LSB  BN 06 VC  BN 26 VF."""
        b = 0xB0 | self._ch
        return bytes([b, 0x63, msb & 0x7F, b, 0x62, lsb & 0x7F,
                      b, 0x06, vc & 0x7F, b, 0x26, vf & 0x7F])

    def _nrpn_get(self, msb: int, lsb: int) -> bytes:
        """Get current value: BN 63 MSB  BN 62 LSB  BN 60 7F."""
        b = 0xB0 | self._ch
        return bytes([b, 0x63, msb & 0x7F, b, 0x62, lsb & 0x7F, b, 0x60, 0x7F])

    def _nrpn_inc(self, msb: int, lsb: int) -> bytes:
        b = 0xB0 | self._ch
        return bytes([b, 0x63, msb & 0x7F, b, 0x62, lsb & 0x7F, b, 0x60, 0x00])

    def _nrpn_dec(self, msb: int, lsb: int) -> bytes:
        b = 0xB0 | self._ch
        return bytes([b, 0x63, msb & 0x7F, b, 0x62, lsb & 0x7F, b, 0x61, 0x00])

    async def _write_and_confirm(self, msb: int, lsb: int,
                                 vc: int, vf: int) -> None:
        """Set a parameter, then read it back.

        The console does not echo a change it received over MIDI, so a driver
        that wants correct state has to ask. Reading back rather than mirroring
        matters on this console: Audio Taper quantises a fader to 64-count
        steps, so the value it keeps is routinely not the value sent.
        """
        await self._send(self._nrpn(msb, lsb, vc, vf))
        await asyncio.sleep(self.CONFIRM_DELAY_S)
        await self._send(self._nrpn_get(msb, lsb))

    # ── Liveness watchdog ───────────────────────────────────────────────

    async def _liveness_probe(self) -> None:
        """Get the LR master level and await the reply.

        This console says nothing unprompted, so without a probe a desk that
        vanished without a FIN would stay "online" forever. Replies carry no
        tag, so the probe correlates by parameter address: any absolute for the
        probed address resolves it -- the Get's own reply, or a console-side
        move of that same fader, which equally proves the desk is alive.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        self._probe_fut = fut
        try:
            await self._send(self._nrpn_get(*self._probe_addr))
            await fut
        finally:
            self._probe_fut = None

    # ── Dispatch ────────────────────────────────────────────────────────

    async def send_command(self, command: str,
                           params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        method = getattr(self, f"cmd_{command}", None)
        if method is None:
            raise ValueError(f"Unknown command: {command}")
        return await method(**params)

    def _num_of(self, ctype: str, sid: Any) -> int:
        n = self._child_num.get((ctype, str(sid)))
        if n is None:
            raise ValueError(f"Unknown {ctype}: {sid!r}")
        return n

    def _child_addr(self, ctype: str, sid: Any, which: str) -> tuple[int, int]:
        """Resolve a registered child's mute / level / pan address.

        Read back out of the route map rather than recomputed, so a child that
        the roster probe placed can never be addressed somewhere else.
        """
        key = str(sid)
        if (ctype, key) not in self._child_num:
            raise ValueError(f"Unknown {ctype}: {sid!r}")
        for addr, (kind, ct, s, _prop) in self._route.items():
            if ct == ctype and s == key and kind == which:
                return addr
        raise ValueError(
            f"{CHILD_ENTITY_TYPES[ctype]['label']} {sid} has no "
            f"{which} on this console"
        )

    # ── Commands: scene / Soft Keys ─────────────────────────────────────

    async def cmd_recall_scene(self, scene: int) -> None:
        bank, program = scene_to_bank_program(int(scene))
        b = 0xB0 | self._ch
        c = 0xC0 | self._ch
        await self._send(bytes([b, 0x00, bank, c, program]))
        self.set_state("current_scene", int(scene))

    async def cmd_softkey_press(self, softkey: int) -> None:
        await self._send(bytes([0x90 | self._ch, softkey_to_note(int(softkey)), 0x7F]))

    async def cmd_softkey_release(self, softkey: int) -> None:
        await self._send(bytes([0x80 | self._ch, softkey_to_note(int(softkey)), 0x00]))

    async def cmd_softkey_pulse(self, softkey: int) -> None:
        await self.cmd_softkey_press(softkey)
        await asyncio.sleep(0.05)
        await self.cmd_softkey_release(softkey)

    # ── Commands: mutes ─────────────────────────────────────────────────

    async def _do_mute(self, msb: int, lsb: int, action: str) -> None:
        action = (action or "on").lower()
        if action == "toggle":
            # Native toggle, then read back -- the outcome depends on the
            # console's current state, which only it knows.
            await self._send(self._nrpn_inc(msb, lsb))
            await asyncio.sleep(self.CONFIRM_DELAY_S)
            await self._send(self._nrpn_get(msb, lsb))
        else:
            await self._write_and_confirm(msb, lsb, 0x00,
                                          0x01 if action == "on" else 0x00)

    async def cmd_mute_input(self, input: Any, action: str = "on") -> None:
        await self._do_mute(*self._child_addr("input", input, "mute"), action)

    async def cmd_mute_stereo_input(self, stereo_input: Any,
                                    action: str = "on") -> None:
        await self._do_mute(
            *self._child_addr("stereo_input", stereo_input, "mute"), action)

    async def cmd_mute_fx_return(self, fx_return: Any, action: str = "on") -> None:
        await self._do_mute(*self._child_addr("fx_return", fx_return, "mute"), action)

    async def cmd_mute_mix(self, mix: Any, action: str = "on") -> None:
        await self._do_mute(*self._child_addr("mix", mix, "mute"), action)

    async def cmd_mute_matrix(self, matrix: Any, action: str = "on") -> None:
        await self._do_mute(*self._child_addr("matrix", matrix, "mute"), action)

    async def cmd_mute_fx_send(self, fx_send: Any, action: str = "on") -> None:
        await self._do_mute(*self._child_addr("fx_send", fx_send, "mute"), action)

    async def cmd_mute_dca(self, dca: Any, action: str = "on") -> None:
        await self._do_mute(*self._child_addr("dca", dca, "mute"), action)

    async def cmd_mute_mute_group(self, mute_group: Any,
                                  action: str = "on") -> None:
        await self._do_mute(
            *self._child_addr("mute_group", mute_group, "mute"), action)

    async def cmd_mute_lr(self, action: str = "on") -> None:
        await self._do_mute(*mute_addr(SRC_LR), action)

    async def _do_mute_all(self, on: bool) -> None:
        targets = [addr for addr, (kind, ctype, _s, _p) in self._route.items()
                   if kind == "mute" and ctype in ("input", "stereo_input")]
        for i, (msb, lsb) in enumerate(sorted(targets)):
            await self._send(self._nrpn(msb, lsb, 0x00, 0x01 if on else 0x00))
            if i % 16 == 15:
                await asyncio.sleep(0.01)
        await asyncio.sleep(self.CONFIRM_DELAY_S)
        for i, (msb, lsb) in enumerate(sorted(targets)):
            await self._send(self._nrpn_get(msb, lsb))
            if i % 16 == 15:
                await asyncio.sleep(0.01)

    async def cmd_mute_all_inputs(self) -> None:
        await self._do_mute_all(True)

    async def cmd_unmute_all_inputs(self) -> None:
        await self._do_mute_all(False)

    # ── Commands: levels ────────────────────────────────────────────────

    async def _do_level(self, msb: int, lsb: int, level: float) -> None:
        await self._write_and_confirm(msb, lsb, *level_to_vcvf(level))

    async def _do_level_db(self, msb: int, lsb: int, db: float) -> None:
        await self._write_and_confirm(
            msb, lsb, *raw_to_vcvf(db_to_raw(float(db), self._law)))

    async def _do_step(self, msb: int, lsb: int, direction: str) -> None:
        up = (direction or "up").lower() == "up"
        await self._send(self._nrpn_inc(msb, lsb) if up
                         else self._nrpn_dec(msb, lsb))
        await asyncio.sleep(self.CONFIRM_DELAY_S)
        await self._send(self._nrpn_get(msb, lsb))

    async def cmd_set_mix_fader(self, mix: Any, level: float) -> None:
        await self._do_level(*self._child_addr("mix", mix, "level"), level)

    async def cmd_set_mix_fader_db(self, mix: Any, db: float) -> None:
        await self._do_level_db(*self._child_addr("mix", mix, "level"), db)

    async def cmd_step_mix_fader(self, mix: Any, direction: str = "up") -> None:
        await self._do_step(*self._child_addr("mix", mix, "level"), direction)

    async def cmd_set_matrix_fader(self, matrix: Any, level: float) -> None:
        await self._do_level(*self._child_addr("matrix", matrix, "level"), level)

    async def cmd_set_matrix_fader_db(self, matrix: Any, db: float) -> None:
        await self._do_level_db(*self._child_addr("matrix", matrix, "level"), db)

    async def cmd_step_matrix_fader(self, matrix: Any,
                                    direction: str = "up") -> None:
        await self._do_step(*self._child_addr("matrix", matrix, "level"), direction)

    async def cmd_set_fx_send_fader(self, fx_send: Any, level: float) -> None:
        await self._do_level(*self._child_addr("fx_send", fx_send, "level"), level)

    async def cmd_set_fx_send_fader_db(self, fx_send: Any, db: float) -> None:
        await self._do_level_db(*self._child_addr("fx_send", fx_send, "level"), db)

    async def cmd_step_fx_send_fader(self, fx_send: Any,
                                     direction: str = "up") -> None:
        await self._do_step(*self._child_addr("fx_send", fx_send, "level"), direction)

    async def cmd_set_dca_fader(self, dca: Any, level: float) -> None:
        await self._do_level(*self._child_addr("dca", dca, "level"), level)

    async def cmd_set_dca_fader_db(self, dca: Any, db: float) -> None:
        await self._do_level_db(*self._child_addr("dca", dca, "level"), db)

    async def cmd_step_dca_fader(self, dca: Any, direction: str = "up") -> None:
        await self._do_step(*self._child_addr("dca", dca, "level"), direction)

    async def cmd_set_lr_fader(self, level: float) -> None:
        await self._do_level(*master_lr(), level)

    async def cmd_set_lr_fader_db(self, db: float) -> None:
        await self._do_level_db(*master_lr(), db)

    async def cmd_step_lr_fader(self, direction: str = "up") -> None:
        await self._do_step(*master_lr(), direction)

    # Channel sends into LR.

    async def cmd_set_input_lr_level(self, input: Any, level: float) -> None:
        await self._do_level(*self._child_addr("input", input, "level"), level)

    async def cmd_set_input_lr_level_db(self, input: Any, db: float) -> None:
        await self._do_level_db(*self._child_addr("input", input, "level"), db)

    async def cmd_step_input_lr_level(self, input: Any,
                                      direction: str = "up") -> None:
        await self._do_step(*self._child_addr("input", input, "level"), direction)

    async def cmd_set_stereo_input_lr_level(self, stereo_input: Any,
                                            level: float) -> None:
        await self._do_level(
            *self._child_addr("stereo_input", stereo_input, "level"), level)

    async def cmd_set_stereo_input_lr_level_db(self, stereo_input: Any,
                                               db: float) -> None:
        await self._do_level_db(
            *self._child_addr("stereo_input", stereo_input, "level"), db)

    async def cmd_step_stereo_input_lr_level(self, stereo_input: Any,
                                             direction: str = "up") -> None:
        await self._do_step(
            *self._child_addr("stereo_input", stereo_input, "level"), direction)

    async def cmd_set_fx_return_lr_level(self, fx_return: Any,
                                         level: float) -> None:
        await self._do_level(
            *self._child_addr("fx_return", fx_return, "level"), level)

    async def cmd_set_fx_return_lr_level_db(self, fx_return: Any,
                                            db: float) -> None:
        await self._do_level_db(
            *self._child_addr("fx_return", fx_return, "level"), db)

    async def cmd_step_fx_return_lr_level(self, fx_return: Any,
                                          direction: str = "up") -> None:
        await self._do_step(
            *self._child_addr("fx_return", fx_return, "level"), direction)

    # Send matrix.

    async def cmd_set_input_to_mix_level(self, input: int, mix: int,
                                         level: float) -> None:
        await self._do_level(*to_mix(_src_input(int(input)), int(mix)), level)

    async def cmd_set_input_to_mix_level_db(self, input: int, mix: int,
                                            db: float) -> None:
        await self._do_level_db(*to_mix(_src_input(int(input)), int(mix)), db)

    async def cmd_set_input_to_mix_pan(self, input: int, mix: int,
                                       pan: float) -> None:
        await self._do_pan(
            *to_mix(_src_input(int(input)), int(mix), PLANE_PAN), pan)

    async def cmd_set_group_to_mix_level(self, group: int, mix: int,
                                         level: float) -> None:
        await self._do_level(*to_mix(_src_group(int(group)), int(mix)), level)

    async def cmd_set_input_to_fx_send_level(self, input: int, fx: int,
                                             level: float) -> None:
        await self._do_level(*to_fx_send(_src_input(int(input)), int(fx)), level)

    async def cmd_set_lr_to_matrix_level(self, matrix: int, level: float) -> None:
        await self._do_level(*to_matrix(0, int(matrix)), level)

    async def cmd_set_mix_to_matrix_level(self, mix: int, matrix: int,
                                          level: float) -> None:
        await self._do_level(*to_matrix(int(mix), int(matrix)), level)

    # ── Commands: pan / balance ─────────────────────────────────────────

    async def _do_pan(self, msb: int, lsb: int, pan: float) -> None:
        await self._write_and_confirm(msb, lsb, *pan_to_vcvf(pan))

    async def cmd_set_input_lr_pan(self, input: Any, pan: float) -> None:
        await self._do_pan(*self._child_addr("input", input, "pan"), pan)

    async def cmd_set_stereo_input_lr_pan(self, stereo_input: Any,
                                          pan: float) -> None:
        await self._do_pan(
            *self._child_addr("stereo_input", stereo_input, "pan"), pan)

    async def cmd_set_fx_return_lr_pan(self, fx_return: Any, pan: float) -> None:
        await self._do_pan(*self._child_addr("fx_return", fx_return, "pan"), pan)

    async def cmd_set_lr_balance(self, pan: float) -> None:
        await self._do_pan(*master_lr(PLANE_PAN), pan)

    async def cmd_set_mix_balance(self, mix: Any, pan: float) -> None:
        await self._do_pan(*self._child_addr("mix", mix, "pan"), pan)

    async def cmd_set_matrix_balance(self, matrix: Any, pan: float) -> None:
        await self._do_pan(*self._child_addr("matrix", matrix, "pan"), pan)

    # ── Commands: assignments ───────────────────────────────────────────

    async def _do_assign(self, msb: int, lsb: int, action: str) -> None:
        action = (action or "on").lower()
        if action == "toggle":
            await self._send(self._nrpn_inc(msb, lsb))
            await asyncio.sleep(self.CONFIRM_DELAY_S)
            await self._send(self._nrpn_get(msb, lsb))
        else:
            await self._write_and_confirm(msb, lsb, 0x00,
                                          0x01 if action == "on" else 0x00)

    async def cmd_assign_input_to_lr(self, input: int, action: str = "on") -> None:
        await self._do_assign(
            *to_lr(_src_input(int(input)), PLANE_ASSIGN), action)

    async def cmd_assign_input_to_mix(self, input: int, mix: int,
                                      action: str = "on") -> None:
        await self._do_assign(
            *to_mix(_src_input(int(input)), int(mix), PLANE_ASSIGN), action)

    # ── Commands: maintenance ───────────────────────────────────────────

    async def cmd_refresh(self) -> None:
        await self._refresh_all()

    async def cmd_rediscover(self) -> dict[str, Any]:
        self._require_connected()
        count = await self._discover_roster()
        await self._refresh_all()
        return {"channels": count}

    async def _refresh_all(self) -> None:
        """Get every parameter the driver exposes as state.

        Sequenced with small yields so a fresh connect does not overrun the
        console's socket buffer.
        """
        if not self.connected:
            return
        for i, (msb, lsb) in enumerate(sorted(self._route)):
            try:
                await self._send(self._nrpn_get(msb, lsb))
            except (ConnectionError, OSError):
                break
            if i % 16 == 15:
                await asyncio.sleep(0.01)

    # ── MIDI parser (incoming) ──────────────────────────────────────────

    def on_data_received(self, data: bytes) -> None:
        if not data:
            return
        self._rx_buf.extend(data)
        self._parse()

    def _parse(self) -> None:
        """MIDI 1.0 status-byte state machine, running status supported."""
        buf = self._rx_buf
        running_status = self._running_status

        i = 0
        while i < len(buf):
            b = buf[i]

            # System Real-Time (0xF8-0xFF): single byte, does not disturb
            # running status.
            if 0xF8 <= b <= 0xFF:
                i += 1
                continue

            if b & 0x80:
                if 0xF0 <= b <= 0xF7:
                    if b == 0xF0:
                        end = buf.find(0xF7, i + 1)
                        if end == -1:
                            break               # incomplete SysEx; wait
                        i = end + 1
                        running_status = 0
                        continue
                    # Other System Common; nothing here uses one. Skipping the
                    # status byte alone is safe -- the 0x80 test resyncs if a
                    # data byte is under-skipped.
                    i += 1
                    running_status = 0
                    continue
                running_status = b
                i += 1
                continue

            if not running_status:
                i += 1                          # lost sync; drop a byte
                continue

            high = running_status & 0xF0
            ch = running_status & 0x0F

            if high in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
                if i + 1 >= len(buf):
                    break                       # incomplete
                d1, d2 = buf[i], buf[i + 1]
                i += 2
                if d1 & 0x80 or d2 & 0x80:
                    running_status = 0          # corrupt frame; resync
                    continue
                if high == 0xB0:
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

    def _handle_cc(self, ch: int, controller: int, value: int) -> None:
        """Aggregate the NRPN building blocks for one MIDI channel.

        Absolute:  63 MSB / 62 LSB / 06 VC / 26 VF
        Toggle:    63 MSB / 62 LSB / 60 00   (console-side mute flip)
        """
        if controller == 0x00:
            if ch == self._ch:
                self._last_bank = value         # precedes a Program Change
            return

        nrpn = self._nrpn_state[ch]
        if controller == 0x63:
            nrpn["msb"] = value
        elif controller == 0x62:
            nrpn["lsb"] = value
        elif controller == 0x06:
            nrpn["vc"] = value
        elif controller == 0x26:
            self._dispatch_absolute(nrpn["msb"], nrpn["lsb"], nrpn["vc"], value)
        elif controller == 0x60 and value == 0x00:
            self._dispatch_toggle(nrpn["msb"], nrpn["lsb"])

    def _handle_program_change(self, ch: int, program: int) -> None:
        if ch != self._ch:
            return
        scene = self._last_bank * 128 + program + 1
        if MIN_SCENE <= scene <= MAX_SCENE:
            self.set_state("current_scene", scene)

    # ── State fan-out ───────────────────────────────────────────────────

    def _dispatch_absolute(self, msb: int, lsb: int, vc: int, vf: int) -> None:
        """Map an absolute (MSB, LSB, VC, VF) to state.

        Also the roster probe's ear: during discovery every answered address is
        recorded, because the fact that the console answered at all is the
        signal, whatever the value.
        """
        key = (msb, lsb)

        if self._collecting:
            self._seen.add(key)

        if (self._probe_fut is not None and not self._probe_fut.done()
                and key == self._probe_addr):
            self._probe_fut.set_result(None)

        route = self._route.get(key)
        if route is None:
            return                              # a parameter we do not expose
        kind, ctype, sid, prop = route

        if kind == "mute":
            updates: dict[str, Any] = {prop: bool(vf & 0x01)}
        elif kind == "pan":
            updates = {prop: vcvf_to_pan(vc, vf)}
        else:
            updates = {prop: vcvf_to_level(vc, vf)}
            twin = _DB_TWIN.get(prop)
            if twin:
                updates[twin] = raw_to_db(vcvf_to_raw(vc, vf), self._law)

        if ctype == "lr":
            for k, v in updates.items():
                self.set_state(k, v)
            if kind == "level":
                self.set_state("lr_fader_db",
                               raw_to_db(vcvf_to_raw(vc, vf), self._law))
            return
        try:
            self.set_child_state_batch(ctype, sid, updates)
        except Exception:                       # noqa: BLE001
            pass

    def _dispatch_toggle(self, msb: int, lsb: int) -> None:
        """Console-side mute toggle. Flip the state we hold."""
        route = self._route.get((msb, lsb))
        if route is None:
            return
        kind, ctype, sid, prop = route
        if kind != "mute":
            return
        if ctype == "lr":
            self.set_state(prop, not bool(self.get_state(prop)))
            return
        try:
            cur = bool(self.get_child_state(ctype, sid).get(prop))
            self.set_child_state_batch(ctype, sid, {prop: not cur})
        except Exception:                       # noqa: BLE001
            pass


# Class export expected by the loader.
DRIVER_CLASS = AllenHeathQu567Driver
