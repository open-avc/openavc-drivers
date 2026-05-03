"""
OpenAVC Allen & Heath SQ Driver.

Controls Allen & Heath SQ-5, SQ-6 and SQ-7 digital mixing consoles
over MIDI-over-TCP/IP on port 51325. The protocol is the standard
MIDI 1.0 wire format carried over a raw TCP stream. Mixer parameters
(mutes, levels, panning, mix assignments) are addressed using NRPN
(Non-Registered Parameter Number) sequences; scene recall uses Bank
Select + Program Change; SoftKeys use Note On/Off.

Push vs poll:
    The SQ both sends and receives NRPN updates. When a console-side
    control moves (fader, mute key, etc.) the console emits the
    matching NRPN sequence on its configured MIDI channel. The driver
    listens for these and reflects them into state immediately. On
    connect the driver sweeps "get" requests across every parameter
    in its state surface to populate initial values; thereafter
    state stays current via push, with a slow polling backstop in
    case packets are missed during a transient disconnect.

Format choice:
    Python rather than YAML because (a) the wire format is binary
    (MIDI bytes, NRPN sequences with running status), (b) parameter
    tables span 624 source/destination cells that are best computed
    programmatically rather than enumerated, and (c) push handling
    requires fanning each incoming NRPN sequence out to the right
    state key based on a reverse lookup. None of this fits
    ConfigurableDriver cleanly today.

Models covered:
    SQ-5  — 8 SoftKeys, 0 Soft Rotaries
    SQ-6  — 16 SoftKeys, 4 Soft Rotaries
    SQ-7  — 16 SoftKeys, 8 Soft Rotaries
    All three share the same MIDI command set; per-model differences
    are physical I/O and surface controls only.

Scope notes:
    - Linear NRPN Fader Law only (the SQ default). Audio Taper has
      a different VC/VF mapping; if a customer needs it, expose a
      `fader_law` config field and a second mapping table.
    - MIDI fader strips, Soft Rotaries, MMC transport, and DAW-
      Control-channel CC/Note traffic are intentionally not exposed.
      Those are end-user surface controls on the console, not
      integrator-addressable mixer functions.

Source:
    https://www.allen-heath.com/content/uploads/2023/11/SQ-MIDI-Protocol-Issue5.pdf
"""

from __future__ import annotations

import asyncio
from typing import Any

from server.drivers.base import BaseDriver
from server.transport.tcp import TCPTransport
from server.utils.logger import get_logger

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
    return _addr(0x5F, 0x00)


def pan_aux_master(n: int) -> tuple[int, int]:
    return _addr(0x5F, 0x01 + (n - 1))


def pan_mtx_master(n: int) -> tuple[int, int]:
    return _addr(0x5F, 0x11 + (n - 1))


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
    """
    pan = max(PAN_LEFT, min(PAN_RIGHT, pan))
    value = round((pan + 1.0) / 2.0 * VALUE_MAX)
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


# ── State variable construction ──────────────────────────────────────────────

def _build_state_vars() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {
        "current_scene": {
            "type": "integer",
            "label": "Current Scene",
            "min": 0,
            "max": MAX_SCENE,
        },
        "lr_mute": {"type": "boolean", "label": "LR Master Mute"},
        "lr_fader": {
            "type": "number",
            "label": "LR Master Fader",
            "min": LEVEL_MIN,
            "max": LEVEL_MAX,
        },
    }

    for n in range(1, NUM_INPUTS + 1):
        out[f"in{n:02d}_mute"] = {"type": "boolean", "label": f"Input {n} Mute"}
        out[f"in{n:02d}_to_lr_level"] = {
            "type": "number",
            "label": f"Input {n} → LR Level",
            "min": LEVEL_MIN,
            "max": LEVEL_MAX,
        }
        out[f"in{n:02d}_to_lr_pan"] = {
            "type": "number",
            "label": f"Input {n} → LR Pan",
            "min": PAN_LEFT,
            "max": PAN_RIGHT,
        }

    for n in range(1, NUM_GROUPS + 1):
        out[f"grp{n:02d}_mute"] = {"type": "boolean", "label": f"Group {n} Mute"}
        out[f"grp{n:02d}_to_lr_level"] = {
            "type": "number",
            "label": f"Group {n} → LR Level",
            "min": LEVEL_MIN,
            "max": LEVEL_MAX,
        }
        out[f"grp{n:02d}_to_lr_pan"] = {
            "type": "number",
            "label": f"Group {n} → LR Pan",
            "min": PAN_LEFT,
            "max": PAN_RIGHT,
        }

    for n in range(1, NUM_AUX + 1):
        out[f"aux{n:02d}_mute"] = {"type": "boolean", "label": f"Aux {n} Master Mute"}
        out[f"aux{n:02d}_fader"] = {
            "type": "number",
            "label": f"Aux {n} Master Fader",
            "min": LEVEL_MIN,
            "max": LEVEL_MAX,
        }

    for n in range(1, NUM_MTX + 1):
        out[f"mtx{n}_mute"] = {"type": "boolean", "label": f"Matrix {n} Master Mute"}
        out[f"mtx{n}_fader"] = {
            "type": "number",
            "label": f"Matrix {n} Master Fader",
            "min": LEVEL_MIN,
            "max": LEVEL_MAX,
        }

    for n in range(1, NUM_FX_SENDS + 1):
        out[f"fxs{n}_mute"] = {"type": "boolean", "label": f"FX Send {n} Mute"}
        out[f"fxs{n}_fader"] = {
            "type": "number",
            "label": f"FX Send {n} Master Fader",
            "min": LEVEL_MIN,
            "max": LEVEL_MAX,
        }

    for n in range(1, NUM_FX_RETURNS + 1):
        out[f"fxr{n}_mute"] = {"type": "boolean", "label": f"FX Return {n} Mute"}
        out[f"fxr{n}_to_lr_level"] = {
            "type": "number",
            "label": f"FX Return {n} → LR Level",
            "min": LEVEL_MIN,
            "max": LEVEL_MAX,
        }
        out[f"fxr{n}_to_lr_pan"] = {
            "type": "number",
            "label": f"FX Return {n} → LR Pan",
            "min": PAN_LEFT,
            "max": PAN_RIGHT,
        }

    for n in range(1, NUM_DCAS + 1):
        out[f"dca{n}_mute"] = {"type": "boolean", "label": f"DCA {n} Mute"}
        out[f"dca{n}_fader"] = {
            "type": "number",
            "label": f"DCA {n} Fader",
            "min": LEVEL_MIN,
            "max": LEVEL_MAX,
        }

    for n in range(1, NUM_MUTE_GROUPS + 1):
        out[f"mgrp{n}_mute"] = {"type": "boolean", "label": f"Mute Group {n} Active"}

    return out


# ── Reverse parameter lookup (incoming NRPN → state key) ─────────────────────

class _ParamMap:
    """Maps every (MSB, LSB) we care about back to the state key it drives.

    Built once at driver construction. Used by the MIDI parser to fan
    incoming NRPN sequences into state updates.
    """

    def __init__(self) -> None:
        self.mute: dict[tuple[int, int], str] = {}
        self.level: dict[tuple[int, int], str] = {}
        self.pan: dict[tuple[int, int], str] = {}

        self.mute[mute_lr()] = "lr_mute"
        self.level[level_lr_master()] = "lr_fader"

        for n in range(1, NUM_INPUTS + 1):
            self.mute[mute_input(n)] = f"in{n:02d}_mute"
            self.level[level_input_to_lr(n)] = f"in{n:02d}_to_lr_level"
            self.pan[pan_input_to_lr(n)] = f"in{n:02d}_to_lr_pan"

        for n in range(1, NUM_GROUPS + 1):
            self.mute[mute_group(n)] = f"grp{n:02d}_mute"
            self.level[level_group_to_lr(n)] = f"grp{n:02d}_to_lr_level"
            self.pan[pan_group_to_lr(n)] = f"grp{n:02d}_to_lr_pan"

        for n in range(1, NUM_AUX + 1):
            self.mute[mute_aux_master(n)] = f"aux{n:02d}_mute"
            self.level[level_aux_master(n)] = f"aux{n:02d}_fader"

        for n in range(1, NUM_MTX + 1):
            self.mute[mute_mtx_master(n)] = f"mtx{n}_mute"
            self.level[level_mtx_master(n)] = f"mtx{n}_fader"

        for n in range(1, NUM_FX_SENDS + 1):
            self.mute[mute_fx_send(n)] = f"fxs{n}_mute"
            self.level[level_fx_send_master(n)] = f"fxs{n}_fader"

        for n in range(1, NUM_FX_RETURNS + 1):
            self.mute[mute_fx_return(n)] = f"fxr{n}_mute"
            self.level[level_fx_return_to_lr(n)] = f"fxr{n}_to_lr_level"
            self.pan[pan_fx_return_to_lr(n)] = f"fxr{n}_to_lr_pan"

        for n in range(1, NUM_DCAS + 1):
            self.mute[mute_dca(n)] = f"dca{n}_mute"
            self.level[level_dca(n)] = f"dca{n}_fader"

        for n in range(1, NUM_MUTE_GROUPS + 1):
            self.mute[mute_mgrp(n)] = f"mgrp{n}_mute"


# ── Driver ───────────────────────────────────────────────────────────────────

class AllenHeathSQDriver(BaseDriver):
    """Allen & Heath SQ-5 / SQ-6 / SQ-7 MIDI-over-TCP driver."""

    DRIVER_INFO = {
        "id": "allenheath_sq",
        "name": "Allen & Heath SQ Digital Mixer",
        "manufacturer": "Allen & Heath",
        "category": "audio",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": (
            "Controls Allen & Heath SQ-5, SQ-6 and SQ-7 digital mixing "
            "consoles via MIDI over TCP/IP on port 51325. Full mute / "
            "level / pan / assignment control across all 48 inputs, "
            "12 groups, 12 aux outs, 3 matrix outs, 4 FX sends, 8 FX "
            "returns, 8 DCAs, and 8 mute groups. Scene recall (1-300), "
            "1 dB increment/decrement, SoftKey triggers, and bidirectional "
            "state — console-side moves push back into OpenAVC state."
        ),
        "source_url": "https://www.allen-heath.com/content/uploads/2023/11/SQ-MIDI-Protocol-Issue5.pdf",
        "tags": ["mixer", "console", "midi", "nrpn", "allen-heath"],
        "verified": False,
        "simulated": True,
        "protocols": ["midi-over-tcp"],
        "ports": [51325],
        "transport": "tcp",
        "discovery": {"ports": [51325]},
        "min_platform_version": "0.6.0",
        "compatible_models": [
            {
                "manufacturer": "Allen & Heath",
                "models": ["SQ-5", "SQ-6", "SQ-7"],
                "confidence": "untested",
                "notes": (
                    "Same MIDI command set across all three. SQ-5 has 8 "
                    "SoftKeys (triggers 9-16 are no-ops). SoftKeys are "
                    "addressed by integer 1-16."
                ),
            },
        ],
        "default_config": {
            "host": "",
            "port": 51325,
            "midi_channel": 1,
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
        },
        "help": {
            "overview": (
                "Controls an Allen & Heath SQ digital mixing console over "
                "the network using MIDI over TCP/IP. Full mute, level, pan "
                "and assignment control plus scene recall and SoftKey "
                "triggers. Console-side moves push back into OpenAVC state "
                "so faders and mute LEDs on a touch panel stay in sync."
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
                "5. The driver subscribes to console-side updates "
                "automatically; no further setup is required."
            ),
        },
        "state_variables": _build_state_vars(),
        "commands": {
            # See command method names below for command identifiers.
            # Filled out at class-construction time so the catalog reflects
            # the actual implementation.
        },
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._param_map = _ParamMap()
        self._rx_buf = bytearray()
        # NRPN aggregator: tracks the last MSB and LSB seen on each MIDI
        # channel so a 4-message sequence (BN 63, BN 62, BN 06, BN 26)
        # resolves to a single parameter update.
        self._nrpn_state: dict[int, dict[str, int]] = {
            ch: {"msb": 0, "lsb": 0, "vc": 0} for ch in range(16)
        }
        self._poll_interval = float(self.config.get("poll_interval", 60.0))

    # ── Connection lifecycle ────────────────────────────────────────────

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
            delimiter=None,                       # raw MIDI byte stream, not line-framed
            name=self.device_id,
        )
        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.{self.device_id}.connected", {})
        log.info("[%s] connected to %s:%d", self.device_id, host, port)

        # Initial sweep — query every parameter we expose as state.
        await asyncio.sleep(0.1)
        await self._refresh_all()

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

    # ── Send-command dispatch ───────────────────────────────────────────

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        """Required BaseDriver entry point. Dispatches to a method on self."""
        params = params or {}
        method = getattr(self, f"cmd_{command}", None)
        if method is None:
            raise ValueError(f"Unknown command: {command}")
        return await method(**params)

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
            await self._send(self._build_nrpn_inc(msb, lsb))
        else:
            await self._send(self._build_mute(msb, lsb, action == "on"))

    async def cmd_mute_input(self, input: int, action: str = "on") -> None:
        await self._do_mute(*mute_input(int(input)), action)

    async def cmd_mute_group(self, group: int, action: str = "on") -> None:
        await self._do_mute(*mute_group(int(group)), action)

    async def cmd_mute_aux_master(self, aux: int, action: str = "on") -> None:
        await self._do_mute(*mute_aux_master(int(aux)), action)

    async def cmd_mute_mtx_master(self, mtx: int, action: str = "on") -> None:
        await self._do_mute(*mute_mtx_master(int(mtx)), action)

    async def cmd_mute_fx_send(self, fx: int, action: str = "on") -> None:
        await self._do_mute(*mute_fx_send(int(fx)), action)

    async def cmd_mute_fx_return(self, fx: int, action: str = "on") -> None:
        await self._do_mute(*mute_fx_return(int(fx)), action)

    async def cmd_mute_dca(self, dca: int, action: str = "on") -> None:
        await self._do_mute(*mute_dca(int(dca)), action)

    async def cmd_mute_mgrp(self, mgrp: int, action: str = "on") -> None:
        await self._do_mute(*mute_mgrp(int(mgrp)), action)

    async def cmd_mute_lr(self, action: str = "on") -> None:
        await self._do_mute(*mute_lr(), action)

    # ── Commands: levels (absolute) ─────────────────────────────────────

    async def _do_level(self, msb: int, lsb: int, level: float) -> None:
        vc, vf = level_to_vcvf(float(level))
        await self._send(self._build_nrpn(msb, lsb, vc, vf))

    async def cmd_set_input_to_lr_level(self, input: int, level: float) -> None:
        await self._do_level(*level_input_to_lr(int(input)), level)

    async def cmd_set_input_to_aux_level(self, input: int, aux: int, level: float) -> None:
        await self._do_level(*level_input_to_aux(int(input), int(aux)), level)

    async def cmd_set_group_to_lr_level(self, group: int, level: float) -> None:
        await self._do_level(*level_group_to_lr(int(group)), level)

    async def cmd_set_group_to_aux_level(self, group: int, aux: int, level: float) -> None:
        await self._do_level(*level_group_to_aux(int(group), int(aux)), level)

    async def cmd_set_fx_return_to_lr_level(self, fx: int, level: float) -> None:
        await self._do_level(*level_fx_return_to_lr(int(fx)), level)

    async def cmd_set_fx_return_to_aux_level(self, fx: int, aux: int, level: float) -> None:
        await self._do_level(*level_fx_return_to_aux(int(fx), int(aux)), level)

    async def cmd_set_input_to_fx_send_level(self, input: int, fx: int, level: float) -> None:
        await self._do_level(*level_input_to_fx_send(int(input), int(fx)), level)

    async def cmd_set_group_to_fx_send_level(self, group: int, fx: int, level: float) -> None:
        await self._do_level(*level_group_to_fx_send(int(group), int(fx)), level)

    async def cmd_set_lr_to_mtx_level(self, mtx: int, level: float) -> None:
        await self._do_level(*level_lr_to_mtx(int(mtx)), level)

    async def cmd_set_aux_to_mtx_level(self, aux: int, mtx: int, level: float) -> None:
        await self._do_level(*level_aux_to_mtx(int(aux), int(mtx)), level)

    async def cmd_set_group_to_mtx_level(self, group: int, mtx: int, level: float) -> None:
        await self._do_level(*level_group_to_mtx(int(group), int(mtx)), level)

    async def cmd_set_lr_master_level(self, level: float) -> None:
        await self._do_level(*level_lr_master(), level)

    async def cmd_set_aux_master_level(self, aux: int, level: float) -> None:
        await self._do_level(*level_aux_master(int(aux)), level)

    async def cmd_set_mtx_master_level(self, mtx: int, level: float) -> None:
        await self._do_level(*level_mtx_master(int(mtx)), level)

    async def cmd_set_fx_send_master_level(self, fx: int, level: float) -> None:
        await self._do_level(*level_fx_send_master(int(fx)), level)

    async def cmd_set_dca_level(self, dca: int, level: float) -> None:
        await self._do_level(*level_dca(int(dca)), level)

    # ── Commands: levels (relative 1 dB step) ───────────────────────────

    async def _do_step(self, msb: int, lsb: int, direction: str) -> None:
        if (direction or "up").lower() == "up":
            await self._send(self._build_nrpn_inc(msb, lsb))
        else:
            await self._send(self._build_nrpn_dec(msb, lsb))

    async def cmd_step_input_to_lr_level(self, input: int, direction: str = "up") -> None:
        await self._do_step(*level_input_to_lr(int(input)), direction)

    async def cmd_step_aux_master_level(self, aux: int, direction: str = "up") -> None:
        await self._do_step(*level_aux_master(int(aux)), direction)

    async def cmd_step_lr_master_level(self, direction: str = "up") -> None:
        await self._do_step(*level_lr_master(), direction)

    async def cmd_step_dca_level(self, dca: int, direction: str = "up") -> None:
        await self._do_step(*level_dca(int(dca)), direction)

    # ── Commands: panning ───────────────────────────────────────────────

    async def _do_pan(self, msb: int, lsb: int, pan: float) -> None:
        vc, vf = pan_to_vcvf(float(pan))
        await self._send(self._build_nrpn(msb, lsb, vc, vf))

    async def cmd_set_input_to_lr_pan(self, input: int, pan: float) -> None:
        await self._do_pan(*pan_input_to_lr(int(input)), pan)

    async def cmd_set_input_to_aux_pan(self, input: int, aux: int, pan: float) -> None:
        await self._do_pan(*pan_input_to_aux(int(input), int(aux)), pan)

    async def cmd_set_group_to_lr_pan(self, group: int, pan: float) -> None:
        await self._do_pan(*pan_group_to_lr(int(group)), pan)

    async def cmd_set_fx_return_to_lr_pan(self, fx: int, pan: float) -> None:
        await self._do_pan(*pan_fx_return_to_lr(int(fx)), pan)

    async def cmd_set_lr_to_mtx_balance(self, mtx: int, pan: float) -> None:
        await self._do_pan(*pan_lr_to_mtx(int(mtx)), pan)

    async def cmd_set_aux_to_mtx_balance(self, aux: int, mtx: int, pan: float) -> None:
        await self._do_pan(*pan_aux_to_mtx(int(aux), int(mtx)), pan)

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

        targets: list[tuple[int, int]] = []
        targets.append(mute_lr())
        targets.append(level_lr_master())

        for n in range(1, NUM_INPUTS + 1):
            targets.append(mute_input(n))
            targets.append(level_input_to_lr(n))
            targets.append(pan_input_to_lr(n))
        for n in range(1, NUM_GROUPS + 1):
            targets.append(mute_group(n))
            targets.append(level_group_to_lr(n))
            targets.append(pan_group_to_lr(n))
        for n in range(1, NUM_AUX + 1):
            targets.append(mute_aux_master(n))
            targets.append(level_aux_master(n))
        for n in range(1, NUM_MTX + 1):
            targets.append(mute_mtx_master(n))
            targets.append(level_mtx_master(n))
        for n in range(1, NUM_FX_SENDS + 1):
            targets.append(mute_fx_send(n))
            targets.append(level_fx_send_master(n))
        for n in range(1, NUM_FX_RETURNS + 1):
            targets.append(mute_fx_return(n))
            targets.append(level_fx_return_to_lr(n))
            targets.append(pan_fx_return_to_lr(n))
        for n in range(1, NUM_DCAS + 1):
            targets.append(mute_dca(n))
            targets.append(level_dca(n))
        for n in range(1, NUM_MUTE_GROUPS + 1):
            targets.append(mute_mgrp(n))

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
        running_status = getattr(self, "_running_status", 0)

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
        # The console-side scene change emits Bank Select (B0 00 BK) followed
        # by Program Change (CN PG). We track only the program for state
        # purposes — bank is rare enough and we cache the last bank seen
        # via CC if needed. For simplicity, infer the scene from current
        # program plus 1 within bank 0; banked scenes need the full
        # sequence which the parser collects below.
        if ch != self._ch:
            return
        # The bank value is the most recent CC 0x00 (Bank MSB) on this
        # channel — cached in nrpn["msb"] only if a Bank Select was just
        # received, which can collide with NRPN. Track it separately.
        bank = getattr(self, "_last_bank", 0)
        scene = bank * 128 + program + 1
        if MIN_SCENE <= scene <= MAX_SCENE:
            self.set_state("current_scene", scene)

    def _handle_bank_select_msb(self, ch: int, value: int) -> None:
        if ch == self._ch:
            self._last_bank = value

    def _dispatch_absolute(self, msb: int, lsb: int, vc: int, vf: int) -> None:
        """Map an absolute (MSB, LSB, VC, VF) tuple to a state update."""
        key = (msb, lsb)

        # Mutes use VC=0, VF=0/1.
        if key in self._param_map.mute:
            self.set_state(self._param_map.mute[key], bool(vf & 0x01))
            return

        # Levels: 14-bit fader position.
        if key in self._param_map.level:
            self.set_state(self._param_map.level[key], vcvf_to_level(vc, vf))
            return

        # Pans: 14-bit pan position.
        if key in self._param_map.pan:
            self.set_state(self._param_map.pan[key], vcvf_to_pan(vc, vf))
            return

        # Unknown parameter — ignore. (E.g. send-level matrix cells we
        # didn't expose as state but that the console may still echo.)

    def _dispatch_toggle(self, msb: int, lsb: int) -> None:
        """Console-side mute/assign toggle echo. Flip the state we hold."""
        key = (msb, lsb)
        state_key = self._param_map.mute.get(key)
        if state_key is None:
            return
        cur = self.get_state(state_key)
        self.set_state(state_key, not bool(cur))


# Class export expected by the loader.
DRIVER_CLASS = AllenHeathSQDriver
