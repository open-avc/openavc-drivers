"""
OpenAVC Allen & Heath SQ Driver — first-class child-entity edition.

Controls Allen & Heath SQ-5, SQ-6 and SQ-7 digital mixing consoles
over MIDI-over-TCP/IP on port 51325. The protocol is the standard
MIDI 1.0 wire format carried over a raw TCP stream. Mixer parameters
(mutes, levels, panning, mix assignments) are addressed using NRPN
(Non-Registered Parameter Number) sequences; scene recall uses Bank
Select + Program Change; SoftKeys use Note On/Off.

What makes this driver first-class
-----------------------------------
* **Channels as typed child entities.** Every input, group, aux master,
  matrix master, FX send/return, DCA and mute group is a child with live
  mute / level / pan state. Per-channel commands take a channel picker
  instead of a free-typed number, and console-side moves push into child
  state so panels stay in sync.
* **Get-watchdog liveness.** The SQ answers NRPN Get requests; the
  driver probes the LR master level on a timer and awaits the reply.
  A console that vanished without closing the socket (power pull, link
  drop) stops answering, and after two misses the driver tears the
  connection down with a typed ``no_response`` fault so the platform
  reconnects and the device card shows the real cause. Pure push
  monitoring alone would leave a silently-dead SQ "online" forever.

Push vs poll:
    Hybrid. When a console-side control moves the SQ emits the matching
    NRPN sequence, which the driver fans into child state immediately.
    On connect the driver sweeps NRPN "get" requests across every
    parameter in its state surface; a slow polling re-sweep (default
    60 s) is the backstop for updates missed during transient drops.

    Writes apply optimistically: sibling Qu hardware demonstrated that
    A&H consoles do NOT echo changes they receive over MIDI (only
    surface-side moves are transmitted), so after sending a set the
    driver mirrors the commanded value into its own state. Toggle and
    1 dB step commands instead follow the native increment/decrement
    with a Get, because their outcome depends on device-side state.

Format choice:
    Python rather than YAML because (a) the wire format is binary
    (MIDI bytes, NRPN sequences with running status), (b) parameter
    tables span 624 source/destination cells that are best computed
    programmatically rather than enumerated, and (c) push handling
    requires fanning each incoming NRPN sequence out to the right
    child state key based on a reverse lookup. None of this fits
    ConfigurableDriver today.

Models covered:
    SQ-5  — 8 SoftKeys, 0 Soft Rotaries
    SQ-6  — 16 SoftKeys, 4 Soft Rotaries
    SQ-7  — 16 SoftKeys, 8 Soft Rotaries
    All three share the same MIDI command set and channel counts;
    per-model differences are physical I/O and surface controls only.

Scope notes:
    - Linear NRPN Fader Law only (the SQ default). Audio Taper has
      a different VC/VF mapping; if a customer needs it, expose a
      `fader_law` config field and a second mapping table.
    - MIDI fader strips, Soft Rotaries, MMC transport, and DAW-
      Control-channel CC/Note traffic are intentionally not exposed.
      Those are end-user surface controls on the console, not
      integrator-addressable mixer functions.
    - The protocol has no channel-name access and no model identify,
      so children carry static labels ("Input 1") that the user can
      rename in the project.
    - The manual's §3.7 "LR Mute" Get example shows parameter 00 00,
      which per its own §3.3 examples and reference tables is Input 1;
      LR mute is 00 44. The reference tables are authoritative and are
      what this driver (and its address helpers) follow.

Source:
    https://www.allen-heath.com/content/uploads/2023/11/SQ-MIDI-Protocol-Issue5.pdf
"""

from __future__ import annotations

import asyncio
from typing import Any

from openavc.drivers.base import BaseDriver
from openavc.utils.logger import get_logger

log = get_logger(__name__)


# ── Channel counts ───────────────────────────────────────────────────────────
NUM_INPUTS = 48
NUM_GROUPS = 12
NUM_AUX = 12
NUM_MTX = 3
NUM_FX_SENDS = 4
NUM_FX_RETURNS = 8
NUM_DCAS = 8
NUM_MUTE_GROUPS = 8

NUM_SOFTKEYS = 16  # Max across SQ-7; SQ-5 only has 8 — extra triggers are no-ops on hardware

# Scene
MIN_SCENE = 1
MAX_SCENE = 300

# 14-bit value range
VALUE_MAX = 0x3FFF  # 16383

# Fader endpoints
LEVEL_MIN = 0.0
LEVEL_MAX = 1.0

# Pan endpoints
PAN_LEFT = -1.0
PAN_CENTER = 0.0
PAN_RIGHT = 1.0


# ── NRPN parameter-number tables ─────────────────────────────────────────────
#
# In SQ-MIDI, MSB and LSB are two independent 7-bit bytes — not a packed
# 14-bit number. The starting MSB and LSB are taken straight from the
# protocol doc; flat-offset indexing (e.g. 12 destinations per input)
# rolls the LSB past 0x7F into MSB+1, MSB+2, ... A small helper handles
# the rollover so every table function is a one-liner you can verify
# against the doc by inspection.
#
# Worked examples used to verify: Ip1→LR=40 00, Ip40→Aux5=44 1C,
# Mute Grp 4=04 03, DCA1 mute=02 00, LR master level=4F 00.

def _addr(msb_base: int, offset: int) -> tuple[int, int]:
    """Return (MSB, LSB) for an MSB starting at msb_base with a flat
    offset that may overflow the 7-bit LSB space and roll into MSB+1.
    """
    return (msb_base + (offset >> 7)) & 0x7F, offset & 0x7F


# Mutes — MSB 0x00 (channels 0x00..0x57), 0x02 (DCAs), 0x04 (Mute Groups)

def mute_input(n: int) -> tuple[int, int]:
    return _addr(0x00, n - 1)                          # Ip1..48 → 00 00..2F


def mute_group(n: int) -> tuple[int, int]:
    return _addr(0x00, 0x30 + (n - 1))                 # Grp1..12 → 00 30..3B


def mute_fx_return(n: int) -> tuple[int, int]:
    return _addr(0x00, 0x3C + (n - 1))                 # FX1Rtn..8Rtn → 00 3C..43


def mute_lr() -> tuple[int, int]:
    return _addr(0x00, 0x44)                           # LR master → 00 44


def mute_aux_master(n: int) -> tuple[int, int]:
    return _addr(0x00, 0x45 + (n - 1))                 # Aux1..12 master → 00 45..50


def mute_fx_send(n: int) -> tuple[int, int]:
    return _addr(0x00, 0x51 + (n - 1))                 # FX1Snd..4Snd → 00 51..54


def mute_mtx_master(n: int) -> tuple[int, int]:
    return _addr(0x00, 0x55 + (n - 1))                 # Mtx1..3 master → 00 55..57


def mute_dca(n: int) -> tuple[int, int]:
    return _addr(0x02, n - 1)                          # DCA1..8 → 02 00..07


def mute_mgrp(n: int) -> tuple[int, int]:
    return _addr(0x04, n - 1)                          # MGRP1..8 → 04 00..07


# Levels — MSB 0x40+ (sends) and 0x4F (master outs)

def level_input_to_lr(n: int) -> tuple[int, int]:
    return _addr(0x40, n - 1)                          # Ip1..48 → 40 00..2F


def level_input_to_aux(n: int, m: int) -> tuple[int, int]:
    return _addr(0x40, 0x44 + (n - 1) * 12 + (m - 1))  # Ip1→Aux1 → 40 44


def level_group_to_lr(n: int) -> tuple[int, int]:
    return _addr(0x40, 0x30 + (n - 1))                 # Grp1..12 → 40 30..3B


def level_group_to_aux(n: int, m: int) -> tuple[int, int]:
    return _addr(0x45, 0x04 + (n - 1) * 12 + (m - 1))  # Grp1→Aux1 → 45 04


def level_fx_return_to_lr(n: int) -> tuple[int, int]:
    return _addr(0x40, 0x3C + (n - 1))                 # FX1Rtn..8Rtn → 40 3C..43


def level_fx_return_to_aux(n: int, m: int) -> tuple[int, int]:
    return _addr(0x46, 0x14 + (n - 1) * 12 + (m - 1))  # FX1Rtn→Aux1 → 46 14


def level_input_to_fx_send(n: int, m: int) -> tuple[int, int]:
    return _addr(0x4C, 0x14 + (n - 1) * 4 + (m - 1))   # Ip1→FX1Snd → 4C 14


def level_group_to_fx_send(n: int, m: int) -> tuple[int, int]:
    return _addr(0x4D, 0x54 + (n - 1) * 4 + (m - 1))   # Grp1→FX1Snd → 4D 54


def level_fx_return_to_fx_send(n: int, m: int) -> tuple[int, int]:
    return _addr(0x4E, 0x04 + (n - 1) * 4 + (m - 1))   # FX1Rtn→FX1Snd → 4E 04


def level_lr_to_mtx(m: int) -> tuple[int, int]:
    return _addr(0x4E, 0x24 + (m - 1))                 # LR→Mtx1..3 → 4E 24..26


def level_aux_to_mtx(n: int, m: int) -> tuple[int, int]:
    return _addr(0x4E, 0x27 + (n - 1) * 3 + (m - 1))   # Aux1→Mtx1 → 4E 27


def level_group_to_mtx(n: int, m: int) -> tuple[int, int]:
    return _addr(0x4E, 0x4B + (n - 1) * 3 + (m - 1))   # Grp1→Mtx1 → 4E 4B


def level_lr_master() -> tuple[int, int]:
    return _addr(0x4F, 0x00)                           # LR master → 4F 00


def level_aux_master(n: int) -> tuple[int, int]:
    return _addr(0x4F, 0x01 + (n - 1))                 # Aux1..12 master → 4F 01..0C


def level_fx_send_master(n: int) -> tuple[int, int]:
    return _addr(0x4F, 0x0D + (n - 1))                 # FX1Snd..4Snd master → 4F 0D..10


def level_mtx_master(n: int) -> tuple[int, int]:
    return _addr(0x4F, 0x11 + (n - 1))                 # Mtx1..3 master → 4F 11..13


def level_dca(n: int) -> tuple[int, int]:
    return _addr(0x4F, 0x20 + (n - 1))                 # DCA1..8 → 4F 20..27


# Pan/balance — MSB 0x50+ (sends) and 0x5F (master outs)

def pan_input_to_lr(n: int) -> tuple[int, int]:
    return _addr(0x50, n - 1)


def pan_input_to_aux(n: int, m: int) -> tuple[int, int]:
    return _addr(0x50, 0x44 + (n - 1) * 12 + (m - 1))


def pan_group_to_lr(n: int) -> tuple[int, int]:
    return _addr(0x50, 0x30 + (n - 1))


def pan_group_to_aux(n: int, m: int) -> tuple[int, int]:
    return _addr(0x55, 0x04 + (n - 1) * 12 + (m - 1))


def pan_fx_return_to_lr(n: int) -> tuple[int, int]:
    return _addr(0x50, 0x3C + (n - 1))


def pan_fx_return_to_aux(n: int, m: int) -> tuple[int, int]:
    return _addr(0x56, 0x14 + (n - 1) * 12 + (m - 1))


def pan_lr_to_mtx(m: int) -> tuple[int, int]:
    return _addr(0x5E, 0x24 + (m - 1))


def pan_aux_to_mtx(n: int, m: int) -> tuple[int, int]:
    return _addr(0x5E, 0x27 + (n - 1) * 3 + (m - 1))


def pan_group_to_mtx(n: int, m: int) -> tuple[int, int]:
    return _addr(0x5E, 0x4B + (n - 1) * 3 + (m - 1))


def pan_lr_master() -> tuple[int, int]:
    return _addr(0x5F, 0x00)                           # LR master balance → 5F 00


def pan_aux_master(n: int) -> tuple[int, int]:
    return _addr(0x5F, 0x01 + (n - 1))                 # Aux1..12 balance → 5F 01..0C


def pan_mtx_master(n: int) -> tuple[int, int]:
    return _addr(0x5F, 0x11 + (n - 1))                 # Mtx1..3 balance → 5F 11..13


# Mix assignments — MSB 0x60

def assign_input_to_aux(n: int, m: int) -> tuple[int, int]:
    return _addr(0x60, 0x44 + (n - 1) * 12 + (m - 1))


def assign_input_to_lr(n: int) -> tuple[int, int]:
    return _addr(0x60, n - 1)


# ── Value encoders/decoders ──────────────────────────────────────────────────

def level_to_vcvf(level: float) -> tuple[int, int]:
    """Map fader position (0.0..1.0) to (VC, VF) in Linear Taper.

    The 14-bit value scales linearly with fader position; the SQ's own
    fader-law curve translates that to dB. 0.0 → silence, 1.0 → +10 dB
    at the top of the fader.
    """
    level = max(LEVEL_MIN, min(LEVEL_MAX, level))
    value = round(level * VALUE_MAX)
    return (value >> 7) & 0x7F, value & 0x7F


def vcvf_to_level(vc: int, vf: int) -> float:
    value = ((vc & 0x7F) << 7) | (vf & 0x7F)
    return value / VALUE_MAX


def pan_to_vcvf(pan: float) -> tuple[int, int]:
    """Map pan (-1.0 full L .. 0.0 center .. +1.0 full R) to (VC, VF).

    Doc: L100% = 00 00, CTR = 3F 7F (8191), R100% = 7F 7F (16383).
    Floor (not round) hits all three documented anchors — the doc's center
    sits at 8191, half an LSB left of the arithmetic midpoint.
    """
    pan = max(PAN_LEFT, min(PAN_RIGHT, pan))
    value = min(VALUE_MAX, int((pan + 1.0) / 2.0 * VALUE_MAX))
    return (value >> 7) & 0x7F, value & 0x7F


def vcvf_to_pan(vc: int, vf: int) -> float:
    value = ((vc & 0x7F) << 7) | (vf & 0x7F)
    return (value / VALUE_MAX) * 2.0 - 1.0


def scene_to_bank_program(scene: int) -> tuple[int, int]:
    """Convert 1-based scene number (1..300) to (bank, program) hex pair.

    Bank 0 = scenes 1-128, Bank 1 = 129-256, Bank 2 = 257-300.
    Program is 0-based MIDI value (scene number minus 1, modulo 128).
    """
    if scene < MIN_SCENE or scene > MAX_SCENE:
        raise ValueError(f"Scene {scene} outside 1..{MAX_SCENE}")
    idx = scene - 1
    return idx // 128, idx % 128


def softkey_to_note(sk: int) -> int:
    """SoftKey 1..16 → MIDI note 0x30..0x3F (C3..D#4)."""
    if sk < 1 or sk > NUM_SOFTKEYS:
        raise ValueError(f"SoftKey {sk} outside 1..{NUM_SOFTKEYS}")
    return 0x30 + (sk - 1)


# ── Child entity types ───────────────────────────────────────────────────────
#
# Mutes relay at high cloud priority (a muted program feed is an incident);
# continuous levels/pans are low (chatty, dashboard-grade).

def _mute_prop() -> dict[str, Any]:
    return {"type": "boolean", "label": "Mute", "cloud_priority": "high",
            "control": True}


def _level_prop(label: str) -> dict[str, Any]:
    # Normalized 0..1 fader position (MIDI NRPN), not dB — no unit declared.
    return {"type": "number", "label": label, "min": LEVEL_MIN,
            "max": LEVEL_MAX, "cloud_priority": "low", "control": True}


def _pan_prop(label: str) -> dict[str, Any]:
    return {"type": "number", "label": label, "min": PAN_LEFT,
            "max": PAN_RIGHT, "cloud_priority": "low", "control": True}


CHILD_ENTITY_TYPES: dict[str, dict[str, Any]] = {
    "input": {
        "label": "Input", "label_plural": "Inputs",
        "state_variables": {
            "mute": _mute_prop(),
            "lr_level": _level_prop("Level (to LR)"),
            "lr_pan": _pan_prop("Pan (to LR)"),
        },
        "summary_fields": ["mute", "lr_level"],
    },
    "group": {
        "label": "Group", "label_plural": "Groups",
        "state_variables": {
            "mute": _mute_prop(),
            "lr_level": _level_prop("Level (to LR)"),
            "lr_pan": _pan_prop("Pan (to LR)"),
        },
        "summary_fields": ["mute", "lr_level"],
    },
    "aux": {
        "label": "Aux", "label_plural": "Auxes",
        "state_variables": {
            "mute": _mute_prop(),
            "fader": _level_prop("Master Fader"),
            "balance": _pan_prop("Balance"),
        },
        "summary_fields": ["mute", "fader"],
    },
    "matrix": {
        "label": "Matrix", "label_plural": "Matrices",
        "state_variables": {
            "mute": _mute_prop(),
            "fader": _level_prop("Master Fader"),
            "balance": _pan_prop("Balance"),
        },
        "summary_fields": ["mute", "fader"],
    },
    "fx_send": {
        "label": "FX Send", "label_plural": "FX Sends",
        "state_variables": {
            "mute": _mute_prop(),
            "fader": _level_prop("Master Fader"),
        },
        "summary_fields": ["mute", "fader"],
    },
    "fx_return": {
        "label": "FX Return", "label_plural": "FX Returns",
        "state_variables": {
            "mute": _mute_prop(),
            "lr_level": _level_prop("Level (to LR)"),
            "lr_pan": _pan_prop("Pan (to LR)"),
        },
        "summary_fields": ["mute", "lr_level"],
    },
    "dca": {
        "label": "DCA", "label_plural": "DCAs",
        "state_variables": {
            "mute": _mute_prop(),
            "fader": _level_prop("Fader"),
        },
        "summary_fields": ["mute", "fader"],
    },
    "mute_group": {
        "label": "Mute Group", "label_plural": "Mute Groups",
        "state_variables": {
            "mute": _mute_prop(),
        },
        "summary_fields": ["mute"],
    },
}

# Children are keyed by string local-ids (in01, aux03, dca1, ...) — declare a
# string id_format on each type; the platform otherwise defaults to integer
# ids and rejects strings at register_child.
for _ctype_def in CHILD_ENTITY_TYPES.values():
    _ctype_def["id_format"] = {"type": "string", "max_length": 64}


# The static roster: (child_type, count, sid template, label template).
# The SQ protocol has no model identify and all three SQ models share the
# same addressable channel set, so the roster is fixed.
_ROSTER: list[tuple[str, int, str, str]] = [
    ("input", NUM_INPUTS, "in{n:02d}", "Input {n}"),
    ("group", NUM_GROUPS, "grp{n:02d}", "Group {n}"),
    ("aux", NUM_AUX, "aux{n:02d}", "Aux {n}"),
    ("matrix", NUM_MTX, "mtx{n}", "Matrix {n}"),
    ("fx_send", NUM_FX_SENDS, "fxs{n}", "FX Send {n}"),
    ("fx_return", NUM_FX_RETURNS, "fxr{n}", "FX Return {n}"),
    ("dca", NUM_DCAS, "dca{n}", "DCA {n}"),
    ("mute_group", NUM_MUTE_GROUPS, "mg{n}", "Mute Group {n}"),
]

# Per-type (prop -> address function) for the parameters mirrored as child
# state. Drives registration-time route building AND the Get sweep.
_TYPE_ADDRS: dict[str, dict[str, Any]] = {
    "input": {"mute": mute_input, "lr_level": level_input_to_lr,
              "lr_pan": pan_input_to_lr},
    "group": {"mute": mute_group, "lr_level": level_group_to_lr,
              "lr_pan": pan_group_to_lr},
    "aux": {"mute": mute_aux_master, "fader": level_aux_master,
            "balance": pan_aux_master},
    "matrix": {"mute": mute_mtx_master, "fader": level_mtx_master,
               "balance": pan_mtx_master},
    "fx_send": {"mute": mute_fx_send, "fader": level_fx_send_master},
    "fx_return": {"mute": mute_fx_return, "lr_level": level_fx_return_to_lr,
                  "lr_pan": pan_fx_return_to_lr},
    "dca": {"mute": mute_dca, "fader": level_dca},
    "mute_group": {"mute": mute_mgrp},
}

# Which value semantics each child prop carries (for inbound decoding).
_MUTE_PROPS = {"mute"}
_PAN_PROPS = {"lr_pan", "balance"}


# ── Command catalog ──────────────────────────────────────────────────────────
#
# Generated so it stays in lock-step with the cmd_* methods on the driver
# class: every entry id maps to a `cmd_<id>` method and each param key matches
# that method's keyword argument.

_ACTION_PARAM = {
    "type": "enum",
    "values": ["on", "off", "toggle"],
    "default": "on",
    "required": True,
    "label": "Action",
    "help": "on, off, or toggle (toggle flips the console's current state "
            "and reads the result back).",
}

_DIRECTION_PARAM = {
    "type": "enum",
    "values": ["up", "down"],
    "default": "up",
    "required": True,
    "label": "Direction",
    "help": "Nudge the level one 1 dB step up or down.",
}

_LEVEL_PARAM = {
    "type": "number",
    "min": LEVEL_MIN,
    "max": LEVEL_MAX,
    "default": 0.75,
    "required": True,
    "label": "Level",
    "help": "Fader position 0.0 (-inf) to 1.0 (+10 dB at the top of the fader).",
}

_PAN_PARAM = {
    "type": "number",
    "min": PAN_LEFT,
    "max": PAN_RIGHT,
    "default": PAN_CENTER,
    "required": True,
    "label": "Pan",
    "help": "-1.0 full left, 0.0 center, +1.0 full right.",
}


def _child_param(ctype: str, label: str | None = None) -> dict[str, Any]:
    return {"type": "child_id", "child_type": ctype, "required": True,
            "label": label or CHILD_ENTITY_TYPES[ctype]["label"]}


def _chan(label: str, maximum: int, help_text: str | None = None) -> dict[str, Any]:
    return {
        "type": "integer",
        "required": True,
        "min": 1,
        "max": maximum,
        "label": label,
        "help": help_text or f"{label} number (1-{maximum}).",
    }


def _build_commands() -> dict[str, dict[str, Any]]:
    cmds: dict[str, dict[str, Any]] = {}

    def add(cid: str, label: str, help_text: str,
            params: dict[str, Any]) -> None:
        cmds[cid] = {"label": label, "help": help_text, "params": params}

    # Scene recall.
    add("recall_scene", "Recall Scene",
        f"Recall a scene {MIN_SCENE}-{MAX_SCENE} via Bank Select + Program "
        "Change. The scene must exist as a saved scene on the SQ.",
        {"scene": {"type": "integer", "required": True,
                   "min": MIN_SCENE, "max": MAX_SCENE, "label": "Scene"}})

    # SoftKeys.
    sk_param = {
        "softkey": _chan(
            "SoftKey", NUM_SOFTKEYS,
            "SoftKey number (1-16; SQ-5 has 8, so 9-16 are no-ops there)."),
    }
    add("softkey_press", "SoftKey Press", "Press and hold a SoftKey.",
        dict(sk_param))
    add("softkey_release", "SoftKey Release", "Release a held SoftKey.",
        dict(sk_param))
    add("softkey_pulse", "SoftKey Pulse",
        "Press then release a SoftKey — the usual one-shot trigger.",
        dict(sk_param))

    # Mutes (channel pickers).
    add("mute_input", "Mute Input", "Mute, unmute, or toggle an input.",
        {"input": _child_param("input"), "action": dict(_ACTION_PARAM)})
    add("mute_group", "Mute Group", "Mute, unmute, or toggle a group.",
        {"group": _child_param("group"), "action": dict(_ACTION_PARAM)})
    add("mute_aux_master", "Mute Aux Master",
        "Mute, unmute, or toggle an aux master.",
        {"aux": _child_param("aux"), "action": dict(_ACTION_PARAM)})
    add("mute_mtx_master", "Mute Matrix Master",
        "Mute, unmute, or toggle a matrix master.",
        {"mtx": _child_param("matrix"), "action": dict(_ACTION_PARAM)})
    add("mute_fx_send", "Mute FX Send", "Mute, unmute, or toggle an FX send.",
        {"fx": _child_param("fx_send"), "action": dict(_ACTION_PARAM)})
    add("mute_fx_return", "Mute FX Return",
        "Mute, unmute, or toggle an FX return.",
        {"fx": _child_param("fx_return"), "action": dict(_ACTION_PARAM)})
    add("mute_dca", "Mute DCA", "Mute, unmute, or toggle a DCA.",
        {"dca": _child_param("dca"), "action": dict(_ACTION_PARAM)})
    add("mute_mgrp", "Fire Mute Group",
        "Activate, clear, or toggle a mute group.",
        {"mgrp": _child_param("mute_group"), "action": dict(_ACTION_PARAM)})
    add("mute_lr", "Mute LR Master",
        "Mute, unmute, or toggle the LR (main) master.",
        {"action": dict(_ACTION_PARAM)})
    add("mute_all_inputs", "Mute All Inputs", "Mute every input channel.", {})
    add("unmute_all_inputs", "Unmute All Inputs",
        "Unmute every input channel.", {})

    # Levels (absolute fader position; channel pickers on the channel's own
    # strip, integer pairs on the send matrix).
    add("set_input_to_lr_level", "Set Input → LR Level",
        "Set an input's send level to the LR mix.",
        {"input": _child_param("input"), "level": dict(_LEVEL_PARAM)})
    add("set_input_to_aux_level", "Set Input → Aux Level",
        "Set an input's send level to an aux mix.",
        {"input": _chan("Input", NUM_INPUTS), "aux": _chan("Aux", NUM_AUX),
         "level": dict(_LEVEL_PARAM)})
    add("set_group_to_lr_level", "Set Group → LR Level",
        "Set a group's send level to the LR mix.",
        {"group": _child_param("group"), "level": dict(_LEVEL_PARAM)})
    add("set_group_to_aux_level", "Set Group → Aux Level",
        "Set a group's send level to an aux mix.",
        {"group": _chan("Group", NUM_GROUPS), "aux": _chan("Aux", NUM_AUX),
         "level": dict(_LEVEL_PARAM)})
    add("set_fx_return_to_lr_level", "Set FX Return → LR Level",
        "Set an FX return's send level to the LR mix.",
        {"fx": _child_param("fx_return"), "level": dict(_LEVEL_PARAM)})
    add("set_fx_return_to_aux_level", "Set FX Return → Aux Level",
        "Set an FX return's send level to an aux mix.",
        {"fx": _chan("FX Return", NUM_FX_RETURNS), "aux": _chan("Aux", NUM_AUX),
         "level": dict(_LEVEL_PARAM)})
    add("set_input_to_fx_send_level", "Set Input → FX Send Level",
        "Set an input's send level to an FX send.",
        {"input": _chan("Input", NUM_INPUTS),
         "fx": _chan("FX Send", NUM_FX_SENDS), "level": dict(_LEVEL_PARAM)})
    add("set_group_to_fx_send_level", "Set Group → FX Send Level",
        "Set a group's send level to an FX send.",
        {"group": _chan("Group", NUM_GROUPS),
         "fx": _chan("FX Send", NUM_FX_SENDS), "level": dict(_LEVEL_PARAM)})
    add("set_fx_return_to_fx_send_level", "Set FX Return → FX Send Level",
        "Set an FX return's send level to an FX send.",
        {"fx_return": _chan("FX Return", NUM_FX_RETURNS),
         "fx": _chan("FX Send", NUM_FX_SENDS), "level": dict(_LEVEL_PARAM)})
    add("set_lr_to_mtx_level", "Set LR → Matrix Level",
        "Set the LR mix's send level to a matrix.",
        {"mtx": _chan("Matrix", NUM_MTX), "level": dict(_LEVEL_PARAM)})
    add("set_aux_to_mtx_level", "Set Aux → Matrix Level",
        "Set an aux master's send level to a matrix.",
        {"aux": _chan("Aux", NUM_AUX), "mtx": _chan("Matrix", NUM_MTX),
         "level": dict(_LEVEL_PARAM)})
    add("set_group_to_mtx_level", "Set Group → Matrix Level",
        "Set a group's send level to a matrix.",
        {"group": _chan("Group", NUM_GROUPS), "mtx": _chan("Matrix", NUM_MTX),
         "level": dict(_LEVEL_PARAM)})
    add("set_lr_master_level", "Set LR Master Level",
        "Set the LR (main) master fader.", {"level": dict(_LEVEL_PARAM)})
    add("set_aux_master_level", "Set Aux Master Level",
        "Set an aux master fader.",
        {"aux": _child_param("aux"), "level": dict(_LEVEL_PARAM)})
    add("set_mtx_master_level", "Set Matrix Master Level",
        "Set a matrix master fader.",
        {"mtx": _child_param("matrix"), "level": dict(_LEVEL_PARAM)})
    add("set_fx_send_master_level", "Set FX Send Master Level",
        "Set an FX send master fader.",
        {"fx": _child_param("fx_send"), "level": dict(_LEVEL_PARAM)})
    add("set_dca_level", "Set DCA Level", "Set a DCA fader.",
        {"dca": _child_param("dca"), "level": dict(_LEVEL_PARAM)})

    # Level nudges (1 dB steps).
    add("step_input_to_lr_level", "Nudge Input → LR Level",
        "Step an input's LR send up or down 1 dB.",
        {"input": _child_param("input"), "direction": dict(_DIRECTION_PARAM)})
    add("step_aux_master_level", "Nudge Aux Master Level",
        "Step an aux master fader up or down 1 dB.",
        {"aux": _child_param("aux"), "direction": dict(_DIRECTION_PARAM)})
    add("step_lr_master_level", "Nudge LR Master Level",
        "Step the LR master fader up or down 1 dB.",
        {"direction": dict(_DIRECTION_PARAM)})
    add("step_dca_level", "Nudge DCA Level",
        "Step a DCA fader up or down 1 dB.",
        {"dca": _child_param("dca"), "direction": dict(_DIRECTION_PARAM)})

    # Pan / balance.
    add("set_input_to_lr_pan", "Set Input → LR Pan",
        "Pan an input within the LR mix.",
        {"input": _child_param("input"), "pan": dict(_PAN_PARAM)})
    add("set_input_to_aux_pan", "Set Input → Aux Pan",
        "Pan an input within an aux mix.",
        {"input": _chan("Input", NUM_INPUTS), "aux": _chan("Aux", NUM_AUX),
         "pan": dict(_PAN_PARAM)})
    add("set_group_to_lr_pan", "Set Group → LR Pan",
        "Pan a group within the LR mix.",
        {"group": _child_param("group"), "pan": dict(_PAN_PARAM)})
    add("set_fx_return_to_lr_pan", "Set FX Return → LR Pan",
        "Pan an FX return within the LR mix.",
        {"fx": _child_param("fx_return"), "pan": dict(_PAN_PARAM)})
    add("set_lr_to_mtx_balance", "Set LR → Matrix Balance",
        "Balance the LR send into a matrix.",
        {"mtx": _chan("Matrix", NUM_MTX), "pan": dict(_PAN_PARAM)})
    add("set_aux_to_mtx_balance", "Set Aux → Matrix Balance",
        "Balance an aux send into a matrix.",
        {"aux": _chan("Aux", NUM_AUX), "mtx": _chan("Matrix", NUM_MTX),
         "pan": dict(_PAN_PARAM)})
    add("set_lr_balance", "Set LR Master Balance",
        "Balance the LR (main) master output.", {"pan": dict(_PAN_PARAM)})
    add("set_aux_master_balance", "Set Aux Master Balance",
        "Balance an aux master output.",
        {"aux": _child_param("aux"), "pan": dict(_PAN_PARAM)})
    add("set_mtx_master_balance", "Set Matrix Master Balance",
        "Balance a matrix master output.",
        {"mtx": _child_param("matrix"), "pan": dict(_PAN_PARAM)})

    # Mix assignments.
    add("set_input_to_lr_assign", "Assign Input → LR",
        "Assign or unassign an input to the LR mix.",
        {"input": _chan("Input", NUM_INPUTS), "action": dict(_ACTION_PARAM)})
    add("set_input_to_aux_assign", "Assign Input → Aux",
        "Assign or unassign an input to an aux mix.",
        {"input": _chan("Input", NUM_INPUTS), "aux": _chan("Aux", NUM_AUX),
         "action": dict(_ACTION_PARAM)})

    # Refresh.
    add("refresh", "Refresh State",
        "Re-query every exposed parameter from the console.", {})

    return cmds


# ── Driver ───────────────────────────────────────────────────────────────────

class AllenHeathSQDriver(BaseDriver):
    """Allen & Heath SQ-5 / SQ-6 / SQ-7 MIDI-over-TCP driver."""

    # Liveness: an NRPN Get for the LR master level, awaited by parameter
    # address. Two missed replies force a typed no_response reconnect.
    HEALTH_FAULT_MESSAGE = (
        "Connected, but the SQ stopped answering Get requests — "
        "connection lost."
    )

    DRIVER_INFO = {
        "id": "allenheath_sq",
        "name": "Allen & Heath SQ Digital Mixer",
        "manufacturer": "Allen & Heath",
        "category": "audio",
        "version": "2.1.0",
        "author": "OpenAVC",
        "description": (
            "Controls Allen & Heath SQ-5, SQ-6 and SQ-7 digital mixing "
            "consoles via MIDI over TCP/IP on port 51325. Every input, "
            "group, aux, matrix, FX send/return, DCA and mute group is a "
            "child entity with live mute / level / pan state and a channel "
            "picker on its commands. Scene recall (1-300), 1 dB nudges, "
            "SoftKey triggers, master balances, and bidirectional state — "
            "console-side moves push back into OpenAVC, and a Get-probe "
            "watchdog flips the device offline if the console vanishes."
        ),
        "source_url": "https://www.allen-heath.com/content/uploads/2023/11/SQ-MIDI-Protocol-Issue5.pdf",
        "tags": ["mixer", "console", "midi", "nrpn", "allen-heath"],
        "verified": False,
        "simulated": True,
        "protocols": ["midi-over-tcp"],
        "ports": [51325],
        "transport": "tcp",
        "discovery": {
            # The SQ is the only Allen & Heath console with 48 inputs, so a Get
            # for Ip40's mute is a fingerprint no sibling can answer: a
            # Qu-5/6/7 shares this protocol but stops at 32 inputs plus
            # ST1/ST2/USB and is silent here (measured on a Qu-5), and the
            # original Qu / Avantis / dLive do not implement the NRPN Get at
            # all. OUI + port alone put all four A&H drivers on an equal
            # footing and identified nothing.
            #
            # The AHNet UDP 51320 announce would be the obvious passive signal,
            # but its wire format is not published and reverse-engineering it
            # is out of bounds — hence an active probe.
            "tcp_probe": {
                "port": 51325,
                "send_hex": "B0 63 00 B0 62 27 B0 60 7F",
                "expect_hex": "B0 63 00 B0 62 27",
                "timeout_ms": 2000,
            },
            "oui": ["00:04:c4"],
            "port_open": [51325],
            "manufacturer_alias": [
                "allen & heath", "allen and heath", "a&h",
                "allen-heath", "audiotonix",
            ],
        },
        # The connection lifecycle hooks this driver overrides landed in 0.24.0.
        "min_platform_version": "0.25.0",
        "compatible_models": [
            {
                "manufacturer": "Allen & Heath",
                "models": ["SQ-5", "SQ-6", "SQ-7"],
                "confidence": "untested",
                "notes": (
                    "Same MIDI command set and channel counts across all "
                    "three. SQ-5 has 8 SoftKeys (triggers 9-16 are no-ops). "
                    "SoftKeys are addressed by integer 1-16."
                ),
            },
        ],
        "child_entity_types": CHILD_ENTITY_TYPES,
        "default_config": {
            "host": "",
            "port": 51325,
            "midi_channel": 1,
            "poll_interval": 60,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
                "description": "SQ network IP address (Setup → Network on the console).",
            },
            "port": {
                "type": "integer",
                "default": 51325,
                "label": "TCP Port",
                "description": "MIDI over TCP/IP port. Always 51325 on SQ.",
            },
            "midi_channel": {
                "type": "integer",
                "default": 1,
                "min": 1,
                "max": 16,
                "label": "MIDI Channel",
                "description": (
                    "Must match the SQ's MIDI Channel setting "
                    "(Utility → General → MIDI). Default 1."
                ),
            },
            "poll_interval": {
                "type": "integer",
                "default": 60,
                "min": 0,
                "label": "Poll Interval (s)",
                "description": (
                    "Periodic full re-sweep backstop. Push keeps state "
                    "live; the sweep catches updates missed during "
                    "transient drops. 0 disables it."
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
                "Controls an Allen & Heath SQ digital mixing console over "
                "the network using MIDI over TCP/IP. Every channel strip is "
                "listed as a child entity with live mute, level and pan "
                "state — bind child state (e.g. device.<id>.input.in05.mute) "
                "to panel elements and drive mutes, faders and scene recall "
                "from macros and buttons. Console-side moves push straight "
                "back into OpenAVC state."
            ),
            "setup": (
                "1. Connect the SQ to the same network as the OpenAVC "
                "server.\n"
                "2. On the SQ, go to Utility → General → Network and note "
                "the IP address.\n"
                "3. Go to Utility → General → MIDI and note the MIDI "
                "Channel.\n"
                "4. Enter the IP address and matching MIDI channel in the "
                "device config. The default port (51325) is correct.\n"
                "5. Save. The channels appear under the device with live "
                "state; rename them in the project as needed (the SQ MIDI "
                "protocol does not expose console channel names)."
            ),
        },
        "state_variables": {
            "current_scene": {
                "type": "integer",
                "label": "Current Scene",
                "min": 0,
                "max": MAX_SCENE,
            },
            "lr_mute": {"type": "boolean", "label": "LR Master Mute",
                        "cloud_priority": "high"},
            "lr_fader": {
                "type": "number",
                "label": "LR Master Fader",
                "min": LEVEL_MIN,
                "max": LEVEL_MAX,
            },
            "lr_balance": {
                "type": "number",
                "label": "LR Master Balance",
                "min": PAN_LEFT,
                "max": PAN_RIGHT,
            },
        },
        "commands": _build_commands(),
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._rx_buf = bytearray()
        # NRPN aggregator: tracks the last MSB and LSB seen on each MIDI
        # channel so a 4-message sequence (BN 63, BN 62, BN 06, BN 26)
        # resolves to a single parameter update.
        self._nrpn_state: dict[int, dict[str, int]] = {
            ch: {"msb": 0, "lsb": 0, "vc": 0} for ch in range(16)
        }
        self._running_status = 0
        self._last_bank = 0

        # Child routing, built by _register_topology():
        #   _route: (MSB, LSB) -> (kind, ctype, sid, prop); ctype "lr" = flat.
        #   _child_num: (ctype, sid) -> 1-based channel number.
        self._route: dict[tuple[int, int], tuple[str, str, str, str]] = {}
        self._child_num: dict[tuple[str, str], int] = {}

        # Liveness probe bookkeeping (see _liveness_probe).
        self._probe_addr = level_lr_master()
        self._probe_fut: asyncio.Future[None] | None = None

    # ── Connection lifecycle ────────────────────────────────────────────

    async def _pre_connect(self) -> None:
        if not self.config.get("host"):
            raise ValueError("host is required")
        self._probe_fut = None

    def _transport_kwargs(
        self, transport_type: str, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        kwargs["delimiter"] = None      # raw MIDI byte stream, not line-framed
        return kwargs

    async def _initial_sync(self) -> None:
        # Register the (static) child roster, then sweep every parameter.
        self._register_topology()
        await asyncio.sleep(0.1)
        await self._refresh_all()

    # ── Topology registration ───────────────────────────────────────────

    def _register_topology(self) -> None:
        """Register the fixed channel roster as children and (re)build the
        inbound (MSB, LSB) -> child-prop route map. Safe to re-run —
        register_child is idempotent, and a driver-seeded label never
        overrides one the user set in the project."""
        self._route = {}
        self._child_num = {}

        for ctype, count, sid_tpl, label_tpl in _ROSTER:
            addrs = _TYPE_ADDRS[ctype]
            for n in range(1, count + 1):
                sid = sid_tpl.format(n=n)
                self._child_num[(ctype, sid)] = n
                for prop, addr_fn in addrs.items():
                    kind = ("mute" if prop in _MUTE_PROPS
                            else "pan" if prop in _PAN_PROPS
                            else "level")
                    self._route[addr_fn(n)] = (kind, ctype, sid, prop)
                initial = None
                project = self._project_child_entities.get(ctype, {}).get(sid)
                if not (project and project.get("label")):
                    initial = {"label": label_tpl.format(n=n)}
                self.register_child(ctype, sid, initial_state=initial)

        # LR master routes to flat device state.
        self._route[mute_lr()] = ("mute", "lr", "lr", "lr_mute")
        self._route[level_lr_master()] = ("level", "lr", "lr", "lr_fader")
        self._route[pan_lr_master()] = ("pan", "lr", "lr", "lr_balance")

    async def refresh_children(self) -> dict[str, Any]:
        """IDE 'Refresh from Device' + the Refresh quick action: re-sweep
        every exposed parameter."""
        if not (self.transport and self._connected):
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self._refresh_all()
        return {"channels": len(self._child_num)}

    # ── Polling backstop ────────────────────────────────────────────────

    async def poll(self) -> None:
        """Periodic re-sweep. Push covers the live case; this catches the
        occasional missed update during reconnects or transient drops.
        """
        if not self.connected:
            return
        await self._refresh_all()

    # ── Send helpers ────────────────────────────────────────────────────

    @property
    def _ch(self) -> int:
        """0-based MIDI channel for status bytes (config is 1-based)."""
        return int(self.config.get("midi_channel", 1)) - 1

    async def _send(self, data: bytes) -> None:
        if not self.transport:
            return
        await self.transport.send(data)

    def _build_nrpn(self, msb: int, lsb: int, vc: int, vf: int) -> bytes:
        """Full NRPN absolute-value sequence: BN 63 MSB BN 62 LSB BN 06 VC BN 26 VF."""
        b = 0xB0 | self._ch
        return bytes([b, 0x63, msb & 0x7F, b, 0x62, lsb & 0x7F,
                      b, 0x06, vc & 0x7F, b, 0x26, vf & 0x7F])

    def _build_nrpn_get(self, msb: int, lsb: int) -> bytes:
        """Get-current-value: BN 63 MSB BN 62 LSB BN 60 7F."""
        b = 0xB0 | self._ch
        return bytes([b, 0x63, msb & 0x7F, b, 0x62, lsb & 0x7F,
                      b, 0x60, 0x7F])

    def _build_nrpn_inc(self, msb: int, lsb: int) -> bytes:
        b = 0xB0 | self._ch
        return bytes([b, 0x63, msb & 0x7F, b, 0x62, lsb & 0x7F,
                      b, 0x60, 0x00])

    def _build_nrpn_dec(self, msb: int, lsb: int) -> bytes:
        b = 0xB0 | self._ch
        return bytes([b, 0x63, msb & 0x7F, b, 0x62, lsb & 0x7F,
                      b, 0x61, 0x00])

    def _build_mute(self, msb: int, lsb: int, on: bool) -> bytes:
        return self._build_nrpn(msb, lsb, 0x00, 0x01 if on else 0x00)

    def _build_assign(self, msb: int, lsb: int, on: bool) -> bytes:
        return self._build_nrpn(msb, lsb, 0x00, 0x01 if on else 0x00)

    # ── Liveness watchdog (BaseDriver health loop) ──────────────────────

    async def _liveness_probe(self) -> None:
        """Send an NRPN Get for the LR master level and await the reply.

        Pushes only happen when a console-side control moves, and poll
        Gets are fire-and-forget, so an SQ that vanished without a FIN
        would otherwise stay "online" forever. Replies aren't tagged, so
        the probe correlates by parameter address: any absolute for the
        probed address resolves it (the Get reply — or a console-side
        move of that same fader, which equally proves the console is
        alive). A push of any other parameter does NOT satisfy it.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        self._probe_fut = fut
        try:
            await self._send(self._build_nrpn_get(*self._probe_addr))
            await fut
        finally:
            self._probe_fut = None

    # ── Send-command dispatch ───────────────────────────────────────────

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        """Required BaseDriver entry point. Dispatches to a method on self."""
        params = params or {}
        method = getattr(self, f"cmd_{command}", None)
        if method is None:
            raise ValueError(f"Unknown command: {command}")
        return await method(**params)

    # ── Channel resolution ──────────────────────────────────────────────

    def _addr_of(self, ctype: str, sid: Any, addr_fn: Any) -> tuple[int, int]:
        n = self._child_num.get((ctype, str(sid)))
        if n is None:
            raise ValueError(f"Unknown {ctype}: {sid!r}")
        return addr_fn(n)

    # ── Commands: scene / SoftKeys ──────────────────────────────────────

    async def cmd_recall_scene(self, scene: int) -> None:
        bank, program = scene_to_bank_program(int(scene))
        b = 0xB0 | self._ch
        c = 0xC0 | self._ch
        await self._send(bytes([b, 0x00, bank, c, program]))
        self.set_state("current_scene", int(scene))

    async def cmd_softkey_press(self, softkey: int) -> None:
        note = softkey_to_note(int(softkey))
        await self._send(bytes([0x90 | self._ch, note, 0x7F]))

    async def cmd_softkey_release(self, softkey: int) -> None:
        note = softkey_to_note(int(softkey))
        await self._send(bytes([0x80 | self._ch, note, 0x00]))

    async def cmd_softkey_pulse(self, softkey: int) -> None:
        """Press + release in a single call. Most common SoftKey use."""
        await self.cmd_softkey_press(softkey)
        await asyncio.sleep(0.05)
        await self.cmd_softkey_release(softkey)

    # ── Commands: mutes ─────────────────────────────────────────────────

    async def _do_mute(self, msb: int, lsb: int, action: str) -> None:
        action = (action or "on").lower()
        if action == "toggle":
            # Native toggle, then read the result back — its outcome depends
            # on console-side state, and the console doesn't echo changes it
            # received over MIDI (A&H behavior confirmed on Qu hardware).
            await self._send(self._build_nrpn_inc(msb, lsb))
            await self._send(self._build_nrpn_get(msb, lsb))
        else:
            on = action == "on"
            await self._send(self._build_mute(msb, lsb, on))
            # Optimistic: mirror the commanded value (no echo on hardware).
            self._dispatch_absolute(msb, lsb, 0x00, 0x01 if on else 0x00)

    async def cmd_mute_input(self, input: Any, action: str = "on") -> None:
        await self._do_mute(*self._addr_of("input", input, mute_input), action)

    async def cmd_mute_group(self, group: Any, action: str = "on") -> None:
        await self._do_mute(*self._addr_of("group", group, mute_group), action)

    async def cmd_mute_aux_master(self, aux: Any, action: str = "on") -> None:
        await self._do_mute(*self._addr_of("aux", aux, mute_aux_master), action)

    async def cmd_mute_mtx_master(self, mtx: Any, action: str = "on") -> None:
        await self._do_mute(*self._addr_of("matrix", mtx, mute_mtx_master), action)

    async def cmd_mute_fx_send(self, fx: Any, action: str = "on") -> None:
        await self._do_mute(*self._addr_of("fx_send", fx, mute_fx_send), action)

    async def cmd_mute_fx_return(self, fx: Any, action: str = "on") -> None:
        await self._do_mute(*self._addr_of("fx_return", fx, mute_fx_return), action)

    async def cmd_mute_dca(self, dca: Any, action: str = "on") -> None:
        await self._do_mute(*self._addr_of("dca", dca, mute_dca), action)

    async def cmd_mute_mgrp(self, mgrp: Any, action: str = "on") -> None:
        await self._do_mute(*self._addr_of("mute_group", mgrp, mute_mgrp), action)

    async def cmd_mute_lr(self, action: str = "on") -> None:
        await self._do_mute(*mute_lr(), action)

    async def _do_mute_all_inputs(self, on: bool) -> None:
        for n in range(1, NUM_INPUTS + 1):
            msb, lsb = mute_input(n)
            await self._send(self._build_mute(msb, lsb, on))
            self._dispatch_absolute(msb, lsb, 0x00, 0x01 if on else 0x00)
            if n % 16 == 0:
                await asyncio.sleep(0.01)

    async def cmd_mute_all_inputs(self) -> None:
        await self._do_mute_all_inputs(True)

    async def cmd_unmute_all_inputs(self) -> None:
        await self._do_mute_all_inputs(False)

    # ── Commands: levels (absolute) ─────────────────────────────────────

    async def _do_level(self, msb: int, lsb: int, level: float) -> None:
        vc, vf = level_to_vcvf(float(level))
        await self._send(self._build_nrpn(msb, lsb, vc, vf))
        self._dispatch_absolute(msb, lsb, vc, vf)

    async def cmd_set_input_to_lr_level(self, input: Any, level: float) -> None:
        await self._do_level(*self._addr_of("input", input, level_input_to_lr), level)

    async def cmd_set_input_to_aux_level(self, input: int, aux: int, level: float) -> None:
        await self._do_level(*level_input_to_aux(int(input), int(aux)), level)

    async def cmd_set_group_to_lr_level(self, group: Any, level: float) -> None:
        await self._do_level(*self._addr_of("group", group, level_group_to_lr), level)

    async def cmd_set_group_to_aux_level(self, group: int, aux: int, level: float) -> None:
        await self._do_level(*level_group_to_aux(int(group), int(aux)), level)

    async def cmd_set_fx_return_to_lr_level(self, fx: Any, level: float) -> None:
        await self._do_level(*self._addr_of("fx_return", fx, level_fx_return_to_lr), level)

    async def cmd_set_fx_return_to_aux_level(self, fx: int, aux: int, level: float) -> None:
        await self._do_level(*level_fx_return_to_aux(int(fx), int(aux)), level)

    async def cmd_set_input_to_fx_send_level(self, input: int, fx: int, level: float) -> None:
        await self._do_level(*level_input_to_fx_send(int(input), int(fx)), level)

    async def cmd_set_group_to_fx_send_level(self, group: int, fx: int, level: float) -> None:
        await self._do_level(*level_group_to_fx_send(int(group), int(fx)), level)

    async def cmd_set_fx_return_to_fx_send_level(self, fx_return: int, fx: int,
                                                 level: float) -> None:
        await self._do_level(*level_fx_return_to_fx_send(int(fx_return), int(fx)), level)

    async def cmd_set_lr_to_mtx_level(self, mtx: int, level: float) -> None:
        await self._do_level(*level_lr_to_mtx(int(mtx)), level)

    async def cmd_set_aux_to_mtx_level(self, aux: int, mtx: int, level: float) -> None:
        await self._do_level(*level_aux_to_mtx(int(aux), int(mtx)), level)

    async def cmd_set_group_to_mtx_level(self, group: int, mtx: int, level: float) -> None:
        await self._do_level(*level_group_to_mtx(int(group), int(mtx)), level)

    async def cmd_set_lr_master_level(self, level: float) -> None:
        await self._do_level(*level_lr_master(), level)

    async def cmd_set_aux_master_level(self, aux: Any, level: float) -> None:
        await self._do_level(*self._addr_of("aux", aux, level_aux_master), level)

    async def cmd_set_mtx_master_level(self, mtx: Any, level: float) -> None:
        await self._do_level(*self._addr_of("matrix", mtx, level_mtx_master), level)

    async def cmd_set_fx_send_master_level(self, fx: Any, level: float) -> None:
        await self._do_level(*self._addr_of("fx_send", fx, level_fx_send_master), level)

    async def cmd_set_dca_level(self, dca: Any, level: float) -> None:
        await self._do_level(*self._addr_of("dca", dca, level_dca), level)

    # ── Commands: levels (relative 1 dB step) ───────────────────────────

    async def _do_step(self, msb: int, lsb: int, direction: str) -> None:
        if (direction or "up").lower() == "up":
            await self._send(self._build_nrpn_inc(msb, lsb))
        else:
            await self._send(self._build_nrpn_dec(msb, lsb))
        # Read the result back — the console doesn't echo received changes.
        await self._send(self._build_nrpn_get(msb, lsb))

    async def cmd_step_input_to_lr_level(self, input: Any, direction: str = "up") -> None:
        await self._do_step(*self._addr_of("input", input, level_input_to_lr), direction)

    async def cmd_step_aux_master_level(self, aux: Any, direction: str = "up") -> None:
        await self._do_step(*self._addr_of("aux", aux, level_aux_master), direction)

    async def cmd_step_lr_master_level(self, direction: str = "up") -> None:
        await self._do_step(*level_lr_master(), direction)

    async def cmd_step_dca_level(self, dca: Any, direction: str = "up") -> None:
        await self._do_step(*self._addr_of("dca", dca, level_dca), direction)

    # ── Commands: panning ───────────────────────────────────────────────

    async def _do_pan(self, msb: int, lsb: int, pan: float) -> None:
        vc, vf = pan_to_vcvf(float(pan))
        await self._send(self._build_nrpn(msb, lsb, vc, vf))
        self._dispatch_absolute(msb, lsb, vc, vf)

    async def cmd_set_input_to_lr_pan(self, input: Any, pan: float) -> None:
        await self._do_pan(*self._addr_of("input", input, pan_input_to_lr), pan)

    async def cmd_set_input_to_aux_pan(self, input: int, aux: int, pan: float) -> None:
        await self._do_pan(*pan_input_to_aux(int(input), int(aux)), pan)

    async def cmd_set_group_to_lr_pan(self, group: Any, pan: float) -> None:
        await self._do_pan(*self._addr_of("group", group, pan_group_to_lr), pan)

    async def cmd_set_fx_return_to_lr_pan(self, fx: Any, pan: float) -> None:
        await self._do_pan(*self._addr_of("fx_return", fx, pan_fx_return_to_lr), pan)

    async def cmd_set_lr_to_mtx_balance(self, mtx: int, pan: float) -> None:
        await self._do_pan(*pan_lr_to_mtx(int(mtx)), pan)

    async def cmd_set_aux_to_mtx_balance(self, aux: int, mtx: int, pan: float) -> None:
        await self._do_pan(*pan_aux_to_mtx(int(aux), int(mtx)), pan)

    async def cmd_set_lr_balance(self, pan: float) -> None:
        await self._do_pan(*pan_lr_master(), pan)

    async def cmd_set_aux_master_balance(self, aux: Any, pan: float) -> None:
        await self._do_pan(*self._addr_of("aux", aux, pan_aux_master), pan)

    async def cmd_set_mtx_master_balance(self, mtx: Any, pan: float) -> None:
        await self._do_pan(*self._addr_of("matrix", mtx, pan_mtx_master), pan)

    # ── Commands: mix assignments ───────────────────────────────────────

    async def _do_assign(self, msb: int, lsb: int, action: str) -> None:
        action = (action or "on").lower()
        if action == "toggle":
            await self._send(self._build_nrpn_inc(msb, lsb))
        else:
            await self._send(self._build_assign(msb, lsb, action == "on"))

    async def cmd_set_input_to_lr_assign(self, input: int, action: str = "on") -> None:
        await self._do_assign(*assign_input_to_lr(int(input)), action)

    async def cmd_set_input_to_aux_assign(self, input: int, aux: int, action: str = "on") -> None:
        await self._do_assign(*assign_input_to_aux(int(input), int(aux)), action)

    # ── Commands: refresh / get ─────────────────────────────────────────

    async def cmd_refresh(self) -> None:
        """Manually re-sweep state. Same as the on-connect query."""
        await self._refresh_all()

    async def _refresh_all(self) -> None:
        """Issue NRPN get requests for every parameter we expose as state.

        Sequenced with small sleeps to avoid overwhelming the console
        socket buffer on a fresh connect.
        """
        if not self.connected:
            return

        targets: list[tuple[int, int]] = list(self._route.keys())
        for i, (msb, lsb) in enumerate(targets):
            try:
                await self._send(self._build_nrpn_get(msb, lsb))
            except Exception:  # noqa: BLE001
                break
            # Yield every 16 messages to keep the socket flowing.
            if i % 16 == 15:
                await asyncio.sleep(0.01)

    # ── MIDI parser (incoming) ──────────────────────────────────────────

    def on_data_received(self, data: bytes) -> None:
        """Called by the transport for every chunk of incoming bytes.

        Buffers, then parses MIDI messages out byte-by-byte. NRPN
        sequences are aggregated across messages (the SQ may use
        running status, so we maintain the last-seen status per
        channel).
        """
        if not data:
            return
        self._rx_buf.extend(data)
        self._parse()

    def _parse(self) -> None:
        # Status-byte state machine. MIDI 1.0 wire format:
        #   1xxx_xxxx = status, 0xxx_xxxx = data.
        # Running status (a status byte applies until a new one arrives)
        # is supported.
        buf = self._rx_buf
        running_status = self._running_status

        i = 0
        while i < len(buf):
            b = buf[i]

            # System Real-Time bytes (0xF8-0xFF) are single-byte and don't
            # affect running status. Skip them in place.
            if 0xF8 <= b <= 0xFF:
                i += 1
                continue

            if b & 0x80:
                # Status byte
                # System Common (0xF0-0xF7) — clear running status; we
                # don't process SysEx beyond skipping to End-of-Exclusive.
                if 0xF0 <= b <= 0xF7:
                    if b == 0xF0:
                        # Skip until 0xF7 — if not present yet, wait for more bytes.
                        end = buf.find(0xF7, i + 1)
                        if end == -1:
                            break  # incomplete
                        i = end + 1
                        running_status = 0
                        continue
                    # Other system common — most are 1 or 2 data bytes;
                    # we don't use any of these. Skip the status byte; if
                    # we under-skip a data byte, the next iteration's
                    # 0x80-bit check resyncs.
                    i += 1
                    running_status = 0
                    continue
                running_status = b
                i += 1
                continue

            # Data byte — must have a status to apply to.
            if not running_status:
                # Lost sync; drop one byte and retry.
                i += 1
                continue

            # Channel voice messages we care about:
            #   8N nn vv  (3 bytes) Note Off
            #   9N nn vv  (3 bytes) Note On
            #   BN cc vv  (3 bytes) Controller (CC / NRPN building blocks)
            #   CN pp     (2 bytes) Program Change
            #   FN ...    handled above
            high = running_status & 0xF0
            ch = running_status & 0x0F

            if high in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
                if i + 1 >= len(buf):
                    break  # incomplete
                d1 = buf[i]
                d2 = buf[i + 1]
                i += 2
                if d1 & 0x80 or d2 & 0x80:
                    # Resync — corrupt frame.
                    running_status = 0
                    continue
                if high == 0xB0:
                    self._handle_cc(ch, d1, d2)
            elif high in (0xC0, 0xD0):
                if i >= len(buf):
                    break  # incomplete
                d1 = buf[i]
                i += 1
                if d1 & 0x80:
                    running_status = 0
                    continue
                if high == 0xC0:
                    self._handle_program_change(ch, d1)
            else:
                # Shouldn't reach here for status bytes covered above.
                i += 1

        self._running_status = running_status
        # Keep only unparsed bytes.
        del buf[:i]

    def _handle_cc(self, ch: int, controller: int, value: int) -> None:
        """Aggregate NRPN building blocks for the given channel.

        NRPN 4-message sequence (absolute set):
          63 MSB  → store msb
          62 LSB  → store lsb
          06 VC   → store vc (coarse data)
          26 VF   → fire absolute set with (msb, lsb, vc, vf)

        NRPN 3-message sequence (mute/assign/get/inc/dec):
          63 MSB
          62 LSB
          60 00 → toggle (or get-response if value == 7F)
          60 7F → get request (we only emit, never receive these)
          61 00 → decrement
          06 VC + 26 VF → absolute (handled above)

        The SQ encodes mute on/off as a 4-message sequence where VC is
        always 0x00 and VF is 0x00 (off) or 0x01 (on); levels and pans
        use the 4-message form with full 14-bit (VC, VF) values.
        """
        if controller == 0x00:
            # Bank Select MSB — used by scene change ("BN 00 BK" then "CN PG").
            self._handle_bank_select_msb(ch, value)
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
        elif controller == 0x60:
            # Increment — toggle (mute/assign) or no-op for an outgoing get.
            # On incoming side, 0x00 is a console-side toggle echo.
            if value == 0x00:
                self._dispatch_toggle(nrpn["msb"], nrpn["lsb"])
        elif controller == 0x61:
            # Decrement — ignored as an aggregator hint; we update state
            # on the absolute echo the SQ emits afterward.
            pass

    def _handle_program_change(self, ch: int, program: int) -> None:
        # Console-side scene change: Bank Select (B0 00 BK) then Program
        # Change (CN PG). The bank is cached from the preceding CC 0x00.
        if ch != self._ch:
            return
        scene = self._last_bank * 128 + program + 1
        if MIN_SCENE <= scene <= MAX_SCENE:
            self.set_state("current_scene", scene)

    def _handle_bank_select_msb(self, ch: int, value: int) -> None:
        if ch == self._ch:
            self._last_bank = value

    # ── State fan-out ───────────────────────────────────────────────────

    def _dispatch_absolute(self, msb: int, lsb: int, vc: int, vf: int) -> None:
        """Map an absolute (MSB, LSB, VC, VF) tuple to a state update.

        Shared by the inbound parser AND the optimistic write path, so a
        sent value and a received value land in state identically.
        """
        key = (msb, lsb)

        # Resolve the liveness probe on a reply for the probed address.
        if (self._probe_fut is not None and not self._probe_fut.done()
                and key == self._probe_addr):
            self._probe_fut.set_result(None)

        route = self._route.get(key)
        if route is None:
            # Unknown parameter — ignore. (E.g. send-level matrix cells we
            # don't expose as state but that another client may set.)
            return
        kind, ctype, sid, prop = route

        if kind == "mute":
            value: Any = bool(vf & 0x01)
        elif kind == "pan":
            value = vcvf_to_pan(vc, vf)
        else:
            value = vcvf_to_level(vc, vf)

        if ctype == "lr":
            self.set_state(prop, value)
            return
        try:
            self.set_child_state_batch(ctype, sid, {prop: value})
        except Exception:  # noqa: BLE001
            pass

    def _dispatch_toggle(self, msb: int, lsb: int) -> None:
        """Console-side mute toggle echo. Flip the state we hold."""
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
        except Exception:  # noqa: BLE001
            pass


# Class export expected by the loader.
DRIVER_CLASS = AllenHeathSQDriver
