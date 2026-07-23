"""
OpenAVC Allen & Heath Qu Driver — first-class auto-identify edition.

Controls the original Allen & Heath Qu family (Qu-16, Qu-24, Qu-32, Qu-Pac,
Qu-SB) over MIDI-over-TCP/IP on port 51325. The wire format is the standard
MIDI 1.0 byte stream carried over a raw TCP socket (running status supported).
Mixer parameters are addressed with NRPN sequences; mutes are Note On/Off;
scene recall is Bank Select + Program Change; a small vendor SysEx layer
carries model identification, channel names, and full-state sync.

What makes this driver first-class
-----------------------------------
* **Auto-identify + state sync on connect.** The Qu answers an "All-Call"
  SysEx with its model (Qu-16 … Qu-SB), firmware, and its MIDI channel, then
  pushes its entire current parameter state. The driver reads the model,
  registers exactly the right channels as **child entities**, learns the MIDI
  channel (no manual config needed), and populates live state from the push —
  the integrator declares nothing.
* **Channels as typed child entities.** Every input, stereo input, mix, group,
  matrix, DCA, mute group, and FX send/return becomes a child with live
  mute / fader / pan state, labelled by its real channel **name** read back
  from the console. Console-side moves push into state so panels stay in sync.
* **Active-sense keep-alive as the liveness signal.** The Qu drops any TCP MIDI
  client that goes silent for 12 s, and streams Active Sense (FE) itself every
  ~300 ms. The driver sends FE on a timer (keep-alive) and watches the inbound
  FE stream (liveness) — a vanished console stops sending, and the driver tears
  the socket down for a clean reconnect.

Scope (this version):
    Live mixing / routing surface — mutes, faders (position + dB), pan, send
    levels / pan / assign / pre-post, LR & mix / group / matrix / DCA / mute-
    group assigns, DCA and mute-group masters, PAFL, local preamp gain / 48V,
    scene recall, MMC transport, channel naming, remote shutdown. Deep per-
    channel DSP (PEQ, GEQ, gate, compressor, delay, HPF, trim, polarity,
    insert, dSNAKE preamp, FX tap tempo) is a planned follow-up increment.

Why Python (not a YAML .avcdriver):
    1) Binary MIDI wire format — NRPN aggregation with running status, Note-On
       mute pairs, and a vendor SysEx layer, none of which fit the declarative
       text/HTTP grammar.
    2) Runtime-registered, per-model child topology — the console reports its
       model and the driver registers the matching child set via
       register_child(); YAML child types are static.
    3) Push fan-out — an inbound NRPN / Note-On stream fans into many child
       state keys via a (channel-select byte) -> child route map.

Source (verified against the published protocol):
    https://www.allen-heath.com/content/uploads/2023/06/Qu_MIDI_Protocol_V1.9.pdf
"""

from __future__ import annotations

import asyncio
from typing import Any

from server.drivers.base import BaseDriver, ConnectionFaultError
from server.utils.logger import get_logger

log = get_logger(__name__)


# ── Wire-protocol constants ──────────────────────────────────────────────────

DEFAULT_PORT = 51325

# MIDI system real-time: Active Sensing. The Qu sends this ~every 300 ms and
# closes the socket if it receives nothing for 12 s; we send it as keep-alive.
ACTIVE_SENSE = 0xFE

# Vendor SysEx header: F0 00 00 1A 50 11 01 00 0N  (0N = the mixer's MIDI
# channel; 7F for an "All-Call" that any channel answers).
SYSEX_MANUFACTURER = bytes([0x00, 0x00, 0x1A])   # Allen & Heath
SYSEX_MODEL = 0x50                                # Qu
SYSEX_VER = bytes([0x11, 0x01, 0x00])             # protocol major/minor markers
SYSEX_ALL_CALL_CH = 0x7F

# SysEx message kinds (the byte after the header).
SX_GET_NAME = 0x01
SX_NAME_REPLY = 0x02
SX_SET_NAME = 0x03
SX_SYSTEM_STATE_REQ = 0x10   # + <iPadFlag>
SX_SYSTEM_STATE_REPLY = 0x11  # + <BoxID> <VerMajor> <VerMinor>
SX_METER_REQ = 0x12
SX_METER_REPLY = 0x13
SX_END_SYNC = 0x14

# BoxID (from the System State reply) -> model key.
BOX_ID_MODELS = {1: "Qu-16", 2: "Qu-24", 3: "Qu-32", 4: "Qu-Pac", 5: "Qu-SB"}

# NRPN framing bytes.
CC_NRPN_MSB = 0x63   # selects the channel (CH)
CC_NRPN_LSB = 0x62   # selects the parameter (ID)
CC_DATA_MSB = 0x06   # parameter value (VA)
CC_DATA_LSB = 0x26   # index / second value (VX)
CC_BANK_MSB = 0x00
CC_BANK_LSB = 0x20

# NRPN parameter IDs (the LSB).
ID_FADER = 0x17
ID_PAN = 0x16
ID_LR_ASSIGN = 0x18
ID_MIX_ASSIGN = 0x55
ID_MIX_PREPOST = 0x50
ID_SEND_LEVEL = 0x20
ID_MUTEGRP_ASSIGN = 0x5C
ID_DCA_ASSIGN = 0x40
ID_PAFL = 0x51
ID_PREAMP_GAIN = 0x19
ID_PREAMP_48V = 0x69
ID_REMOTE_SHUTDOWN = 0x5F

VX_MAIN = 0x07   # the default index used by single-destination parameters

# MMC transport codes (real-time SysEx F0 7F 7F 06 TC F7).
MMC_CODES = {
    "stop": 0x01, "play": 0x02, "ff": 0x04,
    "rewind": 0x05, "record": 0x06, "pause": 0x09,
}

# Scene range.
MIN_SCENE = 1
MAX_SCENE = 100

# Keep-alive / liveness timing (seconds).
ACTIVE_SENSE_TX_INTERVAL = 4.0    # send FE well inside the Qu's 12 s window
RX_SILENCE_TIMEOUT = 10.0         # no inbound bytes this long => console gone
SYSTEM_STATE_TIMEOUT = 3.0        # wait for the All-Call model reply


# ── Value tables (byte <-> engineering units) ────────────────────────────────
#
# The Qu fader/send taper and preamp-gain taper are published as sparse
# (unit, byte) tables. We interpolate linearly between the tabulated points so
# a dB request lands on the nearest sensible byte, and decode inbound bytes the
# same way.

# Fader / send level: dB -> byte. -inf is byte 0x00.
_FADER_DB_TABLE = [
    (-90.0, 0x00), (-45.0, 0x0C), (-40.0, 0x10), (-35.0, 0x17), (-30.0, 0x1F),
    (-25.0, 0x27), (-20.0, 0x2F), (-15.0, 0x36), (-10.0, 0x3F), (-5.0, 0x4F),
    (0.0, 0x62), (5.0, 0x72), (10.0, 0x7F),
]

# Local preamp gain: dB -> byte.
_GAIN_DB_TABLE = [
    (-5.0, 0x00), (0.0, 0x0A), (5.0, 0x13), (10.0, 0x1D), (20.0, 0x30),
    (30.0, 0x44), (40.0, 0x57), (50.0, 0x6B), (60.0, 0x7F),
]

FADER_DB_MIN = -90.0
FADER_DB_MAX = 10.0
GAIN_DB_MIN = -5.0
GAIN_DB_MAX = 60.0

# Pan: -1.0 (full left) .. 0.0 (centre) .. +1.0 (full right) maps linearly onto
# byte 0x00 .. 0x25 .. 0x4A.
PAN_LEFT_BYTE = 0x00
PAN_CENTRE_BYTE = 0x25
PAN_RIGHT_BYTE = 0x4A


def _interp(table: list[tuple[float, int]], value: float) -> int:
    """Interpolate a (unit -> byte) table; clamp outside the endpoints."""
    lo_u, lo_b = table[0]
    hi_u, hi_b = table[-1]
    if value <= lo_u:
        return lo_b
    if value >= hi_u:
        return hi_b
    for (u0, b0), (u1, b1) in zip(table, table[1:]):
        if u0 <= value <= u1:
            if u1 == u0:
                return b0
            frac = (value - u0) / (u1 - u0)
            return max(0, min(0x7F, round(b0 + frac * (b1 - b0))))
    return hi_b


def _interp_inv(table: list[tuple[float, int]], byte: int) -> float:
    """Inverse of _interp: byte -> unit, interpolating between tabulated bytes."""
    byte = max(0, min(0x7F, byte))
    lo_u, lo_b = table[0]
    hi_u, hi_b = table[-1]
    if byte <= lo_b:
        return lo_u
    if byte >= hi_b:
        return hi_u
    for (u0, b0), (u1, b1) in zip(table, table[1:]):
        if b0 <= byte <= b1:
            if b1 == b0:
                return u0
            frac = (byte - b0) / (b1 - b0)
            return u0 + frac * (u1 - u0)
    return hi_u


def fader_db_to_byte(db: float) -> int:
    return _interp(_FADER_DB_TABLE, db)


def byte_to_fader_db(byte: int) -> float:
    return _interp_inv(_FADER_DB_TABLE, byte)


def fader_pos_to_byte(pos: float) -> int:
    """Fader position 0.0..1.0 -> byte 0x00..0x7F (linear over the fader throw)."""
    pos = max(0.0, min(1.0, pos))
    return round(pos * 0x7F)


def byte_to_fader_pos(byte: int) -> float:
    return max(0, min(0x7F, byte)) / 0x7F


def gain_db_to_byte(db: float) -> int:
    return _interp(_GAIN_DB_TABLE, db)


def byte_to_gain_db(byte: int) -> float:
    return _interp_inv(_GAIN_DB_TABLE, byte)


def pan_to_byte(pan: float) -> int:
    pan = max(-1.0, min(1.0, pan))
    return round((pan + 1.0) / 2.0 * PAN_RIGHT_BYTE)


def byte_to_pan(byte: int) -> float:
    byte = max(0, min(PAN_RIGHT_BYTE, byte))
    return (byte / PAN_RIGHT_BYTE) * 2.0 - 1.0


# ── Channel-select (CH) addressing ───────────────────────────────────────────
#
# The NRPN MSB / Note-On note number selects the channel. Values per the
# protocol reference table (page 3 / 13).

def input_ch(n: int) -> int:
    return 0x20 + (n - 1)          # Input 1..32 -> 0x20..0x3F


def stereo_ch(n: int) -> int:
    return 0x40 + (n - 1)          # ST1..ST3 -> 0x40..0x42


def dca_ch(n: int) -> int:
    return 0x10 + (n - 1)          # DCA 1..4 -> 0x10..0x13


def mute_group_ch(n: int) -> int:
    return 0x50 + (n - 1)          # Mute Group 1..4 -> 0x50..0x53


def fx_send_ch(n: int) -> int:
    return 0x00 + (n - 1)          # FX Send 1..4 -> 0x00..0x03


def fx_return_ch(n: int) -> int:
    return 0x08 + (n - 1)          # FX Return 1..4 -> 0x08..0x0B


def group_ch(pair: int) -> int:
    return 0x68 + (pair - 1)       # Grp 1-2..7-8 -> 0x68..0x6B


def matrix_ch(pair: int) -> int:
    return 0x6C + (pair - 1)       # MTX 1-2..3-4 -> 0x6C..0x6D


LR_CH = 0x67                       # Main LR master

# The 7 mix masters shared by every Qu model, as (index, id, label, CH, send-VX).
MIXES: list[tuple[int, str, str, int, int]] = [
    (1, "mix1", "Mix 1", 0x60, 0x00),
    (2, "mix2", "Mix 2", 0x61, 0x01),
    (3, "mix3", "Mix 3", 0x62, 0x02),
    (4, "mix4", "Mix 4", 0x63, 0x03),
    (5, "mix56", "Mix 5-6", 0x64, 0x04),
    (6, "mix78", "Mix 7-8", 0x65, 0x05),
    (7, "mix910", "Mix 9-10", 0x66, 0x06),
]
LR_SEND_VX = 0x07


# ── Per-model topology ───────────────────────────────────────────────────────
#
# Every Qu shares the same mix bus set (Mix 1-4 mono + Mix 5-6/7-8/9-10 stereo
# + LR), 3 stereo inputs, 4 FX returns, 4 DCAs and 4 mute groups. They differ
# in mono input count, Group / Matrix availability, and FX-send count.

MODEL_TOPOLOGY: dict[str, dict[str, int]] = {
    #            inputs  group_pairs  matrix_pairs  fx_sends
    "Qu-16":  {"inputs": 16, "group_pairs": 0, "matrix_pairs": 0, "fx_sends": 2},
    "Qu-24":  {"inputs": 24, "group_pairs": 2, "matrix_pairs": 2, "fx_sends": 4},
    "Qu-32":  {"inputs": 32, "group_pairs": 4, "matrix_pairs": 2, "fx_sends": 4},
    "Qu-Pac": {"inputs": 32, "group_pairs": 4, "matrix_pairs": 2, "fx_sends": 4},
    "Qu-SB":  {"inputs": 32, "group_pairs": 4, "matrix_pairs": 2, "fx_sends": 4},
}
STEREO_INPUTS = 3
FX_RETURNS = 4
DCAS = 4
MUTE_GROUPS = 4
GROUP_PAIR_LABELS = ["Grp 1-2", "Grp 3-4", "Grp 5-6", "Grp 7-8"]
MATRIX_PAIR_LABELS = ["Mtx 1-2", "Mtx 3-4"]


# ── Child entity types ───────────────────────────────────────────────────────

_MUTE_ONLY = {
    "name": {"type": "string", "label": "Name"},
    "mute": {"type": "boolean", "label": "Mute", "control": True},
}
_FADER_CHILD = {
    "name": {"type": "string", "label": "Name"},
    "mute": {"type": "boolean", "label": "Mute", "control": True},
    "fader": {"type": "number", "label": "Fader", "min": 0.0, "max": 1.0,
              "control": True},
    "fader_db": {"type": "number", "label": "Fader (dB)",
                 "min": FADER_DB_MIN, "max": FADER_DB_MAX,
                 "unit": "dB", "control": True},
}
_FADER_PAN_CHILD = {
    **_FADER_CHILD,
    "pan": {"type": "number", "label": "Pan", "min": -1.0, "max": 1.0,
            "control": True},
}

CHILD_ENTITY_TYPES: dict[str, dict[str, Any]] = {
    "input": {
        "label": "Input", "label_plural": "Inputs",
        "state_variables": dict(_FADER_PAN_CHILD),
        "summary_fields": ["name", "mute", "fader_db"], "label_field": "name",
    },
    "stereo_input": {
        "label": "Stereo Input", "label_plural": "Stereo Inputs",
        "state_variables": dict(_FADER_PAN_CHILD),
        "summary_fields": ["name", "mute", "fader_db"], "label_field": "name",
    },
    "mix": {
        "label": "Mix", "label_plural": "Mixes",
        "state_variables": dict(_FADER_CHILD),
        "summary_fields": ["name", "mute", "fader_db"], "label_field": "name",
    },
    "group": {
        "label": "Group", "label_plural": "Groups",
        "state_variables": dict(_FADER_CHILD),
        "summary_fields": ["name", "mute", "fader_db"], "label_field": "name",
    },
    "matrix": {
        "label": "Matrix", "label_plural": "Matrices",
        "state_variables": dict(_FADER_CHILD),
        "summary_fields": ["name", "mute", "fader_db"], "label_field": "name",
    },
    "dca": {
        "label": "DCA", "label_plural": "DCAs",
        "state_variables": dict(_FADER_CHILD),
        "summary_fields": ["name", "mute", "fader_db"], "label_field": "name",
    },
    "mute_group": {
        "label": "Mute Group", "label_plural": "Mute Groups",
        "state_variables": dict(_MUTE_ONLY),
        "summary_fields": ["name", "mute"], "label_field": "name",
    },
    "fx_send": {
        "label": "FX Send", "label_plural": "FX Sends",
        "state_variables": dict(_FADER_CHILD),
        "summary_fields": ["name", "mute", "fader_db"], "label_field": "name",
    },
    "fx_return": {
        "label": "FX Return", "label_plural": "FX Returns",
        "state_variables": dict(_FADER_PAN_CHILD),
        "summary_fields": ["name", "mute", "fader_db"], "label_field": "name",
    },
}

# Every child is keyed by a sanitized string local-id (in01, mix56, dca1, ...),
# so declare a string id_format on each type. The platform otherwise defaults to
# integer child ids and rejects string ids at register_child.
for _ctype_def in CHILD_ENTITY_TYPES.values():
    _ctype_def["id_format"] = {"type": "string", "max_length": 64}

# Which per-channel operations each type supports (drives command generation).
MUTE_TYPES = list(CHILD_ENTITY_TYPES.keys())
FADER_TYPES = [t for t in CHILD_ENTITY_TYPES if t != "mute_group"]
PAN_TYPES = ["input", "stereo_input", "fx_return"]

# Send routing enums.
SEND_SOURCE_TYPES = ["input", "stereo_input", "group", "fx_return"]
SEND_DEST_TYPES = ["mix", "group", "matrix", "fx_send"]


# ── Command catalog ──────────────────────────────────────────────────────────

_ACTION_PARAM = {
    "type": "enum", "values": ["on", "off", "toggle"], "default": "on",
    "required": True, "label": "Action",
    "help": "on, off, or toggle (toggle uses the driver's last-known state).",
}
_ONOFF_PARAM = {
    "type": "enum", "values": ["on", "off"], "default": "on",
    "required": True, "label": "Action",
}
_LEVEL_PARAM = {
    "type": "number", "min": 0.0, "max": 1.0, "default": 0.75, "required": True,
    "label": "Level", "help": "Fader position 0.0 (-inf) to 1.0 (+10 dB).",
}
_DB_PARAM = {
    "type": "number", "min": FADER_DB_MIN, "max": FADER_DB_MAX, "default": 0.0,
    "required": True, "label": "Level (dB)",
    "help": "-inf..+10 dB. Values below -45 dB approach silence.",
}
_PAN_PARAM = {
    "type": "number", "min": -1.0, "max": 1.0, "default": 0.0, "required": True,
    "label": "Pan", "help": "-1.0 full left, 0.0 centre, +1.0 full right.",
}


def _child_param(ctype: str) -> dict[str, Any]:
    return {"type": "child_id", "child_type": ctype, "required": True,
            "label": CHILD_ENTITY_TYPES[ctype]["label"]}


def _source_params() -> dict[str, Any]:
    return {
        "source_type": {"type": "enum", "values": SEND_SOURCE_TYPES,
                        "default": "input", "required": True,
                        "label": "Source Type"},
        "source": {"type": "integer", "min": 1, "required": True,
                   "label": "Source Number",
                   "help": "1-based channel number within the source type."},
    }


def _dest_params() -> dict[str, Any]:
    return {
        "dest_type": {"type": "enum", "values": SEND_DEST_TYPES,
                      "default": "mix", "required": True, "label": "Dest Type"},
        "dest": {"type": "integer", "min": 1, "required": True,
                 "label": "Dest Number",
                 "help": "1-based destination number within the dest type."},
    }


def _build_commands() -> dict[str, dict[str, Any]]:
    cmds: dict[str, dict[str, Any]] = {}

    # Per-channel mutes (child pickers).
    for ctype in MUTE_TYPES:
        label = CHILD_ENTITY_TYPES[ctype]["label"]
        cmds[f"mute_{ctype}"] = {
            "label": f"Mute {label}",
            "help": f"Mute, unmute, or toggle a {label.lower()}.",
            "params": {"channel": _child_param(ctype),
                       "action": dict(_ACTION_PARAM)},
        }
    cmds["mute_lr"] = {
        "label": "Mute LR", "help": "Mute, unmute, or toggle the main LR.",
        "params": {"action": dict(_ACTION_PARAM)},
    }

    # Per-channel faders (position + dB).
    for ctype in FADER_TYPES:
        label = CHILD_ENTITY_TYPES[ctype]["label"]
        cmds[f"set_{ctype}_fader"] = {
            "label": f"Set {label} Fader",
            "help": f"Set a {label.lower()} fader by position (0-1).",
            "params": {"channel": _child_param(ctype), "level": dict(_LEVEL_PARAM)},
        }
        cmds[f"set_{ctype}_fader_db"] = {
            "label": f"Set {label} Fader (dB)",
            "help": f"Set a {label.lower()} fader by dB value.",
            "params": {"channel": _child_param(ctype), "db": dict(_DB_PARAM)},
        }
    cmds["set_lr_fader"] = {
        "label": "Set LR Fader", "help": "Set the main LR fader by position (0-1).",
        "params": {"level": dict(_LEVEL_PARAM)},
    }
    cmds["set_lr_fader_db"] = {
        "label": "Set LR Fader (dB)", "help": "Set the main LR fader by dB value.",
        "params": {"db": dict(_DB_PARAM)},
    }

    # Per-channel pan.
    for ctype in PAN_TYPES:
        label = CHILD_ENTITY_TYPES[ctype]["label"]
        cmds[f"set_{ctype}_pan"] = {
            "label": f"Set {label} Pan",
            "help": f"Pan a {label.lower()} within the main LR mix.",
            "params": {"channel": _child_param(ctype), "pan": dict(_PAN_PARAM)},
        }

    # Sends (flat source/dest addressing).
    cmds["set_send_level"] = {
        "label": "Set Send Level",
        "help": "Set a send level from a source channel to a mix / group / "
                "matrix / FX send.",
        "params": {**_source_params(), **_dest_params(), "level": dict(_LEVEL_PARAM)},
    }
    cmds["set_send_pan"] = {
        "label": "Set Send Pan",
        "help": "Pan a source channel's send into a stereo destination.",
        "params": {**_source_params(), **_dest_params(), "pan": dict(_PAN_PARAM)},
    }
    cmds["set_send_assign"] = {
        "label": "Set Send Assign",
        "help": "Assign or unassign a source channel to a destination.",
        "params": {**_source_params(), **_dest_params(), "action": dict(_ONOFF_PARAM)},
    }
    cmds["set_send_prepost"] = {
        "label": "Set Send Pre/Post",
        "help": "Set a send tap point pre- or post-fader.",
        "params": {**_source_params(), **_dest_params(),
                   "point": {"type": "enum", "values": ["pre", "post"],
                             "default": "post", "required": True,
                             "label": "Tap Point"}},
    }

    # LR assign, DCA / mute-group assign.
    cmds["set_lr_assign"] = {
        "label": "Assign to LR",
        "help": "Assign or unassign a source channel to the main LR mix.",
        "params": {**_source_params(), "action": dict(_ONOFF_PARAM)},
    }
    cmds["set_dca_assign"] = {
        "label": "Assign to DCA",
        "help": "Assign or remove a source channel from a DCA group.",
        "params": {**_source_params(),
                   "dca": {"type": "integer", "min": 1, "max": DCAS,
                           "required": True, "label": "DCA"},
                   "action": dict(_ONOFF_PARAM)},
    }
    cmds["set_mute_group_assign"] = {
        "label": "Assign to Mute Group",
        "help": "Assign or remove a source channel from a mute group.",
        "params": {**_source_params(),
                   "mute_group": {"type": "integer", "min": 1, "max": MUTE_GROUPS,
                                  "required": True, "label": "Mute Group"},
                   "action": dict(_ONOFF_PARAM)},
    }

    # PAFL.
    cmds["set_pafl"] = {
        "label": "Set PAFL",
        "help": "Turn PAFL (solo monitoring) on or off for a source channel.",
        "params": {**_source_params(), "action": dict(_ONOFF_PARAM)},
    }

    # Local preamp.
    cmds["set_preamp_gain"] = {
        "label": "Set Preamp Gain (dB)",
        "help": "Set the local (rear-panel) preamp gain for an input.",
        "params": {"channel": _child_param("input"),
                   "gain_db": {"type": "number", "min": GAIN_DB_MIN,
                               "max": GAIN_DB_MAX, "default": 30.0,
                               "required": True, "label": "Gain (dB)"}},
    }
    cmds["set_preamp_48v"] = {
        "label": "Set Preamp 48V",
        "help": "Engage or disengage 48V phantom on a local input preamp.",
        "params": {"channel": _child_param("input"), "action": dict(_ONOFF_PARAM)},
    }

    # Scene recall.
    cmds["recall_scene"] = {
        "label": "Recall Scene",
        "help": f"Recall a scene {MIN_SCENE}-{MAX_SCENE} (Bank 1).",
        "params": {"scene": {"type": "integer", "min": MIN_SCENE,
                             "max": MAX_SCENE, "required": True, "label": "Scene"}},
    }

    # MMC transport.
    for action in MMC_CODES:
        cmds[f"mmc_{action}"] = {
            "label": f"MMC {action.title()}",
            "help": f"Send MIDI Machine Control {action.upper()}.",
            "params": {},
        }

    # Channel naming.
    cmds["set_channel_name"] = {
        "label": "Set Channel Name",
        "help": "Write a channel name on the console.",
        "params": {**_source_params(),
                   "name": {"type": "string", "required": True, "label": "Name",
                            "help": "Shown on the console strip."}},
    }

    # Quick-action bulk helpers + system.
    cmds["mute_all_inputs"] = {
        "label": "Mute All Inputs", "params": {},
        "help": "Mute every input channel.",
    }
    cmds["unmute_all_inputs"] = {
        "label": "Unmute All Inputs", "params": {},
        "help": "Unmute every input channel.",
    }
    cmds["identify"] = {
        "label": "Re-identify / Sync", "params": {},
        "help": "Re-run the All-Call model identify and full-state sync.",
    }
    cmds["remote_shutdown"] = {
        "label": "Remote Shutdown", "params": {},
        "help": "Shut the mixer down. A hard power cycle is then required to "
                "switch it back on.",
    }

    # Escape hatch for anything the typed commands don't cover (channel DSP —
    # PEQ / GEQ / gate / compressor / delay — is intentionally not exposed as
    # its own commands; recall it via scenes, or poke a specific value here).
    cmds["set_raw_parameter"] = {
        "label": "Advanced: Set Raw Parameter",
        "help": "Send any Qu NRPN parameter directly, using the raw 0-127 byte "
                "values from the Qu MIDI Protocol tables. CH selects the "
                "channel (e.g. 32 / 0x20 = Input 1), Parameter ID selects the "
                "function (e.g. 23 / 0x17 = fader), Value is the setting, and "
                "Index is the second value (7 for most single-destination "
                "parameters).",
        "params": {
            "ch": {"type": "integer", "min": 0, "max": 127, "required": True,
                   "label": "Channel (CH)",
                   "help": "Channel-select byte from the protocol table."},
            "param_id": {"type": "integer", "min": 0, "max": 127,
                         "required": True, "label": "Parameter ID",
                         "help": "NRPN parameter id."},
            "value": {"type": "integer", "min": 0, "max": 127, "required": True,
                      "label": "Value (VA)"},
            "index": {"type": "integer", "min": 0, "max": 127, "default": 7,
                      "required": True, "label": "Index (VX)",
                      "help": "Second value; 7 for most parameters."},
        },
    }

    return cmds


# ── The driver ───────────────────────────────────────────────────────────────

class AllenHeathQuDriver(BaseDriver):
    """Allen & Heath Qu MIDI-over-TCP driver — auto-identifies the model,
    registers its channels as child entities, and streams live state."""

    DRIVER_INFO = {
        "id": "allenheath_qu",
        "name": "Allen & Heath Qu Digital Mixer",
        "manufacturer": "Allen & Heath",
        "category": "audio",
        "version": "1.1.2",
        # The connection lifecycle hooks this driver overrides landed in 0.24.0.
        "min_platform_version": "0.24.0",
        "author": "OpenAVC",
        "description": (
            "Controls the Allen & Heath Qu family (Qu-16, Qu-24, Qu-32, Qu-Pac, "
            "Qu-SB) via MIDI over TCP/IP on port 51325. Auto-identifies the "
            "model on connect and registers every input, stereo input, mix, "
            "group, matrix, DCA, mute group and FX send/return as a child "
            "entity with live mute / fader / pan state and its real channel "
            "name. Mute / fader / pan / send / assign / pre-post control, DCA "
            "and mute-group masters, PAFL, local preamp gain and 48V, scene "
            "recall (1-100), MMC transport, channel naming and remote shutdown, "
            "with bidirectional state from console-side moves."
        ),
        "source_url": (
            "https://www.allen-heath.com/content/uploads/2023/06/"
            "Qu_MIDI_Protocol_V1.9.pdf"
        ),
        "tags": ["mixer", "console", "midi", "nrpn", "allen-heath", "qu"],
        "verified": True,
        "simulated": True,
        "protocols": ["midi-over-tcp"],
        "ports": [51325],
        "transport": "tcp",
        "discovery": {
            # Same AHNet situation as the SQ / Avantis / dLive drivers — the
            # UDP announce wire format isn't public. Hint-only via the
            # Audiotonix OUI + control port + manufacturer alias.
            "oui": ["00:04:c4"],
            "port_open": [51325],
            "manufacturer_alias": [
                "allen & heath", "allen and heath", "a&h",
                "allen-heath", "audiotonix",
            ],
        },
        "compatible_models": [
            {
                "manufacturer": "Allen & Heath",
                "models": ["Qu-16"],
                "confidence": "full",
                "notes": (
                    "Verified end-to-end against a real Qu-16 (firmware V1.97): "
                    "All-Call identify + full-state sync, child-entity "
                    "registration with live channel names, and mute / fader / "
                    "pan write round-trips confirmed by re-reading board state."
                ),
            },
            {
                "manufacturer": "Allen & Heath",
                "models": ["Qu-24", "Qu-32", "Qu-Pac", "Qu-SB"],
                "confidence": "untested",
                "notes": (
                    "Same MIDI protocol and command surface as the Qu-16; the "
                    "console reports its model over the All-Call SysEx, so the "
                    "driver registers the right channel set automatically. Not "
                    "yet tested on this hardware. Qu allows only one TCP MIDI "
                    "connection at a time. The newer Qu-5/6/7 consoles use a "
                    "different protocol and are not covered."
                ),
            },
        ],
        "child_entity_types": CHILD_ENTITY_TYPES,
        "default_config": {
            "host": "",
            "port": DEFAULT_PORT,
            "model": "auto",
            "midi_channel": 0,
            "poll_interval": 0,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address",
                     "description": "Qu network IP (Setup / Network on the "
                                    "console)."},
            "port": {"type": "integer", "default": DEFAULT_PORT, "label": "TCP Port",
                     "min": 1, "max": 65535,
                     "description": "MIDI over TCP/IP port. Always 51325 on Qu."},
            "model": {
                "type": "enum",
                "values": ["auto", "Qu-16", "Qu-24", "Qu-32", "Qu-Pac", "Qu-SB"],
                "default": "auto", "label": "Model",
                "description": "Leave on 'auto' to detect the model from the "
                               "console. Set a model only as a fallback if "
                               "auto-identify does not complete.",
            },
            "midi_channel": {
                "type": "integer", "default": 0, "min": 0, "max": 16,
                "label": "MIDI Channel (override)",
                "description": "0 = learn the MIDI channel from the console "
                               "(recommended). Otherwise force 1-16 to match "
                               "Setup / MIDI on the mixer.",
            },
            "poll_interval": {
                "type": "integer", "default": 0, "min": 0,
                "label": "Poll Interval (s)",
                "description": "The Qu pushes state; polling is unused. Leave "
                               "at 0 unless debugging.",
            },
        },
        "quick_actions": [
            "recall_scene", "mute_all_inputs", "unmute_all_inputs", "identify",
        ],
        "actions": [
            {"id": "recall_scene", "kind": "command", "icon": "layers"},
            {"id": "mute_all_inputs", "kind": "command", "icon": "volume-x"},
            {"id": "unmute_all_inputs", "kind": "command", "icon": "volume-2"},
            {"id": "identify", "kind": "command", "icon": "radar"},
            {"id": "remote_shutdown", "kind": "command", "icon": "power",
             "confirm": "Shut down the mixer? A hard power cycle is required to "
                        "switch it back on."},
        ],
        "help": {
            "overview": (
                "Controls an Allen & Heath Qu mixer over MIDI-over-TCP. On "
                "connect the driver asks the console to identify itself, learns "
                "the model and MIDI channel, and lists every channel as a child "
                "entity with live mute / fader / pan state and its real name. "
                "Bind child state (e.g. device.<id>.input.in05.mute) to panel "
                "elements, and drive Set Fader / Mute / Recall Scene from macros "
                "and buttons. Console-side moves push straight back into state."
            ),
            "setup": (
                "1. Connect the Qu to the same network as the OpenAVC server.\n"
                "2. On the Qu, go to Setup / Network and note the IP address.\n"
                "3. Enter the IP address above. Port 51325 is correct and the "
                "model/MIDI channel are detected automatically.\n"
                "4. Save. Within a second or two the device connects, the model "
                "populates, and each channel appears under the device with its "
                "live state and name.\n"
                "\n"
                "Notes:\n"
                "  * The Qu allows only ONE TCP MIDI connection at a time — "
                "close Qu-Pad / other MIDI clients before connecting.\n"
                "  * If the model doesn't populate, set it manually in the "
                "config as a fallback and run the Re-identify action.\n"
                "  * The Qu drops idle MIDI connections; the driver keeps the "
                "link alive automatically."
            ),
        },
        "state_variables": {
            "model": {"type": "string", "label": "Model"},
            "firmware": {"type": "string", "label": "Firmware"},
            "midi_channel": {"type": "integer", "label": "MIDI Channel"},
            "identified": {"type": "boolean", "label": "Identified"},
            "current_scene": {"type": "integer", "label": "Current Scene",
                              "min": 0, "max": MAX_SCENE},
            "channel_count": {"type": "integer", "label": "Channels"},
            "lr_mute": {"type": "boolean", "label": "LR Mute",
                        "control": True},
            "lr_fader": {"type": "number", "label": "LR Fader",
                         "min": 0.0, "max": 1.0, "control": True},
            "lr_fader_db": {"type": "number", "label": "LR Fader (dB)",
                            "min": FADER_DB_MIN, "max": FADER_DB_MAX,
                            "unit": "dB", "control": True},
            "lr_pan": {"type": "number", "label": "LR Pan", "min": -1.0,
                       "max": 1.0, "control": True},
        },
        "commands": {},   # populated per-instance in __init__
    }

    def __init__(self, device_id: str, config: dict[str, Any],
                 state: Any, events: Any) -> None:
        self.DRIVER_INFO = {**type(self).DRIVER_INFO, "commands": _build_commands()}
        super().__init__(device_id, config, state, events)

        self._rx_buf = bytearray()
        self._running_status = 0
        # NRPN aggregator per MIDI channel (0..15).
        self._nrpn: dict[int, dict[str, int]] = {
            ch: {"ch": 0, "id": 0, "va": 0} for ch in range(16)
        }

        # Learned identity.
        self._model = ""
        self._midi_n = 0                     # 0-based MIDI channel in use
        self._identify_future: asyncio.Future[str] | None = None

        # Topology routing:
        #   _ch_by_child: (ctype, sid) -> channel-select byte
        #   _route_by_ch: channel-select byte -> (ctype, sid)   (LR -> ("lr","lr"))
        self._ch_by_child: dict[tuple[str, str], int] = {}
        self._route_by_ch: dict[int, tuple[str, str]] = {}
        self._registered: set[tuple[str, str]] = set()

        # Keep-alive / liveness.
        self._sense_task: asyncio.Task[None] | None = None
        self._last_rx = 0.0

    # ── Small helpers ───────────────────────────────────────────────────────

    @property
    def _configured_channel(self) -> int | None:
        """0-based MIDI channel from an explicit config override, else None."""
        try:
            n = int(self.config.get("midi_channel", 0))
        except (TypeError, ValueError):
            return None
        return (n - 1) if 1 <= n <= 16 else None

    def _now(self) -> float:
        return asyncio.get_event_loop().time()

    async def _send(self, data: bytes) -> None:
        if self.transport:
            await self.transport.send(data)

    def _status(self, high: int) -> int:
        """A channel-voice status byte for the mixer's MIDI channel."""
        return high | (self._midi_n & 0x0F)

    def _sysex_header(self, channel_byte: int) -> bytes:
        return bytes([0xF0]) + SYSEX_MANUFACTURER + bytes([SYSEX_MODEL]) + \
            SYSEX_VER + bytes([channel_byte & 0x7F])

    # ── NRPN builders ───────────────────────────────────────────────────────

    def _nrpn_set(self, ch: int, param_id: int, va: int, vx: int) -> bytes:
        b = self._status(0xB0)
        return bytes([
            b, CC_NRPN_MSB, ch & 0x7F,
            b, CC_NRPN_LSB, param_id & 0x7F,
            b, CC_DATA_MSB, va & 0x7F,
            b, CC_DATA_LSB, vx & 0x7F,
        ])

    def _note_pair(self, note: int, velocity: int) -> bytes:
        n = self._status(0x90)
        return bytes([n, note & 0x7F, velocity & 0x7F, n, note & 0x7F, 0x00])

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def _pre_connect(self) -> None:
        if not str(self.config.get("host", "")).strip():
            raise ValueError("host is required")
        # Seed the MIDI channel from a config override (auto-learned otherwise).
        override = self._configured_channel
        if override is not None:
            self._midi_n = override

    def _transport_kwargs(
        self, transport_type: str, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        kwargs["host"] = str(kwargs["host"]).strip()
        kwargs["delimiter"] = None      # raw MIDI byte stream
        return kwargs

    async def _initial_sync(self) -> None:
        # Start the active-sense keep-alive + liveness watchdog immediately —
        # the Qu drops a silent client after 12 s.
        self._last_rx = self._now()
        await self._send(bytes([ACTIVE_SENSE]))
        self._start_sense_loop()

        # Identify + full-state sync.
        await self._identify_and_sync()

    async def _close_session(self) -> None:
        self._stop_sense_loop()

    # ── Active-sense keep-alive + liveness watchdog ─────────────────────────

    def _start_sense_loop(self) -> None:
        if self._sense_task and not self._sense_task.done():
            return
        self._sense_task = asyncio.ensure_future(self._sense_loop())

    def _stop_sense_loop(self) -> None:
        if self._sense_task and not self._sense_task.done():
            self._sense_task.cancel()
        self._sense_task = None

    async def _sense_loop(self) -> None:
        """Send Active Sense on a timer (keep-alive) and tear the socket down
        if the console stops sending (liveness). The Qu emits FE ~every 300 ms,
        so a multi-second inbound gap means it has vanished."""
        try:
            while self._connected and self.transport:
                await asyncio.sleep(ACTIVE_SENSE_TX_INTERVAL)
                if not (self._connected and self.transport):
                    return
                if self._now() - self._last_rx > RX_SILENCE_TIMEOUT:
                    log.warning("[%s] no MIDI for %.0fs — dropping connection",
                                self.device_id, RX_SILENCE_TIMEOUT)
                    self._last_fault = ConnectionFaultError(
                        "The Qu stopped sending — connection lost.",
                        code="no_response")
                    if self.transport:
                        try:
                            await self.transport.close()
                        except Exception:  # noqa: BLE001
                            pass
                    return
                try:
                    await self._send(bytes([ACTIVE_SENSE]))
                except (ConnectionError, OSError):
                    return
        except asyncio.CancelledError:
            pass

    # ── Identify + state sync ───────────────────────────────────────────────

    async def _identify_and_sync(self) -> None:
        """Send the All-Call system-state request, learn the model + MIDI
        channel from the reply, register the child topology, and let the
        console's state push populate everything. Falls back to the config
        model on timeout."""
        loop = asyncio.get_event_loop()
        self._identify_future = loop.create_future()

        # All-Call: header(7F) + 0x10 + iPadFlag(1) + F7. iPadFlag=1 asks for a
        # full-state push; we already active-sense so its 5 s follow-up rule is
        # satisfied.
        req = self._sysex_header(SYSEX_ALL_CALL_CH) + bytes([SX_SYSTEM_STATE_REQ,
                                                             0x01, 0xF7])
        try:
            await self._send(req)
        except (ConnectionError, OSError):
            return

        model = ""
        try:
            model = await asyncio.wait_for(self._identify_future,
                                           timeout=SYSTEM_STATE_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("[%s] no All-Call reply; using configured model",
                        self.device_id)
        finally:
            self._identify_future = None

        # On a reply, the topology was already registered synchronously in
        # _handle_system_state_reply (so the state push that follows in the
        # same packet routes correctly). Only the timeout path registers here.
        if not model:
            cfg = str(self.config.get("model", "auto"))
            self._apply_identity(cfg if cfg in MODEL_TOPOLOGY else "Qu-16",
                                 identified=False)

        # Pull channel names to label the children (replies arrive async).
        await self._request_all_names()

    def _apply_identity(self, model: str, identified: bool) -> None:
        self._model = model
        self.set_state("model", model)
        self.set_state("midi_channel", self._midi_n + 1)
        self.set_state("identified", identified)
        self._register_topology(model)

    def _handle_system_state_reply(self, body: bytes, midi_ch: int) -> None:
        """body = [0x11, BoxID, VerMajor, VerMinor]. Learn model + MIDI channel,
        register the topology now (before the state push that follows), and
        release the identify wait."""
        if len(body) < 2:
            return
        box_id = body[1]
        model = BOX_ID_MODELS.get(box_id, "")
        if len(body) >= 4:
            self.set_state("firmware", f"V{body[2]}.{body[3]}")
        # Learn the MIDI channel from the reply header unless overridden.
        if self._configured_channel is None:
            self._midi_n = midi_ch & 0x0F
        if not model:
            return
        self._apply_identity(model, identified=True)
        if self._identify_future and not self._identify_future.done():
            self._identify_future.set_result(model)

    # ── Topology registration ───────────────────────────────────────────────

    def _register_topology(self, model: str) -> None:
        topo = MODEL_TOPOLOGY.get(model, MODEL_TOPOLOGY["Qu-16"])
        self._ch_by_child = {}
        self._route_by_ch = {}
        new_children: set[tuple[str, str]] = set()

        def add(ctype: str, sid: str, label: str, ch: int) -> None:
            self._ch_by_child[(ctype, sid)] = ch
            self._route_by_ch[ch] = (ctype, sid)
            new_children.add((ctype, sid))
            # register_child is idempotent: a re-identify won't clobber a name
            # already learned, and the initial "Ch N" placeholder is replaced
            # by the real name once the name reply lands.
            self.register_child(ctype, sid, initial_state={"name": label})

        for n in range(1, topo["inputs"] + 1):
            add("input", f"in{n:02d}", f"Ch {n}", input_ch(n))
        for n in range(1, STEREO_INPUTS + 1):
            add("stereo_input", f"st{n}", f"ST{n}", stereo_ch(n))
        for _idx, sid, label, ch, _vx in MIXES:
            add("mix", sid, label, ch)
        for i in range(1, topo["group_pairs"] + 1):
            add("group", f"grp{i}", GROUP_PAIR_LABELS[i - 1], group_ch(i))
        for i in range(1, topo["matrix_pairs"] + 1):
            add("matrix", f"mtx{i}", MATRIX_PAIR_LABELS[i - 1], matrix_ch(i))
        for n in range(1, DCAS + 1):
            add("dca", f"dca{n}", f"DCA {n}", dca_ch(n))
        for n in range(1, MUTE_GROUPS + 1):
            add("mute_group", f"mg{n}", f"Mute Group {n}", mute_group_ch(n))
        for n in range(1, topo["fx_sends"] + 1):
            add("fx_send", f"fxs{n}", f"FX Send {n}", fx_send_ch(n))
        for n in range(1, FX_RETURNS + 1):
            add("fx_return", f"fxr{n}", f"FX Ret {n}", fx_return_ch(n))

        # LR isn't a child (it's the main master) but shares the route map so
        # inbound LR fader/mute/pan lands on device state.
        self._route_by_ch[LR_CH] = ("lr", "lr")

        # Reconcile: drop children left over from a previous (different) model.
        for ctype, sid in self._registered - new_children:
            self.deregister_child(ctype, sid)
        self._registered = new_children
        self.set_state("channel_count", len(self._ch_by_child))
        log.info("[%s] %s: registered %d channels", self.device_id, model,
                 len(self._ch_by_child))

    async def _request_all_names(self) -> None:
        """Ask the console for every registered channel's name (replies async)."""
        for i, ((_ctype, _sid), ch) in enumerate(self._ch_by_child.items()):
            try:
                await self._send(self._sysex_header(self._midi_n) +
                                 bytes([SX_GET_NAME, ch & 0x7F, 0xF7]))
            except (ConnectionError, OSError):
                return
            if i % 16 == 15:
                await asyncio.sleep(0.01)

    async def refresh_children(self) -> dict[str, Any]:
        """IDE 'Refresh from Device' + the Re-identify action: re-run the
        All-Call identify and full-state sync."""
        if not self.transport or not self._connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self._identify_and_sync()
        return {"model": self._model, "channels": len(self._ch_by_child)}

    # ── send_command dispatch ───────────────────────────────────────────────

    async def send_command(self, command: str,
                           params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        handler = _COMMAND_HANDLERS.get(command)
        if handler is None:
            raise ValueError(f"Unknown command: {command}")
        return await handler(self, params)

    # ── Channel resolution ──────────────────────────────────────────────────

    def _child_ch(self, ctype: str, sid: str) -> int:
        ch = self._ch_by_child.get((ctype, sid))
        if ch is None:
            raise ValueError(f"Unknown {ctype}: {sid!r}")
        return ch

    def _source_ch(self, source_type: str, n: int) -> int:
        n = int(n)
        if source_type == "input":
            return input_ch(n)
        if source_type == "stereo_input":
            return stereo_ch(n)
        if source_type == "group":
            return group_ch(n)
        if source_type == "fx_return":
            return fx_return_ch(n)
        raise ValueError(f"Invalid send source type: {source_type}")

    @staticmethod
    def _dest_vx(dest_type: str, n: int) -> int:
        n = int(n)
        if dest_type == "mix":
            return next(vx for idx, _sid, _l, _ch, vx in MIXES if idx == n)
        if dest_type == "group":
            return 0x08 + (n - 1)
        if dest_type == "matrix":
            return 0x0C + (n - 1)
        if dest_type == "fx_send":
            return 0x10 + (n - 1)
        raise ValueError(f"Invalid send dest type: {dest_type}")

    # ── Command implementations ─────────────────────────────────────────────
    #
    # Optimistic state: the Qu does NOT echo changes it receives over MIDI
    # (verified on real hardware — only console-side surface moves push back),
    # so after sending a mute / fader / pan we mirror the commanded value into
    # our own state. Otherwise a panel that just set a value would snap back to
    # the stale one. Console-side moves still correct us via the push parser.

    async def _do_mute(self, ctype: str, params: dict[str, Any]) -> None:
        if ctype == "lr":
            ch, sid = LR_CH, "lr"
        else:
            sid = params["channel"]
            ch = self._child_ch(ctype, sid)
        action = str(params.get("action", "on")).lower()
        if action == "toggle":
            cur = self._get_mute_state(ctype, sid)
            action = "off" if cur else "on"
        on = action == "on"
        velocity = 0x7F if on else 0x3F
        await self._send(self._note_pair(ch, velocity))
        self._apply_mute((ctype, sid), on)

    def _get_mute_state(self, ctype: str, sid: str) -> bool:
        if ctype == "lr":
            return bool(self.get_state("lr_mute"))
        return bool(self.get_child_state(ctype, sid).get("mute"))

    async def _do_fader(self, ctype: str, params: dict[str, Any],
                        as_db: bool) -> None:
        if ctype == "lr":
            ch, sid = LR_CH, "lr"
        else:
            sid = params["channel"]
            ch = self._child_ch(ctype, sid)
        if as_db:
            va = fader_db_to_byte(float(params["db"]))
        else:
            va = fader_pos_to_byte(float(params["level"]))
        await self._send(self._nrpn_set(ch, ID_FADER, va, VX_MAIN))
        self._apply_fader((ctype, sid), va)

    async def _do_pan(self, ctype: str, params: dict[str, Any]) -> None:
        sid = params["channel"]
        ch = self._child_ch(ctype, sid)
        va = pan_to_byte(float(params["pan"]))
        await self._send(self._nrpn_set(ch, ID_PAN, va, VX_MAIN))
        self._apply_pan((ctype, sid), va)

    async def _do_send_level(self, params: dict[str, Any]) -> None:
        ch = self._source_ch(params["source_type"], params["source"])
        vx = self._dest_vx(params["dest_type"], params["dest"])
        va = fader_pos_to_byte(float(params["level"]))
        await self._send(self._nrpn_set(ch, ID_SEND_LEVEL, va, vx))

    async def _do_send_pan(self, params: dict[str, Any]) -> None:
        ch = self._source_ch(params["source_type"], params["source"])
        vx = self._dest_vx(params["dest_type"], params["dest"])
        va = pan_to_byte(float(params["pan"]))
        await self._send(self._nrpn_set(ch, ID_PAN, va, vx))

    async def _do_send_assign(self, params: dict[str, Any]) -> None:
        ch = self._source_ch(params["source_type"], params["source"])
        vx = self._dest_vx(params["dest_type"], params["dest"])
        va = 0x01 if str(params.get("action", "on")).lower() == "on" else 0x00
        await self._send(self._nrpn_set(ch, ID_MIX_ASSIGN, va, vx))

    async def _do_send_prepost(self, params: dict[str, Any]) -> None:
        ch = self._source_ch(params["source_type"], params["source"])
        vx = self._dest_vx(params["dest_type"], params["dest"])
        va = 0x01 if str(params.get("point", "post")).lower() == "pre" else 0x00
        await self._send(self._nrpn_set(ch, ID_MIX_PREPOST, va, vx))

    async def _do_lr_assign(self, params: dict[str, Any]) -> None:
        ch = self._source_ch(params["source_type"], params["source"])
        va = 0x01 if str(params.get("action", "on")).lower() == "on" else 0x00
        await self._send(self._nrpn_set(ch, ID_LR_ASSIGN, va, VX_MAIN))

    async def _do_dca_assign(self, params: dict[str, Any]) -> None:
        ch = self._source_ch(params["source_type"], params["source"])
        dca = int(params["dca"])
        on = str(params.get("action", "on")).lower() == "on"
        va = (0x40 if on else 0x00) + (dca - 1)
        await self._send(self._nrpn_set(ch, ID_DCA_ASSIGN, va, VX_MAIN))

    async def _do_mute_group_assign(self, params: dict[str, Any]) -> None:
        ch = self._source_ch(params["source_type"], params["source"])
        mg = int(params["mute_group"])
        on = str(params.get("action", "on")).lower() == "on"
        va = (0x40 if on else 0x00) + (mg - 1)
        await self._send(self._nrpn_set(ch, ID_MUTEGRP_ASSIGN, va, VX_MAIN))

    async def _do_pafl(self, params: dict[str, Any]) -> None:
        ch = self._source_ch(params["source_type"], params["source"])
        va = 0x01 if str(params.get("action", "on")).lower() == "on" else 0x00
        await self._send(self._nrpn_set(ch, ID_PAFL, va, VX_MAIN))

    async def _do_preamp_gain(self, params: dict[str, Any]) -> None:
        ch = self._child_ch("input", params["channel"])
        va = gain_db_to_byte(float(params["gain_db"]))
        await self._send(self._nrpn_set(ch, ID_PREAMP_GAIN, va, VX_MAIN))

    async def _do_preamp_48v(self, params: dict[str, Any]) -> None:
        ch = self._child_ch("input", params["channel"])
        va = 0x01 if str(params.get("action", "on")).lower() == "on" else 0x00
        await self._send(self._nrpn_set(ch, ID_PREAMP_48V, va, VX_MAIN))

    async def _do_recall_scene(self, params: dict[str, Any]) -> None:
        scene = int(params["scene"])
        if not MIN_SCENE <= scene <= MAX_SCENE:
            raise ValueError(f"Scene {scene} outside {MIN_SCENE}..{MAX_SCENE}")
        b = self._status(0xB0)
        c = self._status(0xC0)
        # Select Bank 1, then Program Change to the (0-based) scene.
        await self._send(bytes([b, CC_BANK_MSB, 0x00, b, CC_BANK_LSB, 0x00,
                                c, (scene - 1) & 0x7F]))
        self.set_state("current_scene", scene)

    async def _do_mmc(self, code: int) -> None:
        await self._send(bytes([0xF0, 0x7F, 0x7F, 0x06, code & 0x7F, 0xF7]))

    async def _do_set_channel_name(self, params: dict[str, Any]) -> None:
        ch = self._source_ch(params["source_type"], params["source"])
        name = str(params.get("name", ""))[:8].encode("ascii", errors="replace")
        await self._send(self._sysex_header(self._midi_n) +
                         bytes([SX_SET_NAME, ch & 0x7F]) + name + bytes([0xF7]))

    async def _do_mute_all_inputs(self, on: bool) -> None:
        velocity = 0x7F if on else 0x3F
        for (ctype, _sid), ch in self._ch_by_child.items():
            if ctype == "input":
                await self._send(self._note_pair(ch, velocity))

    async def _do_remote_shutdown(self) -> None:
        b = self._status(0xB0)
        await self._send(bytes([b, CC_NRPN_MSB, 0x00, b, CC_NRPN_LSB,
                                ID_REMOTE_SHUTDOWN, b, CC_DATA_MSB, 0x00,
                                b, CC_DATA_LSB, 0x00]))

    async def _do_raw_parameter(self, params: dict[str, Any]) -> None:
        await self._send(self._nrpn_set(
            int(params["ch"]), int(params["param_id"]),
            int(params["value"]), int(params.get("index", VX_MAIN))))

    # ── Incoming MIDI parser ────────────────────────────────────────────────

    def on_data_received(self, data: bytes) -> None:
        if not data:
            return
        self._last_rx = self._now()
        self._rx_buf.extend(data)
        self._parse()

    def _parse(self) -> None:
        buf = self._rx_buf
        running_status = self._running_status
        i = 0
        while i < len(buf):
            b = buf[i]
            # System real-time (F8-FF) — single byte, no effect on running
            # status. FE (Active Sense) is our liveness heartbeat (already
            # timestamped in on_data_received).
            if 0xF8 <= b <= 0xFF:
                i += 1
                continue
            if b & 0x80:
                if 0xF0 <= b <= 0xF7:
                    if b == 0xF0:
                        end = buf.find(0xF7, i + 1)
                        if end == -1:
                            break                      # incomplete SysEx
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
                    break
                d1, d2 = buf[i], buf[i + 1]
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

    def _handle_note_on(self, ch: int, note: int, velocity: int) -> None:
        if velocity == 0x00:
            return                                     # trailing Note-Off
        route = self._route_by_ch.get(note)
        if route is None:
            return
        on = velocity >= 0x40
        self._apply_mute(route, on)

    def _handle_cc(self, ch: int, controller: int, value: int) -> None:
        # Bank Select bytes precede scene Program Changes; the Qu only uses
        # Bank 1, so there's nothing to track.
        if controller in (CC_BANK_MSB, CC_BANK_LSB):
            return
        agg = self._nrpn[ch]
        if controller == CC_NRPN_MSB:
            agg["ch"] = value
        elif controller == CC_NRPN_LSB:
            agg["id"] = value
        elif controller == CC_DATA_MSB:
            agg["va"] = value
        elif controller == CC_DATA_LSB:
            self._dispatch_nrpn(agg["ch"], agg["id"], agg["va"], value)

    def _dispatch_nrpn(self, ch: int, param_id: int, va: int, vx: int) -> None:
        route = self._route_by_ch.get(ch)
        if route is None:
            return
        if param_id == ID_FADER:
            self._apply_fader(route, va)
        elif param_id == ID_PAN and vx == VX_MAIN:
            self._apply_pan(route, va)
        # Other inbound NRPN (send levels, assigns, processing) aren't mirrored
        # as channel state and are safely ignored.

    def _handle_program_change(self, ch: int, program: int) -> None:
        # Scene recall echo (Bank 1 assumed; the Qu only uses Bank 1).
        scene = program + 1
        if MIN_SCENE <= scene <= MAX_SCENE:
            self.set_state("current_scene", scene)

    def _handle_sysex(self, msg: bytes) -> None:
        # F0 00 00 1A 50 11 01 00 0N <kind> ... F7
        if len(msg) < 11:
            return
        if bytes(msg[1:4]) != SYSEX_MANUFACTURER or msg[4] != SYSEX_MODEL:
            return
        midi_ch = msg[8] & 0x0F
        kind = msg[9]
        body = msg[9:-1]                               # kind .. before F7
        if kind == SX_SYSTEM_STATE_REPLY:
            self._handle_system_state_reply(body, midi_ch)
        elif kind == SX_NAME_REPLY:
            self._handle_name_reply(body)
        # SX_END_SYNC / SX_METER_REPLY: nothing to mirror.

    def _handle_name_reply(self, body: bytes) -> None:
        # body = [0x02, CH, <name bytes>]
        if len(body) < 2:
            return
        ch = body[1]
        route = self._route_by_ch.get(ch)
        if route is None or route[0] == "lr":
            return
        name = bytes(body[2:]).decode("ascii", errors="replace").strip("\x00 ")
        if not name:
            return
        ctype, sid = route
        try:
            self.set_child_state_batch(ctype, sid, {"name": name})
        except Exception:  # noqa: BLE001
            pass

    # ── State fan-out ───────────────────────────────────────────────────────

    def _apply_mute(self, route: tuple[str, str], on: bool) -> None:
        ctype, sid = route
        if ctype == "lr":
            self.set_state("lr_mute", on)
            return
        try:
            self.set_child_state_batch(ctype, sid, {"mute": on})
        except Exception:  # noqa: BLE001
            pass

    def _apply_fader(self, route: tuple[str, str], va: int) -> None:
        ctype, sid = route
        pos = byte_to_fader_pos(va)
        db = byte_to_fader_db(va)
        if ctype == "lr":
            self.set_state("lr_fader", pos)
            self.set_state("lr_fader_db", db)
            return
        try:
            self.set_child_state_batch(ctype, sid,
                                       {"fader": pos, "fader_db": db})
        except Exception:  # noqa: BLE001
            pass

    def _apply_pan(self, route: tuple[str, str], va: int) -> None:
        ctype, sid = route
        pan = byte_to_pan(va)
        if ctype == "lr":
            self.set_state("lr_pan", pan)
            return
        if "pan" not in CHILD_ENTITY_TYPES.get(ctype, {}).get(
                "state_variables", {}):
            return
        try:
            self.set_child_state_batch(ctype, sid, {"pan": pan})
        except Exception:  # noqa: BLE001
            pass


# Class export expected by the loader.
DRIVER_CLASS = AllenHeathQuDriver


# ── Command dispatch table ───────────────────────────────────────────────────
#
# Built once at import; send_command() looks each id up. Per-type mute / fader /
# pan handlers are generated so a new channel type needs no new method.

def _mute_handler(ctype: str):
    async def _h(self: "AllenHeathQuDriver", params: dict[str, Any]) -> None:
        await self._do_mute(ctype, params)
    return _h


def _fader_handler(ctype: str, as_db: bool):
    async def _h(self: "AllenHeathQuDriver", params: dict[str, Any]) -> None:
        await self._do_fader(ctype, params, as_db)
    return _h


def _pan_handler(ctype: str):
    async def _h(self: "AllenHeathQuDriver", params: dict[str, Any]) -> None:
        await self._do_pan(ctype, params)
    return _h


def _mmc_handler(code: int):
    async def _h(self: "AllenHeathQuDriver", _params: dict[str, Any]) -> None:
        await self._do_mmc(code)
    return _h


_COMMAND_HANDLERS: dict[str, Any] = {
    "mute_lr": lambda self, p: self._do_mute("lr", p),
    "set_lr_fader": lambda self, p: self._do_fader("lr", p, False),
    "set_lr_fader_db": lambda self, p: self._do_fader("lr", p, True),
    "set_send_level": lambda self, p: self._do_send_level(p),
    "set_send_pan": lambda self, p: self._do_send_pan(p),
    "set_send_assign": lambda self, p: self._do_send_assign(p),
    "set_send_prepost": lambda self, p: self._do_send_prepost(p),
    "set_lr_assign": lambda self, p: self._do_lr_assign(p),
    "set_dca_assign": lambda self, p: self._do_dca_assign(p),
    "set_mute_group_assign": lambda self, p: self._do_mute_group_assign(p),
    "set_pafl": lambda self, p: self._do_pafl(p),
    "set_preamp_gain": lambda self, p: self._do_preamp_gain(p),
    "set_preamp_48v": lambda self, p: self._do_preamp_48v(p),
    "recall_scene": lambda self, p: self._do_recall_scene(p),
    "set_channel_name": lambda self, p: self._do_set_channel_name(p),
    "mute_all_inputs": lambda self, p: self._do_mute_all_inputs(True),
    "unmute_all_inputs": lambda self, p: self._do_mute_all_inputs(False),
    "identify": lambda self, p: self._identify_and_sync(),
    "remote_shutdown": lambda self, p: self._do_remote_shutdown(),
    "set_raw_parameter": lambda self, p: self._do_raw_parameter(p),
}
for _ct in MUTE_TYPES:
    _COMMAND_HANDLERS[f"mute_{_ct}"] = _mute_handler(_ct)
for _ct in FADER_TYPES:
    _COMMAND_HANDLERS[f"set_{_ct}_fader"] = _fader_handler(_ct, False)
    _COMMAND_HANDLERS[f"set_{_ct}_fader_db"] = _fader_handler(_ct, True)
for _ct in PAN_TYPES:
    _COMMAND_HANDLERS[f"set_{_ct}_pan"] = _pan_handler(_ct)
for _action, _code in MMC_CODES.items():
    _COMMAND_HANDLERS[f"mmc_{_action}"] = _mmc_handler(_code)
