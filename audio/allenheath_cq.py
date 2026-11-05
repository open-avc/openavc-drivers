"""
OpenAVC Allen & Heath CQ Driver.

Controls the Allen & Heath CQ compact digital mixers (CQ-12T, CQ-18T, CQ-20B)
over MIDI-over-TCP/IP on port 51325. The wire format is standard MIDI 1.0 on a
raw TCP stream, always on MIDI channel 1: mixer parameters are NRPN sequences,
scene recall is Bank Select + Program Change, and Soft Keys are Note On/Off.

The CQ speaks the SQ's NRPN framing and its parameter map is a subset of the
SQ's, but the same numbers mean different channels: 00 18 / 1A / 1C / 1E are
the CQ's ST1, ST2, USB and Bluetooth inputs where an SQ has Inputs 25 to 31,
and 00 45 is the CQ's Out 1 where an SQ has Aux 1. So this is a driver of its
own rather than a model of ``allenheath_sq``.

What makes this driver first-class
-----------------------------------
* **Channels as typed child entities**, sized by the Model setting from Allen &
  Heath's model table: every input, stereo input (ST1, ST2 on the CQ-20B, USB,
  Bluetooth), FX engine, output, DCA and mute group is a child with live mute /
  level / dB / pan state. Per-channel commands take a channel picker.
* **Push.** The CQ transmits the NRPN for a parameter whenever it changes on
  the unit or from a connected app, so the driver fans console-side moves
  straight into state.
* **Writes are read back, not assumed.** Allen & Heath consoles do not echo a
  change they receive over MIDI (measured on the Qu and SQ generations), so
  each write is followed by a Get in the background and state shows what the
  console kept. A fader dragged on a panel sends every value and reads the
  fader back once, after the last.
* **Get-probe liveness.** The driver asks for the LR mute on a timer and awaits
  the answer; two misses force a typed ``no_response`` fault and a reconnect.

Push vs poll:
    Hybrid. The document says the CQ "transmits MIDI messages when any changes
    are made on the unit (or using a connected app)" to any parameter with NRPN
    control, so surface and CQ-MixPad moves arrive as they happen. A Get sweep
    at connect and on the poll interval is the backstop for what is not pushed
    (what a scene recall changes is not stated), and a scene recall is followed
    by a sweep of its own.

Format choice:
    Python rather than YAML because the wire format is binary MIDI (status
    bytes, running status, real-time bytes such as Active Sense between
    messages), a value is two 7-bit bytes joined into a 14-bit number and
    converted through the document's level table, the parameter map is
    computed from source and destination offsets, and each inbound NRPN has to
    fan out to the right child through a reverse lookup. None of that fits
    ConfigurableDriver.

Models covered:
    CQ-12T (10 mono inputs, ST1, USB, Bluetooth, 2 FX, 3 SoftKeys), CQ-18T
    (16 mono inputs, ST1, USB, Bluetooth, 4 FX, 3 SoftKeys) and CQ-20B
    (16 mono inputs, ST1, ST2, USB, Bluetooth, 4 FX, no SoftKeys). All have the
    Main LR, Out 1-6, 4 DCAs and 4 mute groups. The MIDI protocol carries no
    model identify, so the Model setting picks the roster. MIDI control needs
    CQ firmware V1.2.0 or later.

Scope notes:
    - **One level table.** The CQ has no NRPN fader law setting and its
      document prints one table, the same values as Allen & Heath's Audio
      Taper table for the Qu-5/6/7 (0 dB = 62 00). dB values are interpolated
      between its points; a fader position (0..1) is the raw value normalised.
    - **Pan and balance** run 00 00 (full left) to 40 00 (centre) to 7F 7F
      (full right), the three values the document gives; between them the
      scale is linear on each side.
    - **DCA and mute group mutes are not read back.** CQ firmware V1.2.1 and
      V1.2.2 list as known issues that a MIDI Get for a DCA or mute group mute
      always answers "off" (ID-1878) and that Mute Toggle does not work on them
      (ID-1875; the protocol document says the same). So those mutes take on
      and off only, the driver shows what it sent and what the console
      reports when the mute changes on the unit, and a sweep never asks for
      them.
    - **Linked outputs.** An odd/even output pair set to Stereo Link acts as
      one channel; the document addresses a pair through its odd output
      (Out1/2 is Out 1's number). Pan to an output exists only for a linked
      pair, and only while that output's Follow Main LR Pan is Off.
    - **Scenes 1-100.** The CQ holds 100 scenes per show; the document's
      Program Change reaches 128. A recall of an empty scene does nothing.
    - **The FX mute.** The document lists one mute per FX engine (FX1 00 51)
      and does not say whether it mutes the return or the send; it is the
      FX child's ``mute``.
    - The protocol carries no channel names, so children carry positional
      labels ("Input 1") that the user renames in the project. A label the user
      sets is never overwritten.
    - Only ONE MIDI-over-TCP connection at a time (the document says so), so
      the Allen & Heath MIDI Control app or another control system holding it
      keeps OpenAVC out, and vice versa.

Source:
    https://www.allen-heath.com/content/uploads/2024/06/CQ_MIDI_Protocol_V1_2_0_iss5.pdf
    (CQ MIDI Protocol, Firmware V1.2, Issue 5)
"""

from __future__ import annotations

import asyncio
from typing import Any

from openavc.drivers.base import BaseDriver
from openavc.utils.logger import get_logger

try:
    from openavc.drivers.base import CommandRefusedError
except ImportError:  # OpenAVC before 0.37.1
    CommandRefusedError = RuntimeError

log = get_logger(__name__)


DEFAULT_PORT = 51325

# The CQ uses MIDI channel 1 for all control messaging (document section 1.3);
# there is no setting for it on the console.
_CC = 0xB0
_PC = 0xC0
_NOTE_ON = 0x90
_NOTE_OFF = 0x80

# ── Models ───────────────────────────────────────────────────────────────────
#
# From the CQ User Guide's model table and the three Tech Datasheets (Iss3):
# mono inputs, the stereo channels (stereo line input(s), USB, Bluetooth), FX
# engines and SoftKeys. Everything else is common to all three.

MODELS: dict[str, dict[str, Any]] = {
    "CQ-12T": {"inputs": 10, "stereo": ("st1", "usb", "bt"), "fx": 2, "softkeys": 3},
    "CQ-18T": {"inputs": 16, "stereo": ("st1", "usb", "bt"), "fx": 4, "softkeys": 3},
    "CQ-20B": {"inputs": 16, "stereo": ("st1", "st2", "usb", "bt"), "fx": 4,
               "softkeys": 0},
}
DEFAULT_MODEL = "CQ-18T"

MAX_INPUTS = 16
NUM_OUTPUTS = 6
MAX_FX = 4
NUM_DCAS = 4
NUM_MUTE_GROUPS = 4
MAX_SOFTKEYS = 3

MIN_SCENE = 1
MAX_SCENE = 100          # "The CQ has 100 Scene slots available (per show)"

VALUE_MAX = 0x3FFF       # 16383, the 14-bit NRPN ceiling

LEVEL_MIN, LEVEL_MAX = 0.0, 1.0
PAN_LEFT, PAN_CENTER, PAN_RIGHT = -1.0, 0.0, 1.0
PAN_CENTRE_RAW = 0x2000  # 40 00, the document's centre

# dB endpoints of the level table. -inf is represented by DB_MIN.
DB_MIN, DB_MAX = -90.0, 10.0


# ── Source offsets ───────────────────────────────────────────────────────────
#
# Every channel that can be muted or sent from has one offset, and it drives
# the mute, level and pan planes alike. Read off the document's reference
# tables; a stereo channel takes two numbers and is addressed by the first.

def _src_input(n: int) -> int:
    return n - 1                       # Ip1..16 -> 0x00..0x0F


SRC_STEREO: dict[str, int] = {
    "st1": 0x18,
    "st2": 0x1A,
    "usb": 0x1C,
    "bt": 0x1E,
}


def _src_fx_return(n: int) -> int:
    return 0x3C + (n - 1)              # FX1..4 return -> 0x3C..0x3F


SRC_LR = 0x44


def _src_output(n: int) -> int:
    return 0x45 + (n - 1)              # Out1..6 mute -> 0x45..0x4A


def _src_fx_mute(n: int) -> int:
    return 0x51 + (n - 1)              # FX1..4 mute -> 0x51..0x54


# ── Address planes ───────────────────────────────────────────────────────────
#
# MSB and LSB are two independent 7-bit bytes, so a flat offset rolls the LSB
# past 0x7F into MSB+1 (Ip6 to Out1 is 41 00).

PLANE_MUTE = 0x00
PLANE_LEVEL = 0x40
PLANE_PAN = 0x50

MSB_DCA_MUTE = 0x02
MSB_MGRP_MUTE = 0x04

# The send tables are laid out twelve destinations wide per source and four
# FX units wide, the SQ's spacing, of which the CQ uses six outputs.
_OUT_STRIDE = 12
_FX_STRIDE = 4


def _addr(msb_base: int, offset: int) -> tuple[int, int]:
    """(MSB, LSB) for a base MSB plus a flat offset that may roll the LSB."""
    return (msb_base + (offset >> 7)) & 0x7F, offset & 0x7F


def mute_addr(src_offset: int) -> tuple[int, int]:
    return _addr(PLANE_MUTE, src_offset)


def mute_dca(n: int) -> tuple[int, int]:
    return _addr(MSB_DCA_MUTE, n - 1)              # DCA1..4   -> 02 00..03


def mute_mgrp(n: int) -> tuple[int, int]:
    return _addr(MSB_MGRP_MUTE, n - 1)             # MGRP1..4  -> 04 00..03


def to_lr(src_offset: int, plane: int = PLANE_LEVEL) -> tuple[int, int]:
    """A source's send to the Main LR.  Ip1 -> 40 00, FX1 return -> 40 3C."""
    return _addr(plane, src_offset)


def to_output(src_offset: int, out: int, plane: int = PLANE_LEVEL) -> tuple[int, int]:
    """A source's send to Out <out>.  Ip1->Out1 = 40 44, Ip12->Out2 = 41 49,
    ST1->Out1 = 42 64, FX2->Out1 = 46 20. On the pan plane, Out 1, 3 and 5
    are the linked pairs 1/2, 3/4 and 5/6."""
    return _addr(plane, 0x44 + src_offset * _OUT_STRIDE + (out - 1))


def to_fx(src_offset: int, fx: int) -> tuple[int, int]:
    """A source's send into FX unit <fx>.  Ip1->FX1 = 4C 14, USB->FX1 = 4D 04,
    FX1->FX2 = 4E 05. Level only: the document has no pan to an FX unit."""
    return _addr(PLANE_LEVEL + 0x0C, 0x14 + src_offset * _FX_STRIDE + (fx - 1))


def master_lr() -> tuple[int, int]:
    return (0x4F, 0x00)


def master_output(n: int) -> tuple[int, int]:
    return (0x4F, 0x01 + (n - 1))                   # Out1..6   -> 4F 01..06


def master_fx(n: int) -> tuple[int, int]:
    """The level into FX unit <n> (the document's "to FX unit")."""
    return (0x4F, 0x0D + (n - 1))                   # FX1..4    -> 4F 0D..10


def master_dca(n: int) -> tuple[int, int]:
    return (0x4F, 0x20 + (n - 1))                   # DCA1..4   -> 4F 20..23


# ── Messages ─────────────────────────────────────────────────────────────────

def nrpn_set(msb: int, lsb: int, vc: int, vf: int) -> bytes:
    """Absolute set: B0 63 MSB  B0 62 LSB  B0 06 VC  B0 26 VF."""
    return bytes([_CC, 0x63, msb & 0x7F, _CC, 0x62, lsb & 0x7F,
                  _CC, 0x06, vc & 0x7F, _CC, 0x26, vf & 0x7F])


def nrpn_get(msb: int, lsb: int) -> bytes:
    """Get the current value: B0 63 MSB  B0 62 LSB  B0 60 7F."""
    return bytes([_CC, 0x63, msb & 0x7F, _CC, 0x62, lsb & 0x7F, _CC, 0x60, 0x7F])


def nrpn_increment(msb: int, lsb: int) -> bytes:
    """+1 dB, one pan step right, or a mute toggle: B0 60 00."""
    return bytes([_CC, 0x63, msb & 0x7F, _CC, 0x62, lsb & 0x7F, _CC, 0x60, 0x00])


def nrpn_decrement(msb: int, lsb: int) -> bytes:
    """-1 dB, one pan step left, or a mute toggle: B0 61 00."""
    return bytes([_CC, 0x63, msb & 0x7F, _CC, 0x62, lsb & 0x7F, _CC, 0x61, 0x00])


def scene_message(scene: int) -> bytes:
    """Scene 1..100 -> Bank Select (always bank 00) + Program Change, with the
    document's offset of one: scene 1 is program 00."""
    if scene < MIN_SCENE or scene > MAX_SCENE:
        raise ValueError(f"Scene {scene} outside {MIN_SCENE}..{MAX_SCENE}")
    return bytes([_CC, 0x00, 0x00, _PC, (scene - 1) & 0x7F])


def softkey_note(softkey: int) -> int:
    """SoftKey 1..3 -> MIDI note 0x30..0x32 (C3 upward)."""
    if softkey < 1 or softkey > MAX_SOFTKEYS:
        raise ValueError(f"SoftKey {softkey} outside 1..{MAX_SOFTKEYS}")
    return 0x30 + (softkey - 1)


# ── Level table ──────────────────────────────────────────────────────────────
#
# The document's "Example Level Values (VC/VF)", written as the published hex
# pairs so each row can be checked against it by eye. Below -89 the value is
# -inf (00 00).

_LEVEL_TABLE: tuple[tuple[float, int, int], ...] = (
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

_CURVE: tuple[tuple[float, int], ...] = tuple(
    (db, (vc << 7) | vf) for db, vc, vf in _LEVEL_TABLE
)


def db_to_raw(db: float) -> int:
    """dB -> 14-bit NRPN value, interpolating between the document's points.

    At or below DB_MIN is -inf, which the protocol represents as 0.
    """
    if db <= DB_MIN:
        return 0
    if db <= _CURVE[0][0]:
        return _CURVE[0][1]
    if db >= _CURVE[-1][0]:
        return _CURVE[-1][1]
    for i in range(1, len(_CURVE)):
        db1, raw1 = _CURVE[i]
        if db <= db1:
            db0, raw0 = _CURVE[i - 1]
            frac = (db - db0) / (db1 - db0)
            return int(round(raw0 + frac * (raw1 - raw0)))
    return _CURVE[-1][1]


def raw_to_db(raw: int) -> float:
    """14-bit NRPN value -> dB. 0 is -inf, reported as DB_MIN."""
    raw = max(0, min(VALUE_MAX, int(raw)))
    if raw <= 0:
        return DB_MIN
    if raw <= _CURVE[0][1]:
        return _CURVE[0][0]
    if raw >= _CURVE[-1][1]:
        return _CURVE[-1][0]
    for i in range(1, len(_CURVE)):
        db1, raw1 = _CURVE[i]
        if raw <= raw1:
            db0, raw0 = _CURVE[i - 1]
            frac = (raw - raw0) / (raw1 - raw0)
            return round(db0 + frac * (db1 - db0), 1)
    return _CURVE[-1][0]


# ── Value encoders / decoders ────────────────────────────────────────────────

def vcvf_to_raw(vc: int, vf: int) -> int:
    return ((vc & 0x7F) << 7) | (vf & 0x7F)


def raw_to_vcvf(raw: int) -> tuple[int, int]:
    raw = max(0, min(VALUE_MAX, int(raw)))
    return (raw >> 7) & 0x7F, raw & 0x7F


def level_to_vcvf(level: float) -> tuple[int, int]:
    """Fader position 0.0..1.0 -> (VC, VF): the raw 14-bit value normalised."""
    level = max(LEVEL_MIN, min(LEVEL_MAX, float(level)))
    return raw_to_vcvf(round(level * VALUE_MAX))


def vcvf_to_level(vc: int, vf: int) -> float:
    return round(vcvf_to_raw(vc, vf) / VALUE_MAX, 4)


def pan_to_vcvf(pan: float) -> tuple[int, int]:
    """Pan -1.0 (full left) .. 0.0 (centre) .. +1.0 (full right) -> (VC, VF).

    Linear on each side of the document's centre, 40 00: full left is 00 00
    and full right 7F 7F. The document's percentage examples sit within one
    count of this (its L30% is 2C 65, this gives 2C 66).
    """
    pan = max(PAN_LEFT, min(PAN_RIGHT, float(pan)))
    if pan <= 0.0:
        raw = round(PAN_CENTRE_RAW * (1.0 + pan))
    else:
        raw = PAN_CENTRE_RAW + round((VALUE_MAX - PAN_CENTRE_RAW) * pan)
    return raw_to_vcvf(raw)


def vcvf_to_pan(vc: int, vf: int) -> float:
    """(VC, VF) -> -1.0..+1.0; the document's centre, 40 00, reads 0.0."""
    raw = vcvf_to_raw(vc, vf)
    if raw <= PAN_CENTRE_RAW:
        return round(raw / PAN_CENTRE_RAW - 1.0, 4)
    return round((raw - PAN_CENTRE_RAW) / (VALUE_MAX - PAN_CENTRE_RAW), 4)


# ── Child entity types ───────────────────────────────────────────────────────
#
# Mutes relay at high cloud priority (a muted programme feed is an incident);
# continuous levels and pans are low (chatty, dashboard-grade).

def _mute_prop() -> dict[str, Any]:
    return {"type": "boolean", "label": "Mute", "cloud_priority": "high",
            "control": True}


def _level_prop(label: str) -> dict[str, Any]:
    # A normalised fader position, deliberately unitless: the dB twin beside
    # it carries the unit.
    return {"type": "number", "label": label, "min": LEVEL_MIN,
            "max": LEVEL_MAX, "cloud_priority": "low", "control": True}


def _db_prop(label: str) -> dict[str, Any]:
    return {"type": "number", "label": label, "min": DB_MIN, "max": DB_MAX,
            "unit": "dB", "cloud_priority": "low", "control": True}


def _pan_prop(label: str) -> dict[str, Any]:
    return {"type": "number", "label": label, "min": PAN_LEFT,
            "max": PAN_RIGHT, "cloud_priority": "low", "control": True}


CHILD_ENTITY_TYPES: dict[str, dict[str, Any]] = {
    "input": {
        "label": "Input", "label_plural": "Inputs",
        "state_variables": {
            "mute": _mute_prop(),
            "lr_level": _level_prop("Level (to LR)"),
            "lr_level_db": _db_prop("Level (to LR, dB)"),
            "lr_pan": _pan_prop("Pan (to LR)"),
        },
        "summary_fields": ["mute", "lr_level_db"],
    },
    "stereo_input": {
        "label": "Stereo Input", "label_plural": "Stereo Inputs",
        "state_variables": {
            "mute": _mute_prop(),
            "lr_level": _level_prop("Level (to LR)"),
            "lr_level_db": _db_prop("Level (to LR, dB)"),
            "lr_pan": _pan_prop("Balance (to LR)"),
        },
        "summary_fields": ["mute", "lr_level_db"],
    },
    "fx": {
        "label": "FX", "label_plural": "FX",
        "state_variables": {
            "mute": _mute_prop(),
            "lr_level": _level_prop("Return Level (to LR)"),
            "lr_level_db": _db_prop("Return Level (to LR, dB)"),
            "lr_pan": _pan_prop("Return Balance (to LR)"),
            "send_fader": _level_prop("Send Master"),
            "send_fader_db": _db_prop("Send Master (dB)"),
        },
        "summary_fields": ["mute", "lr_level_db"],
    },
    "output": {
        "label": "Output", "label_plural": "Outputs",
        "state_variables": {
            "mute": _mute_prop(),
            "fader": _level_prop("Master Fader"),
            "fader_db": _db_prop("Master Fader (dB)"),
        },
        "summary_fields": ["mute", "fader_db"],
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

# Children are keyed by string local ids (in01, st1, usb, out3, dca1, ...).
# Declare a string id_format on each type; the platform otherwise defaults to
# integer ids and rejects strings at register_child.
for _ctype_def in CHILD_ENTITY_TYPES.values():
    _ctype_def["id_format"] = {"type": "string", "max_length": 16}

# Which state key carries the dB twin of each position key.
_DB_TWIN = {"lr_level": "lr_level_db", "fader": "fader_db",
            "send_fader": "send_fader_db"}

_STEREO_LABELS = {"st1": "Stereo 1", "st2": "Stereo 2", "usb": "USB",
                  "bt": "Bluetooth"}


# ── The roster ───────────────────────────────────────────────────────────────

# (child type, local id, label, source offset or None, [(kind, prop, address)])
_Channel = tuple[str, str, str, "int | None", list[tuple[str, str, tuple[int, int]]]]


def roster(model: str) -> list[_Channel]:
    """Every channel a model has, and the addresses each one's state lives at."""
    spec = MODELS.get(model) or MODELS[DEFAULT_MODEL]
    out: list[_Channel] = []

    def send_channel(off: int) -> list[tuple[str, str, tuple[int, int]]]:
        return [("mute", "mute", mute_addr(off)),
                ("level", "lr_level", to_lr(off)),
                ("pan", "lr_pan", to_lr(off, PLANE_PAN))]

    for n in range(1, spec["inputs"] + 1):
        off = _src_input(n)
        out.append(("input", f"in{n:02d}", f"Input {n}", off, send_channel(off)))
    for sid in spec["stereo"]:
        off = SRC_STEREO[sid]
        out.append(("stereo_input", sid, _STEREO_LABELS[sid], off, send_channel(off)))
    for n in range(1, spec["fx"] + 1):
        off = _src_fx_return(n)
        out.append(("fx", f"fx{n}", f"FX {n}", off, [
            ("mute", "mute", mute_addr(_src_fx_mute(n))),
            ("level", "lr_level", to_lr(off)),
            ("pan", "lr_pan", to_lr(off, PLANE_PAN)),
            ("level", "send_fader", master_fx(n)),
        ]))
    for n in range(1, NUM_OUTPUTS + 1):
        out.append(("output", f"out{n}", f"Out {n}", None, [
            ("mute", "mute", mute_addr(_src_output(n))),
            ("level", "fader", master_output(n)),
        ]))
    for n in range(1, NUM_DCAS + 1):
        out.append(("dca", f"dca{n}", f"DCA {n}", None, [
            ("mute", "mute", mute_dca(n)),
            ("level", "fader", master_dca(n)),
        ]))
    for n in range(1, NUM_MUTE_GROUPS + 1):
        out.append(("mute_group", f"mg{n}", f"Mute Group {n}", None, [
            ("mute", "mute", mute_mgrp(n)),
        ]))
    return out


# DCA and mute group mutes: a Get always answers "off" and a toggle does
# nothing (CQ firmware V1.2.2 known issues ID-1878 and ID-1875).
UNREADABLE: frozenset[tuple[int, int]] = frozenset(
    [mute_dca(n) for n in range(1, NUM_DCAS + 1)]
    + [mute_mgrp(n) for n in range(1, NUM_MUTE_GROUPS + 1)]
)

# Output pairs a pan to an output addresses: the pair's odd output.
OUTPUT_PAIRS: dict[str, int] = {"1-2": 1, "3-4": 3, "5-6": 5}


# ── Command catalog ──────────────────────────────────────────────────────────

_ACTION_PARAM = {
    "type": "enum", "values": ["on", "off", "toggle"], "default": "on",
    "required": True, "label": "Action",
    "help": "on, off, or toggle (toggle flips the console's current state and "
            "reads the result back).",
}

_ON_OFF_PARAM = {
    "type": "enum", "values": ["on", "off"], "default": "on",
    "required": True, "label": "Action",
    "help": "on or off. The CQ does not toggle this mute over MIDI.",
}

_DIRECTION_PARAM = {
    "type": "enum", "values": ["up", "down"], "default": "up",
    "required": True, "label": "Direction",
    "help": "Up or down by 1 dB.",
}

_LEVEL_PARAM = {
    "type": "number", "min": LEVEL_MIN, "max": LEVEL_MAX, "default": 0.75,
    "required": True, "label": "Level",
    "help": "Fader position, 0.0 (off) to 1.0 (top of the fader).",
}

_DB_PARAM = {
    "type": "number", "min": DB_MIN, "max": DB_MAX, "default": 0.0,
    "required": True, "label": "Level (dB)", "unit": "dB",
    "help": f"-inf..+10 dB. {DB_MIN} means off.",
}

_PAN_PARAM = {
    "type": "number", "min": PAN_LEFT, "max": PAN_RIGHT, "default": PAN_CENTER,
    "required": True, "label": "Pan",
    "help": "-1.0 full left, 0.0 centre, +1.0 full right.",
}

_PAIR_PARAM = {
    "type": "enum", "required": True, "label": "Output Pair", "default": "1-2",
    "values": [
        {"value": "1-2", "label": "Out 1/2"},
        {"value": "3-4", "label": "Out 3/4"},
        {"value": "5-6", "label": "Out 5/6"},
    ],
    "help": "A pair of outputs set to Stereo Link. The pair's Follow Main LR "
            "Pan must be Off for this to take effect.",
}


def _child_param(ctype: str, label: str | None = None) -> dict[str, Any]:
    return {"type": "child_id", "child_type": ctype, "required": True,
            "label": label or CHILD_ENTITY_TYPES[ctype]["label"]}


# Channel types that carry a master fader.
_MASTER_TYPES: dict[str, str] = {"output": "Output", "dca": "DCA"}
# Channel types whose level is a send into the Main LR.
_SEND_TYPES: dict[str, str] = {
    "input": "Input", "stereo_input": "Stereo Input", "fx": "FX Return",
}


def _a(label: str) -> str:
    return f"{'an' if label[0] in 'AEIOU' else 'a'} {label.lower()}"


def _build_commands() -> dict[str, dict[str, Any]]:
    cmds: dict[str, dict[str, Any]] = {}

    def add(cid: str, label: str, help_text: str, params: dict[str, Any]) -> None:
        cmds[cid] = {"label": label, "help": help_text, "params": params}

    add("recall_scene", "Recall Scene",
        f"Recalls scene {MIN_SCENE}-{MAX_SCENE} of the current show. An empty "
        "scene cannot be recalled.",
        {"scene": {"type": "integer", "required": True, "min": MIN_SCENE,
                   "max": MAX_SCENE, "label": "Scene"}})

    # Mutes.
    for ctype, label in (("input", "Input"), ("stereo_input", "Stereo Input"),
                         ("fx", "FX"), ("output", "Output")):
        add(f"mute_{ctype}", f"Mute {label}",
            f"Mutes, unmutes or toggles {_a(label)}.",
            {ctype: _child_param(ctype), "action": dict(_ACTION_PARAM)})
    add("mute_dca", "Mute DCA", "Mutes or unmutes a DCA.",
        {"dca": _child_param("dca"), "action": dict(_ON_OFF_PARAM)})
    add("mute_mute_group", "Mute Group", "Turns a mute group on or off.",
        {"mute_group": _child_param("mute_group"), "action": dict(_ON_OFF_PARAM)})
    add("mute_lr", "Mute Main LR", "Mutes, unmutes or toggles the Main LR.",
        {"action": dict(_ACTION_PARAM)})
    add("mute_all_inputs", "Mute All Inputs",
        "Mutes every input and stereo input on the mixer.", {})
    add("unmute_all_inputs", "Unmute All Inputs",
        "Unmutes every input and stereo input on the mixer.", {})

    # Master faders.
    for ctype, label in _MASTER_TYPES.items():
        add(f"set_{ctype}_fader", f"Set {label} Fader",
            f"Moves {_a(label)} master fader to a position.",
            {ctype: _child_param(ctype), "level": dict(_LEVEL_PARAM)})
        add(f"set_{ctype}_fader_db", f"Set {label} Fader (dB)",
            f"Sets {_a(label)} master fader to a level in dB.",
            {ctype: _child_param(ctype), "db": dict(_DB_PARAM)})
        add(f"step_{ctype}_fader", f"Nudge {label} Fader",
            f"Raises or lowers {_a(label)} master fader by 1 dB.",
            {ctype: _child_param(ctype), "direction": dict(_DIRECTION_PARAM)})
    add("set_fx_send_fader", "Set FX Send Master",
        "Sets the level into an FX unit, by fader position.",
        {"fx": _child_param("fx"), "level": dict(_LEVEL_PARAM)})
    add("set_fx_send_fader_db", "Set FX Send Master (dB)",
        "Sets the level into an FX unit, in dB.",
        {"fx": _child_param("fx"), "db": dict(_DB_PARAM)})
    add("step_fx_send_fader", "Nudge FX Send Master",
        "Raises or lowers the level into an FX unit by 1 dB.",
        {"fx": _child_param("fx"), "direction": dict(_DIRECTION_PARAM)})
    add("set_lr_fader", "Set Main LR Fader",
        "Moves the Main LR fader to a position.", {"level": dict(_LEVEL_PARAM)})
    add("set_lr_fader_db", "Set Main LR Fader (dB)",
        "Sets the Main LR fader to a level in dB.", {"db": dict(_DB_PARAM)})
    add("step_lr_fader", "Nudge Main LR Fader",
        "Raises or lowers the Main LR fader by 1 dB.",
        {"direction": dict(_DIRECTION_PARAM)})

    # Channel sends into the Main LR.
    for ctype, label in _SEND_TYPES.items():
        add(f"set_{ctype}_lr_level", f"Set {label} -> LR Level",
            f"Sets {_a(label)}'s level into the Main LR, by fader position.",
            {ctype: _child_param(ctype, label), "level": dict(_LEVEL_PARAM)})
        add(f"set_{ctype}_lr_level_db", f"Set {label} -> LR Level (dB)",
            f"Sets {_a(label)}'s level into the Main LR, in dB.",
            {ctype: _child_param(ctype, label), "db": dict(_DB_PARAM)})
        add(f"step_{ctype}_lr_level", f"Nudge {label} -> LR Level",
            f"Raises or lowers {_a(label)}'s level into the Main LR by 1 dB.",
            {ctype: _child_param(ctype, label),
             "direction": dict(_DIRECTION_PARAM)})
        word = "Pan" if ctype == "input" else "Balance"
        add(f"set_{ctype}_lr_pan", f"Set {label} -> LR {word}",
            f"Sets {_a(label)}'s {word.lower()} in the Main LR.",
            {ctype: _child_param(ctype, label), "pan": dict(_PAN_PARAM)})

    # Sends to the outputs and the FX units.
    for ctype, label in _SEND_TYPES.items():
        src = {ctype: _child_param(ctype, label)}
        add(f"set_{ctype}_to_output_level", f"Set {label} -> Output Level",
            f"Sets {_a(label)}'s send level to an output, by fader position. "
            "For a linked output pair, pick its odd output.",
            {**src, "output": _child_param("output"), "level": dict(_LEVEL_PARAM)})
        add(f"set_{ctype}_to_output_level_db", f"Set {label} -> Output Level (dB)",
            f"Sets {_a(label)}'s send level to an output, in dB. For a linked "
            "output pair, pick its odd output.",
            {**src, "output": _child_param("output"), "db": dict(_DB_PARAM)})
        word = "Pan" if ctype == "input" else "Balance"
        add(f"set_{ctype}_to_output_pan", f"Set {label} -> Output {word}",
            f"Sets {_a(label)}'s {word.lower()} in a linked output pair.",
            {**src, "pair": dict(_PAIR_PARAM), "pan": dict(_PAN_PARAM)})
        add(f"set_{ctype}_to_fx_level", f"Set {label} -> FX Level",
            f"Sets {_a(label)}'s send level into an FX unit, by fader position."
            + (" An FX return does not send to its own unit." if ctype == "fx"
               else ""),
            {**src, "to_fx": _child_param("fx", "To FX"),
             "level": dict(_LEVEL_PARAM)})
        add(f"set_{ctype}_to_fx_level_db", f"Set {label} -> FX Level (dB)",
            f"Sets {_a(label)}'s send level into an FX unit, in dB."
            + (" An FX return does not send to its own unit." if ctype == "fx"
               else ""),
            {**src, "to_fx": _child_param("fx", "To FX"), "db": dict(_DB_PARAM)})

    # SoftKeys.
    sk = {"softkey": {"type": "integer", "required": True, "min": 1,
                      "max": MAX_SOFTKEYS, "label": "SoftKey",
                      "help": "SoftKey 1-3 (the CQ-12T and CQ-18T; the CQ-20B "
                              "has none)."}}
    add("softkey_press", "SoftKey Press",
        "Presses and holds a SoftKey, doing what the key is set up to do on "
        "the mixer.", dict(sk))
    add("softkey_release", "SoftKey Release", "Releases a held SoftKey.", dict(sk))
    add("softkey_pulse", "SoftKey Pulse",
        "Presses and releases a SoftKey, doing what the key is set up to do on "
        "the mixer.", dict(sk))

    add("refresh", "Refresh State",
        "Reads every mute, level and pan the driver shows from the mixer "
        "again.", {})

    return cmds


# ── Driver ───────────────────────────────────────────────────────────────────

class AllenHeathCQDriver(BaseDriver):
    """Allen & Heath CQ-12T / CQ-18T / CQ-20B MIDI-over-TCP driver."""

    HEALTH_FAULT_MESSAGE = "The CQ stopped answering Get requests."

    DRIVER_INFO = {
        "id": "allenheath_cq",
        "name": "Allen & Heath CQ Digital Mixer",
        "manufacturer": "Allen & Heath",
        "category": "audio",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": (
            "Controls the Allen & Heath CQ-12T, CQ-18T and CQ-20B compact "
            "mixers via MIDI over TCP/IP on port 51325. Every input, stereo "
            "input (stereo line, USB, Bluetooth), FX engine, output, DCA and "
            "mute group is a child entity with live mute, level, dB and pan "
            "state, kept current by the mixer's own reports. Mute, fader "
            "(position or dB), 1 dB nudges, pan and balance, sends to the "
            "outputs and FX units, scene recall (1-100) and SoftKey triggers."
        ),
        "source_url": (
            "https://www.allen-heath.com/content/uploads/2024/06/"
            "CQ_MIDI_Protocol_V1_2_0_iss5.pdf"
        ),
        "tags": ["mixer", "console", "midi", "nrpn", "allen-heath", "cq"],
        "verified": False,
        "simulated": True,
        "protocols": ["midi-over-tcp"],
        # Literal, not DEFAULT_PORT: the catalog builder reads the index
        # fields straight out of the source and takes only literal data.
        "ports": [51325],
        "transport": "tcp",
        "discovery": {
            # Identifying a CQ needs an ABSENCE. Every Allen & Heath console
            # of this protocol generation answers a Get for the LR mute, and
            # the CQ's parameter map is a subset of the Qu-5/6/7's and the
            # SQ's, so no question identifies a CQ positively. What separates
            # it is Input 17, which a Qu-5/6/7 and an SQ have and a CQ (16
            # inputs at most) does not. That a CQ stays silent for a
            # parameter it lacks is what the Qu-5/6/7 does, measured; on a
            # CQ it has not been seen. The answer is searched for rather than
            # expected first: firmware V1.2.2 added Active Sense, and an FE
            # byte read ahead of the answer would fail a prefix match.
            "tcp_probe": {
                "port": 51325,
                "send_hex": "B0 63 00 B0 62 44 B0 60 7F",
                "expect_regex": "\\xB0\\x63\\x00\\xB0\\x62\\x44",
                "timeout_ms": 2000,
                "then": {
                    "send_hex": "B0 63 00 B0 62 10 B0 60 7F",
                    "expect_silence": True,
                    "timeout_ms": 600,
                },
            },
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
                "models": ["CQ-12T", "CQ-18T", "CQ-20B"],
                "confidence": "untested",
                "notes": (
                    "Built from Allen & Heath's CQ MIDI Protocol (firmware "
                    "V1.2, Issue 5); not yet run on a CQ. Requires CQ firmware "
                    "V1.2.0 or later, which added MIDI control. Set the Model "
                    "setting to the mixer's model."
                ),
            },
        ],
        "child_entity_types": CHILD_ENTITY_TYPES,
        "default_config": {
            "host": "",
            "port": DEFAULT_PORT,
            "model": DEFAULT_MODEL,
            "poll_interval": 60,
        },
        "config_schema": {
            "host": {
                "type": "string", "required": True, "label": "IP Address",
                "description": "The CQ's network IP address (shown in the "
                               "network settings on the mixer or CQ-MixPad).",
            },
            "port": {
                "type": "integer", "default": DEFAULT_PORT, "label": "TCP Port",
                "min": 1, "max": 65535,
                "description": "MIDI over TCP/IP port. Always 51325 on a CQ.",
            },
            "model": {
                "type": "enum",
                "values": [
                    {"value": "CQ-12T", "label": "CQ-12T"},
                    {"value": "CQ-18T", "label": "CQ-18T"},
                    {"value": "CQ-20B", "label": "CQ-20B"},
                ],
                "default": DEFAULT_MODEL, "label": "Model",
                "description": (
                    "Which CQ this is. It decides the channel list: 10 inputs "
                    "and 2 FX on a CQ-12T, 16 inputs and 4 FX on a CQ-18T or "
                    "CQ-20B, and a second stereo input on a CQ-20B."
                ),
            },
            "poll_interval": {
                "type": "integer", "default": 60, "min": 0,
                "label": "Poll Interval (s)", "advanced": True,
                "description": (
                    "How often every value is read from the mixer again. The "
                    "mixer reports changes as they happen; this also catches "
                    "what a scene recall changed. 0 turns it off."
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
                "Controls an Allen & Heath CQ-12T, CQ-18T or CQ-20B over the "
                "network using MIDI over TCP/IP. Each input, stereo input, FX "
                "engine, output, DCA and mute group is listed under the device "
                "with live mute and level state (and pan on the inputs): bind "
                "child state (e.g. device.<id>.input.in05.mute) to panel "
                "elements and drive mutes, faders, sends and scene recall from "
                "macros and buttons. Levels are shown both as a fader position "
                "and in dB."
            ),
            "setup": (
                "1. Update the CQ to firmware V1.2.0 or later (MIDI control "
                "arrived in V1.2.0).\n"
                "2. Connect the CQ's network port to the same network as the "
                "OpenAVC server and note its IP address.\n"
                "3. Enter the IP address here and pick the Model. The default "
                "port (51325) is correct.\n"
                "4. Save. The channels appear under the device with live "
                "state. Rename them in the project as you like: the CQ's MIDI "
                "protocol does not carry channel names, and a name you set is "
                "never overwritten.\n"
                "\n"
                "Notes:\n"
                "  * The CQ takes only ONE MIDI-over-TCP connection at a time. "
                "Close the Allen & Heath MIDI Control app, or anything else "
                "using MIDI over the network, before connecting.\n"
                "  * DCA and mute group mutes show what OpenAVC sent and what "
                "the CQ reports when they change on the mixer. The CQ cannot "
                "be asked for them over MIDI, so they read blank until one of "
                "those happens.\n"
                "  * For a stereo-linked output pair, use its odd output "
                "(Out 1 for Out 1/2). Pan to an output pair needs that pair's "
                "Follow Main LR Pan set to Off."
            ),
        },
        "state_variables": {
            "lr_mute": {"type": "boolean", "label": "Main LR Mute",
                        "cloud_priority": "high", "control": True},
            "lr_fader": {"type": "number", "label": "Main LR Fader",
                         "min": LEVEL_MIN, "max": LEVEL_MAX, "control": True},
            "lr_fader_db": {"type": "number", "label": "Main LR Fader (dB)",
                            "min": DB_MIN, "max": DB_MAX, "unit": "dB",
                            "control": True},
        },
        "commands": _build_commands(),
    }

    # Delay between a write and the Get that reads it back. The Get goes out in
    # the background, and a newer write to the same parameter replaces a
    # read-back still waiting.
    CONFIRM_DELAY_S = 0.12
    # Wait after a scene recall before reading every value again. Whether the
    # CQ reports what a recall changed is not documented; the poll corrects a
    # late re-read.
    SCENE_SETTLE_S = 1.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._rx_buf = bytearray()
        # NRPN aggregator for MIDI channel 1: the last MSB / LSB / VC seen, so
        # a four-message sequence resolves to one parameter update.
        self._nrpn = {"msb": 0, "lsb": 0, "vc": 0}
        self._running_status = 0

        # Built by _build_topology():
        #   _route: (MSB, LSB) -> (kind, ctype, sid, prop); ctype "lr" is flat.
        #   _src: (ctype, sid) -> source offset (inputs, stereo inputs, FX).
        self._route: dict[tuple[int, int], tuple[str, str, str, str]] = {}
        self._addrs: dict[tuple[str, str, str], tuple[int, int]] = {}
        self._src: dict[tuple[str, str], int] = {}
        self._fx_num: dict[str, int] = {}

        self._probe_addr = mute_addr(SRC_LR)
        self._probe_fut: asyncio.Future[None] | None = None

        # Read-backs still waiting out CONFIRM_DELAY_S, by parameter address,
        # and every background task not yet finished (for teardown).
        self._readbacks: dict[tuple[int, int], asyncio.Task[None]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._scene_reread_task: asyncio.Task[None] | None = None

    # ── Config ──────────────────────────────────────────────────────────

    @property
    def _model(self) -> str:
        model = str(self.config.get("model", DEFAULT_MODEL)).strip().upper()
        return model if model in MODELS else DEFAULT_MODEL

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
        self._build_topology()
        await self._refresh_all()

    async def _close_session(self) -> None:
        """Every teardown path: drop the read-backs still pending.

        Their Gets belong to a connection that is gone, and the next connect's
        sweep reads every value again.
        """
        await self._cancel_tasks()

    # ── Roster ──────────────────────────────────────────────────────────

    def _build_topology(self) -> None:
        """Register the model's channels and build the inbound route map.

        register_child is idempotent and never overwrites a label the user set
        in the project; a channel the model does not have (left from an
        earlier Model setting) is removed.
        """
        self._route = {}
        self._addrs = {}
        self._src = {}
        self._fx_num = {}
        keep: dict[str, set[str]] = {ctype: set() for ctype in CHILD_ENTITY_TYPES}

        for ctype, sid, label, src, addrs in roster(self._model):
            keep[ctype].add(sid)
            if src is not None:
                self._src[(ctype, sid)] = src
            if ctype == "fx":
                self._fx_num[sid] = int(sid[2:])
            for kind, prop, addr in addrs:
                self._route[addr] = (kind, ctype, sid, prop)
                self._addrs[(ctype, sid, prop)] = addr

            initial: dict[str, Any] = {}
            project = self._project_child_entities.get(ctype, {}).get(sid)
            if not (project and project.get("label")):
                initial["label"] = label
            if ctype in ("dca", "mute_group"):
                # Unknown until it is sent or the console reports it: the
                # console cannot be asked (see UNREADABLE).
                initial["mute"] = None
            self.register_child(ctype, sid, initial_state=initial or None)

        for ctype, sids in keep.items():
            for sid in self.list_children(ctype):
                if sid not in sids:
                    self.deregister_child(ctype, sid)

        self._route[mute_addr(SRC_LR)] = ("mute", "lr", "lr", "lr_mute")
        self._route[master_lr()] = ("level", "lr", "lr", "lr_fader")

    async def refresh_children(self) -> dict[str, Any]:
        """IDE 'Refresh from Device': rebuild the roster, then read it all."""
        self._require_connected()
        self._build_topology()
        await self._refresh_all()
        return {"channels": len({(c, s) for c, s, _p in self._addrs})}

    # ── Polling backstop ────────────────────────────────────────────────

    async def poll(self) -> None:
        if not self.connected:
            return
        await self._refresh_all()

    async def _refresh_all(self) -> None:
        """Get every parameter the driver shows as state, except the DCA and
        mute group mutes, whose Get always answers "off" on this firmware.

        Paced at sixteen Gets, then 10 ms.
        """
        if not self.connected:
            return
        addrs = sorted(a for a in self._route if a not in UNREADABLE)
        for i, (msb, lsb) in enumerate(addrs):
            try:
                await self._send(nrpn_get(msb, lsb))
            except (ConnectionError, OSError):
                break
            if i % 16 == 15:
                await asyncio.sleep(0.01)

    # ── Send helpers ────────────────────────────────────────────────────

    def _require_connected(self) -> None:
        if not (self.transport and self._connected):
            raise ConnectionError(f"[{self.device_id}] Not connected")

    async def _send(self, data: bytes) -> None:
        if not self.transport:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self.transport.send(data)

    async def _write(self, addr: tuple[int, int], vc: int, vf: int,
                     read_back: bool = True) -> None:
        """Set a parameter, then read it back in the background.

        The console does not echo a change it received over MIDI, so the Get
        is what puts the value it kept into state. A send to an output or FX
        unit has no state of its own and is not read back.
        """
        await self._send(nrpn_set(*addr, vc, vf))
        if read_back:
            self._confirm_later(addr)

    def _confirm_later(self, addr: tuple[int, int],
                       delay: float | None = None) -> None:
        """Get a parameter CONFIRM_DELAY_S from now, without waiting for it.

        One read-back per address: a newer write replaces one still waiting,
        so a fader dragged on a panel sends every value and is read once,
        after the last. (On a Qu-5D, a command that waited for its read-back
        held a panel's later values behind it, and the fader trailed the
        finger by seconds.)
        """
        waiting = self._readbacks.pop(addr, None)
        if waiting is not None:
            waiting.cancel()
        task = asyncio.get_running_loop().create_task(self._readback(
            addr, self.CONFIRM_DELAY_S if delay is None else delay))
        self._readbacks[addr] = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _readback(self, addr: tuple[int, int], delay: float) -> None:
        await asyncio.sleep(delay)
        # From here the Get is going out: a newer write schedules a read-back
        # of its own rather than cancelling this one part-way onto the wire.
        if self._readbacks.get(addr) is asyncio.current_task():
            del self._readbacks[addr]
        try:
            await self._send(nrpn_get(*addr))
        except (ConnectionError, OSError):
            pass        # the link went down; the reconnect reads everything

    async def _cancel_tasks(self) -> None:
        self._readbacks.clear()
        self._scene_reread_task = None
        tasks = [t for t in self._tasks if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ── Liveness watchdog ───────────────────────────────────────────────

    async def _liveness_probe(self) -> None:
        """Get the LR mute and await the answer.

        Replies carry no tag, so the probe is answered by any report of the LR
        mute from the console: the Get's own reply, or the mute pressed on the
        mixer, which equally proves it is there. The LR mute is the same
        address on every model.
        """
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._probe_fut = fut
        try:
            await self._send(nrpn_get(*self._probe_addr))
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

    def _addr_of(self, ctype: str, sid: Any, prop: str) -> tuple[int, int]:
        addr = self._addrs.get((ctype, str(sid), prop))
        if addr is None:
            if not any(c == ctype and s == str(sid) for c, s, _ in self._addrs):
                raise ValueError(
                    f"The {self._model} has no "
                    f"{CHILD_ENTITY_TYPES[ctype]['label'].lower()} {sid!r}"
                )
            raise ValueError(f"{CHILD_ENTITY_TYPES[ctype]['label']} {sid} has "
                             f"no {prop}")
        return addr

    def _src_of(self, ctype: str, sid: Any) -> int:
        src = self._src.get((ctype, str(sid)))
        if src is None:
            raise ValueError(
                f"The {self._model} has no "
                f"{CHILD_ENTITY_TYPES[ctype]['label'].lower()} {sid!r}"
            )
        return src

    def _output_num(self, output: Any) -> int:
        sid = str(output)
        if ("output", sid, "fader") not in self._addrs:
            raise ValueError(f"No output {output!r}")
        return int(sid[3:])

    def _fx_of(self, fx: Any) -> int:
        n = self._fx_num.get(str(fx))
        if n is None:
            raise ValueError(f"The {self._model} has no FX {fx!r}")
        return n

    # ── Commands: scene / SoftKeys ──────────────────────────────────────

    async def cmd_recall_scene(self, scene: int) -> None:
        await self._send(scene_message(int(scene)))
        # The re-read runs in the background so a macro that recalls a scene
        # moves straight on; a newer recall replaces one still waiting.
        waiting = self._scene_reread_task
        if waiting is not None:
            waiting.cancel()
        task = asyncio.get_running_loop().create_task(self._scene_reread())
        self._scene_reread_task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _scene_reread(self) -> None:
        await asyncio.sleep(self.SCENE_SETTLE_S)
        if self._scene_reread_task is asyncio.current_task():
            self._scene_reread_task = None
        await self._refresh_all()

    def _softkey(self, softkey: Any) -> int:
        if not MODELS[self._model]["softkeys"]:
            raise CommandRefusedError(f"The {self._model} has no SoftKeys.")
        return softkey_note(int(softkey))

    async def cmd_softkey_press(self, softkey: int) -> None:
        await self._send(bytes([_NOTE_ON, self._softkey(softkey), 0x7F]))

    async def cmd_softkey_release(self, softkey: int) -> None:
        await self._send(bytes([_NOTE_OFF, self._softkey(softkey), 0x00]))

    async def cmd_softkey_pulse(self, softkey: int) -> None:
        note = self._softkey(softkey)
        await self._send(bytes([_NOTE_ON, note, 0x7F]))
        # The gap is how long the key is held. The release is part of this
        # command, so the command waits for it: a pulse that returned first
        # could have its release land after the next press of the same key.
        await asyncio.sleep(0.05)
        await self._send(bytes([_NOTE_OFF, note, 0x00]))

    # ── Commands: mutes ─────────────────────────────────────────────────

    async def _do_mute(self, addr: tuple[int, int], action: str) -> None:
        action = (action or "on").lower()
        if action == "toggle":
            # The console flips the mute on an increment; only it knows the
            # state it flipped from, so the result is read back.
            await self._send(nrpn_increment(*addr))
            self._confirm_later(addr)
        else:
            await self._write(addr, 0x00, 0x01 if action == "on" else 0x00)

    async def _do_mute_unreadable(self, ctype: str, sid: Any, action: str) -> None:
        """A DCA or mute group mute: on or off, shown as sent.

        A Get for it always answers "off" and a toggle does nothing (CQ
        firmware V1.2.2 known issues ID-1878 and ID-1875), so the value sent is
        the best the driver can know until the console reports a change.
        """
        action = (action or "on").lower()
        if action not in ("on", "off"):
            raise ValueError("The CQ takes on or off for this mute, not toggle.")
        on = action == "on"
        await self._send(nrpn_set(*self._addr_of(ctype, sid, "mute"), 0x00,
                                  0x01 if on else 0x00))
        self.set_child_state_batch(ctype, str(sid), {"mute": on})

    async def cmd_mute_input(self, input: Any, action: str = "on") -> None:
        await self._do_mute(self._addr_of("input", input, "mute"), action)

    async def cmd_mute_stereo_input(self, stereo_input: Any,
                                    action: str = "on") -> None:
        await self._do_mute(self._addr_of("stereo_input", stereo_input, "mute"),
                            action)

    async def cmd_mute_fx(self, fx: Any, action: str = "on") -> None:
        await self._do_mute(self._addr_of("fx", fx, "mute"), action)

    async def cmd_mute_output(self, output: Any, action: str = "on") -> None:
        await self._do_mute(self._addr_of("output", output, "mute"), action)

    async def cmd_mute_dca(self, dca: Any, action: str = "on") -> None:
        await self._do_mute_unreadable("dca", dca, action)

    async def cmd_mute_mute_group(self, mute_group: Any,
                                  action: str = "on") -> None:
        await self._do_mute_unreadable("mute_group", mute_group, action)

    async def cmd_mute_lr(self, action: str = "on") -> None:
        await self._do_mute(mute_addr(SRC_LR), action)

    async def _do_mute_all(self, on: bool) -> None:
        targets = sorted(addr for addr, (kind, ctype, _s, _p) in self._route.items()
                         if kind == "mute" and ctype in ("input", "stereo_input"))
        for i, addr in enumerate(targets):
            await self._send(nrpn_set(*addr, 0x00, 0x01 if on else 0x00))
            if i % 16 == 15:
                await asyncio.sleep(0.01)
        # The read-backs keep the same pace: 16 Gets, then 10 ms.
        for i, addr in enumerate(targets):
            self._confirm_later(addr, self.CONFIRM_DELAY_S + (i // 16) * 0.01)

    async def cmd_mute_all_inputs(self) -> None:
        await self._do_mute_all(True)

    async def cmd_unmute_all_inputs(self) -> None:
        await self._do_mute_all(False)

    # ── Commands: levels ────────────────────────────────────────────────

    async def _do_level(self, addr: tuple[int, int], level: float,
                        read_back: bool = True) -> None:
        await self._write(addr, *level_to_vcvf(level), read_back)

    async def _do_level_db(self, addr: tuple[int, int], db: float,
                           read_back: bool = True) -> None:
        await self._write(addr, *raw_to_vcvf(db_to_raw(float(db))), read_back)

    async def _do_step(self, addr: tuple[int, int], direction: str) -> None:
        up = (direction or "up").lower() == "up"
        await self._send(nrpn_increment(*addr) if up else nrpn_decrement(*addr))
        self._confirm_later(addr)

    async def cmd_set_output_fader(self, output: Any, level: float) -> None:
        await self._do_level(self._addr_of("output", output, "fader"), level)

    async def cmd_set_output_fader_db(self, output: Any, db: float) -> None:
        await self._do_level_db(self._addr_of("output", output, "fader"), db)

    async def cmd_step_output_fader(self, output: Any,
                                    direction: str = "up") -> None:
        await self._do_step(self._addr_of("output", output, "fader"), direction)

    async def cmd_set_dca_fader(self, dca: Any, level: float) -> None:
        await self._do_level(self._addr_of("dca", dca, "fader"), level)

    async def cmd_set_dca_fader_db(self, dca: Any, db: float) -> None:
        await self._do_level_db(self._addr_of("dca", dca, "fader"), db)

    async def cmd_step_dca_fader(self, dca: Any, direction: str = "up") -> None:
        await self._do_step(self._addr_of("dca", dca, "fader"), direction)

    async def cmd_set_fx_send_fader(self, fx: Any, level: float) -> None:
        await self._do_level(self._addr_of("fx", fx, "send_fader"), level)

    async def cmd_set_fx_send_fader_db(self, fx: Any, db: float) -> None:
        await self._do_level_db(self._addr_of("fx", fx, "send_fader"), db)

    async def cmd_step_fx_send_fader(self, fx: Any, direction: str = "up") -> None:
        await self._do_step(self._addr_of("fx", fx, "send_fader"), direction)

    async def cmd_set_lr_fader(self, level: float) -> None:
        await self._do_level(master_lr(), level)

    async def cmd_set_lr_fader_db(self, db: float) -> None:
        await self._do_level_db(master_lr(), db)

    async def cmd_step_lr_fader(self, direction: str = "up") -> None:
        await self._do_step(master_lr(), direction)

    # Channel sends into the Main LR.

    async def cmd_set_input_lr_level(self, input: Any, level: float) -> None:
        await self._do_level(self._addr_of("input", input, "lr_level"), level)

    async def cmd_set_input_lr_level_db(self, input: Any, db: float) -> None:
        await self._do_level_db(self._addr_of("input", input, "lr_level"), db)

    async def cmd_step_input_lr_level(self, input: Any,
                                      direction: str = "up") -> None:
        await self._do_step(self._addr_of("input", input, "lr_level"), direction)

    async def cmd_set_input_lr_pan(self, input: Any, pan: float) -> None:
        await self._write(self._addr_of("input", input, "lr_pan"),
                          *pan_to_vcvf(pan))

    async def cmd_set_stereo_input_lr_level(self, stereo_input: Any,
                                            level: float) -> None:
        await self._do_level(
            self._addr_of("stereo_input", stereo_input, "lr_level"), level)

    async def cmd_set_stereo_input_lr_level_db(self, stereo_input: Any,
                                               db: float) -> None:
        await self._do_level_db(
            self._addr_of("stereo_input", stereo_input, "lr_level"), db)

    async def cmd_step_stereo_input_lr_level(self, stereo_input: Any,
                                             direction: str = "up") -> None:
        await self._do_step(
            self._addr_of("stereo_input", stereo_input, "lr_level"), direction)

    async def cmd_set_stereo_input_lr_pan(self, stereo_input: Any,
                                          pan: float) -> None:
        await self._write(
            self._addr_of("stereo_input", stereo_input, "lr_pan"),
            *pan_to_vcvf(pan))

    async def cmd_set_fx_lr_level(self, fx: Any, level: float) -> None:
        await self._do_level(self._addr_of("fx", fx, "lr_level"), level)

    async def cmd_set_fx_lr_level_db(self, fx: Any, db: float) -> None:
        await self._do_level_db(self._addr_of("fx", fx, "lr_level"), db)

    async def cmd_step_fx_lr_level(self, fx: Any, direction: str = "up") -> None:
        await self._do_step(self._addr_of("fx", fx, "lr_level"), direction)

    async def cmd_set_fx_lr_pan(self, fx: Any, pan: float) -> None:
        await self._write(self._addr_of("fx", fx, "lr_pan"), *pan_to_vcvf(pan))

    # Sends to the outputs and FX units: no state of their own, so not read
    # back.

    def _to_output(self, ctype: str, sid: Any, output: Any) -> tuple[int, int]:
        return to_output(self._src_of(ctype, sid), self._output_num(output))

    def _to_pair(self, ctype: str, sid: Any, pair: Any) -> tuple[int, int]:
        out = OUTPUT_PAIRS.get(str(pair))
        if out is None:
            raise ValueError(f"Output pair {pair!r} is not 1-2, 3-4 or 5-6")
        return to_output(self._src_of(ctype, sid), out, PLANE_PAN)

    def _to_fx(self, ctype: str, sid: Any, unit: Any) -> tuple[int, int]:
        n = self._fx_of(unit)
        if ctype == "fx" and self._fx_of(sid) == n:
            raise ValueError("An FX return does not send to its own FX unit.")
        return to_fx(self._src_of(ctype, sid), n)

    async def cmd_set_input_to_output_level(self, input: Any, output: Any,
                                            level: float) -> None:
        await self._do_level(self._to_output("input", input, output), level, False)

    async def cmd_set_input_to_output_level_db(self, input: Any, output: Any,
                                               db: float) -> None:
        await self._do_level_db(self._to_output("input", input, output), db, False)

    async def cmd_set_input_to_output_pan(self, input: Any, pair: str,
                                          pan: float) -> None:
        await self._write(self._to_pair("input", input, pair),
                          *pan_to_vcvf(pan), False)

    async def cmd_set_input_to_fx_level(self, input: Any, to_fx: Any,
                                        level: float) -> None:
        await self._do_level(self._to_fx("input", input, to_fx), level, False)

    async def cmd_set_input_to_fx_level_db(self, input: Any, to_fx: Any,
                                           db: float) -> None:
        await self._do_level_db(self._to_fx("input", input, to_fx), db, False)

    async def cmd_set_stereo_input_to_output_level(
            self, stereo_input: Any, output: Any, level: float) -> None:
        await self._do_level(
            self._to_output("stereo_input", stereo_input, output), level, False)

    async def cmd_set_stereo_input_to_output_level_db(
            self, stereo_input: Any, output: Any, db: float) -> None:
        await self._do_level_db(
            self._to_output("stereo_input", stereo_input, output), db, False)

    async def cmd_set_stereo_input_to_output_pan(
            self, stereo_input: Any, pair: str, pan: float) -> None:
        await self._write(self._to_pair("stereo_input", stereo_input, pair),
                          *pan_to_vcvf(pan), False)

    async def cmd_set_stereo_input_to_fx_level(
            self, stereo_input: Any, to_fx: Any, level: float) -> None:
        await self._do_level(
            self._to_fx("stereo_input", stereo_input, to_fx), level, False)

    async def cmd_set_stereo_input_to_fx_level_db(
            self, stereo_input: Any, to_fx: Any, db: float) -> None:
        await self._do_level_db(
            self._to_fx("stereo_input", stereo_input, to_fx), db, False)

    async def cmd_set_fx_to_output_level(self, fx: Any, output: Any,
                                         level: float) -> None:
        await self._do_level(self._to_output("fx", fx, output), level, False)

    async def cmd_set_fx_to_output_level_db(self, fx: Any, output: Any,
                                            db: float) -> None:
        await self._do_level_db(self._to_output("fx", fx, output), db, False)

    async def cmd_set_fx_to_output_pan(self, fx: Any, pair: str,
                                       pan: float) -> None:
        await self._write(self._to_pair("fx", fx, pair), *pan_to_vcvf(pan), False)

    async def cmd_set_fx_to_fx_level(self, fx: Any, to_fx: Any,
                                     level: float) -> None:
        await self._do_level(self._to_fx("fx", fx, to_fx), level, False)

    async def cmd_set_fx_to_fx_level_db(self, fx: Any, to_fx: Any,
                                        db: float) -> None:
        await self._do_level_db(self._to_fx("fx", fx, to_fx), db, False)

    # ── Commands: maintenance ───────────────────────────────────────────

    async def cmd_refresh(self) -> None:
        await self._refresh_all()

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

            # System Real-Time (0xF8-0xFF), Active Sense (0xFE) among them,
            # which CQ firmware V1.2.2 added: one byte, allowed anywhere, and
            # it leaves running status alone.
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
                    i += 1                      # other System Common
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
                if high == 0xB0 and ch == 0:
                    self._handle_cc(d1, d2)
            elif high in (0xC0, 0xD0):
                if i >= len(buf):
                    break
                d1 = buf[i]
                i += 1
                if d1 & 0x80:
                    running_status = 0
                    continue
            else:
                i += 1

        self._running_status = running_status
        del buf[:i]

    def _handle_cc(self, controller: int, value: int) -> None:
        """Aggregate the NRPN building blocks on MIDI channel 1.

        Absolute:  63 MSB / 62 LSB / 06 VC / 26 VF
        Toggle:    63 MSB / 62 LSB / 60 00 (a mute pressed on the mixer)
        """
        nrpn = self._nrpn
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

    # ── State fan-out ───────────────────────────────────────────────────

    def _dispatch_absolute(self, msb: int, lsb: int, vc: int, vf: int) -> None:
        """Map a value the console reported to state."""
        key = (msb, lsb)

        if (self._probe_fut is not None and not self._probe_fut.done()
                and key == self._probe_addr):
            self._probe_fut.set_result(None)

        route = self._route.get(key)
        if route is None:
            return                              # a parameter not shown as state
        kind, ctype, sid, prop = route

        if kind == "mute":
            updates: dict[str, Any] = {prop: bool(vf & 0x01)}
        elif kind == "pan":
            updates = {prop: vcvf_to_pan(vc, vf)}
        else:
            updates = {prop: vcvf_to_level(vc, vf)}
            twin = "lr_fader_db" if ctype == "lr" else _DB_TWIN.get(prop)
            if twin:
                updates[twin] = raw_to_db(vcvf_to_raw(vc, vf))

        if ctype == "lr":
            for k, v in updates.items():
                self.set_state(k, v)
            return
        self.set_child_state_batch(ctype, sid, updates)

    def _dispatch_toggle(self, msb: int, lsb: int) -> None:
        """A mute flipped on the mixer reported as an increment: flip the
        state held, then ask the console for the value, so a state the driver
        did not know yet is not guessed."""
        route = self._route.get((msb, lsb))
        if route is None:
            return
        kind, ctype, sid, prop = route
        if kind != "mute":
            return
        if ctype == "lr":
            current = self.get_state(prop)
            if current is not None:
                self.set_state(prop, not bool(current))
        else:
            current = self.get_child_state(ctype, sid).get(prop)
            if current is not None:
                self.set_child_state_batch(ctype, sid, {prop: not bool(current)})
        if (msb, lsb) not in UNREADABLE:
            self._confirm_later((msb, lsb))


# Class export expected by the loader.
DRIVER_CLASS = AllenHeathCQDriver
