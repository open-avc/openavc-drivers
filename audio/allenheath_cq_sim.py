"""
Allen & Heath CQ — Simulator.

Implements the CQ's MIDI-over-TCP protocol on port 51325 closely enough to
exercise the driver:

  - Raw binary TCP (no delimiter), MIDI 1.0 byte stream on MIDI channel 1.
  - NRPN (CC 63 / 62 / 06 / 26) for mutes, levels and pans.
  - "Get" (CC 60 = 7F) answered with the current value as a full four-message
    absolute set.
  - Increment / decrement (CC 60 / 61 = 00): a mute toggles, a level moves
    1 dB along the document's level table, a pan moves one step.
  - Bank Select + Program Change for scene recall (scenes 1-100).
  - Note On/Off for the SoftKeys (momentary; counted, no state).

The Model setting ("model": CQ-12T, CQ-18T or CQ-20B) decides which channels
exist, from Allen & Heath's model table.

Behaviours modelled on purpose, because each is a case the driver has to get
right and a friendlier simulator would hide:

  1. **A change received over MIDI is not echoed.** Allen & Heath consoles
     report surface moves, not the changes a controller sends them (measured
     on the Qu and SQ generations), so a driver has to read a write back.
  2. **A Get for a DCA or mute group mute always answers "off", and a toggle
     of one does nothing** (CQ firmware V1.2.2 known issues ID-1878 and
     ID-1875). A driver that read those mutes back would be told "off" here.
  3. **A parameter the model does not have is answered with silence**, as a
     Qu-5/6/7 does (measured there; on a CQ it has not been seen).
  4. **Active Sense.** Firmware V1.2.2 added MIDI Active Sense (FE). The
     release notes say no more than that, so when a CQ sends it is not known;
     here every reply is preceded by one, so a driver's parser and a
     discovery probe meet it.

A change made in the Simulator UI (the controls below) is sent to the driver
as the console sends a surface move. ``push_param()`` does the same for any
parameter.

Driver side: ``audio/allenheath_cq.py``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

VALUE_MAX = 0x3FFF
PAN_CENTRE = 0x2000          # 40 00, the document's centre
UNITY = 0x3100               # 62 00 = 0 dB in the document's level table
ACTIVE_SENSE = b"\xFE"

PLANE_MUTE, PLANE_LEVEL, PLANE_PAN = 0x00, 0x40, 0x50
MSB_DCA, MSB_MGRP = 0x02, 0x04

MODELS: dict[str, dict[str, Any]] = {
    "CQ-12T": {"inputs": 10, "stereo": (0x18, 0x1C, 0x1E), "fx": 2, "softkeys": 3},
    "CQ-18T": {"inputs": 16, "stereo": (0x18, 0x1C, 0x1E), "fx": 4, "softkeys": 3},
    "CQ-20B": {"inputs": 16, "stereo": (0x18, 0x1A, 0x1C, 0x1E), "fx": 4,
               "softkeys": 0},
}
DEFAULT_MODEL = "CQ-18T"

# The document's "Example Level Values": (dB, raw).
_LEVELS: tuple[tuple[float, int], ...] = tuple((db, (vc << 7) | vf) for db, vc, vf in (
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
))


def _db_to_raw(db: float) -> int:
    if db <= -90:
        return 0
    if db <= _LEVELS[0][0]:
        return _LEVELS[0][1]
    for (d0, r0), (d1, r1) in zip(_LEVELS, _LEVELS[1:]):
        if db <= d1:
            return int(round(r0 + (db - d0) / (d1 - d0) * (r1 - r0)))
    return _LEVELS[-1][1]


def _raw_to_db(raw: int) -> float:
    if raw <= 0:
        return -90.0
    if raw <= _LEVELS[0][1]:
        return float(_LEVELS[0][0])
    for (d0, r0), (d1, r1) in zip(_LEVELS, _LEVELS[1:]):
        if raw <= r1:
            return round(d0 + (raw - r0) / (r1 - r0) * (d1 - d0), 1)
    return float(_LEVELS[-1][0])


def _addr(base: int, offset: int) -> tuple[int, int]:
    return (base + (offset >> 7)) & 0x7F, offset & 0x7F


# Simulator UI controls: key -> (address, kind). A toggle stores a real
# boolean; a slider holds dB.
_UI_KEYS: dict[str, tuple[tuple[int, int], str]] = {
    "in01_mute": ((0x00, 0x00), "mute"),
    "in02_mute": ((0x00, 0x01), "mute"),
    "in01_lr_db": ((0x40, 0x00), "db"),
    "st1_mute": ((0x00, 0x18), "mute"),
    "out1_mute": ((0x00, 0x45), "mute"),
    "lr_mute": ((0x00, 0x44), "mute"),
    "lr_fader_db": ((0x4F, 0x00), "db"),
    "dca1_mute": ((MSB_DCA, 0x00), "mute"),
    "mg1_mute": ((MSB_MGRP, 0x00), "mute"),
}


class AllenHeathCQSimulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "allenheath_cq",
        "name": "Allen & Heath CQ Mixer (sim)",
        "category": "audio",
        "transport": "tcp",
        "default_port": 51325,
        "initial_state": {
            "in01_mute": False,
            "in02_mute": False,
            "in01_lr_db": -90.0,
            "st1_mute": False,
            "out1_mute": False,
            "lr_mute": False,
            "lr_fader_db": 0.0,
            "dca1_mute": False,
            "mg1_mute": False,
            "last_scene": 0,
            "softkey_presses": 0,
        },
        "controls": [
            {"type": "group", "label": "Inputs", "controls": [
                {"type": "toggle", "key": "in01_mute", "label": "Input 1 Mute"},
                {"type": "toggle", "key": "in02_mute", "label": "Input 2 Mute"},
                {"type": "slider", "key": "in01_lr_db", "label": "Input 1 to LR",
                 "min": -90, "max": 10, "step": 0.5, "unit": "dB"},
                {"type": "toggle", "key": "st1_mute", "label": "ST1 Mute"},
            ]},
            {"type": "group", "label": "Outputs", "controls": [
                {"type": "toggle", "key": "lr_mute", "label": "Main LR Mute"},
                {"type": "slider", "key": "lr_fader_db", "label": "Main LR Fader",
                 "min": -90, "max": 10, "step": 0.5, "unit": "dB"},
                {"type": "toggle", "key": "out1_mute", "label": "Out 1 Mute"},
                {"type": "toggle", "key": "dca1_mute", "label": "DCA 1 Mute"},
                {"type": "toggle", "key": "mg1_mute", "label": "Mute Group 1"},
            ]},
            {"type": "group", "label": "Console", "controls": [
                {"type": "indicator", "key": "last_scene", "label": "Last Scene Recalled"},
                {"type": "indicator", "key": "softkey_presses", "label": "SoftKey Presses"},
            ]},
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # Raw binary mode: MIDI is not line-delimited.
        self._delimiter = None
        self._line_mode = False

        model = str((self.config or {}).get("model", DEFAULT_MODEL)).strip().upper()
        self._model = model if model in MODELS else DEFAULT_MODEL
        self._spec = MODELS[self._model]

        self._valid: set[tuple[int, int]] = self._build_roster()
        self._params: dict[tuple[int, int], int] = {}
        self._nrpn = {"msb": 0, "lsb": 0, "vc": 0}
        self._last_bank = 0
        self._running_status = 0
        self._from_wire = False

        # Masters rest at 0 dB, like a mixer out of the box.
        for addr in self._masters():
            self._params[addr] = UNITY

    # ── Roster ──────────────────────────────────────────────────────────

    def _sources(self) -> list[int]:
        return ([n for n in range(self._spec["inputs"])]
                + list(self._spec["stereo"])
                + [0x3C + n for n in range(self._spec["fx"])])

    def _masters(self) -> list[tuple[int, int]]:
        out = [(0x4F, 0x00)]
        out += [(0x4F, 0x01 + n) for n in range(6)]
        out += [(0x4F, 0x0D + n) for n in range(self._spec["fx"])]
        out += [(0x4F, 0x20 + n) for n in range(4)]
        return out

    def _build_roster(self) -> set[tuple[int, int]]:
        valid: set[tuple[int, int]] = set()
        fx = self._spec["fx"]
        inputs_and_stereo = ([n for n in range(self._spec["inputs"])]
                             + list(self._spec["stereo"]))

        # Mutes: channels, the Main LR, Out1-6, the FX mutes, DCAs, groups.
        for off in inputs_and_stereo:
            valid.add(_addr(PLANE_MUTE, off))
        valid.add((0x00, 0x44))
        for n in range(6):
            valid.add((0x00, 0x45 + n))
        for n in range(fx):
            valid.add((0x00, 0x51 + n))
        for n in range(4):
            valid.add((MSB_DCA, n))
            valid.add((MSB_MGRP, n))

        for off in self._sources():
            # To the Main LR and Out1-6 (level), LR and the pairs (pan).
            valid.add(_addr(PLANE_LEVEL, off))
            valid.add(_addr(PLANE_PAN, off))
            for out in range(6):
                valid.add(_addr(PLANE_LEVEL, 0x44 + off * 12 + out))
            for out in (0, 2, 4):
                valid.add(_addr(PLANE_PAN, 0x44 + off * 12 + out))
            # To the FX units, except an FX return to its own unit.
            for unit in range(fx):
                if off == 0x3C + unit:
                    continue
                valid.add(_addr(PLANE_LEVEL + 0x0C, 0x14 + off * 4 + unit))

        valid.update(self._masters())
        return valid

    # ── Frame handler ───────────────────────────────────────────────────

    def handle_command(self, data: bytes) -> bytes | None:
        out = bytearray()
        i = 0
        running = self._running_status
        while i < len(data):
            b = data[i]
            if 0xF8 <= b <= 0xFF:
                i += 1
                continue
            if b & 0x80:
                if 0xF0 <= b <= 0xF7:
                    if b == 0xF0:
                        end = data.find(0xF7, i + 1)
                        if end == -1:
                            break
                        i = end + 1
                    else:
                        i += 1
                    running = 0
                    continue
                running = b
                i += 1
                continue
            if not running:
                i += 1
                continue

            high, ch = running & 0xF0, running & 0x0F
            if high in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
                if i + 1 >= len(data):
                    break
                d1, d2 = data[i], data[i + 1]
                i += 2
                if d1 & 0x80 or d2 & 0x80:
                    running = 0
                    continue
                if ch != 0:
                    continue                    # the CQ listens on channel 1
                if high == 0xB0:
                    reply = self._handle_cc(d1, d2)
                    if reply:
                        out.extend(ACTIVE_SENSE + reply)
                elif high == 0x90 and d2 and self._spec["softkeys"] \
                        and 0x30 <= d1 < 0x30 + self._spec["softkeys"]:
                    self.set_state("softkey_presses",
                                   int(self.get_state("softkey_presses", 0)) + 1)
            elif high in (0xC0, 0xD0):
                if i >= len(data):
                    break
                d1 = data[i]
                i += 1
                if d1 & 0x80:
                    running = 0
                    continue
                if high == 0xC0 and ch == 0 and self._last_bank == 0 and d1 < 100:
                    self.set_state("last_scene", d1 + 1)
            else:
                i += 1

        self._running_status = running
        return bytes(out) if out else None

    def _handle_cc(self, controller: int, value: int) -> bytes | None:
        nrpn = self._nrpn
        if controller == 0x00:
            self._last_bank = value
        elif controller == 0x63:
            nrpn["msb"] = value
        elif controller == 0x62:
            nrpn["lsb"] = value
        elif controller == 0x06:
            nrpn["vc"] = value
        elif controller == 0x26:
            # Deliberately no echo: see the header.
            self._store((nrpn["msb"], nrpn["lsb"]),
                        ((nrpn["vc"] & 0x7F) << 7) | (value & 0x7F))
        elif controller == 0x60 and value == 0x7F:
            return self._reply_get(nrpn["msb"], nrpn["lsb"])
        elif controller == 0x60 and value == 0x00:
            self._step((nrpn["msb"], nrpn["lsb"]), +1)
        elif controller == 0x61 and value == 0x00:
            self._step((nrpn["msb"], nrpn["lsb"]), -1)
        return None

    # ── Parameters ──────────────────────────────────────────────────────

    @staticmethod
    def _is_mute(msb: int) -> bool:
        return msb in (PLANE_MUTE, MSB_DCA, MSB_MGRP)

    @staticmethod
    def _is_pan(msb: int) -> bool:
        return PLANE_PAN <= msb <= PLANE_PAN + 0x0F

    def _current(self, key: tuple[int, int]) -> int:
        if key in self._params:
            return self._params[key]
        return PAN_CENTRE if self._is_pan(key[0]) else 0

    def _store(self, key: tuple[int, int], raw: int) -> None:
        if key not in self._valid:
            return                              # this model has no such parameter
        self._params[key] = (raw & 0x01) if self._is_mute(key[0]) else raw
        self._mirror(key)

    def _step(self, key: tuple[int, int], sign: int) -> None:
        if key not in self._valid:
            return
        if key[0] in (MSB_DCA, MSB_MGRP):
            return                              # ID-1875: no toggle here
        cur = self._current(key)
        if self._is_mute(key[0]):
            self._params[key] = 0 if cur else 1
        elif self._is_pan(key[0]):
            self._params[key] = max(0, min(VALUE_MAX, cur + sign * 0x80))
        else:
            self._params[key] = _db_to_raw(_raw_to_db(cur) + sign)
        self._mirror(key)

    def _reply_get(self, msb: int, lsb: int) -> bytes | None:
        key = (msb, lsb)
        if key not in self._valid:
            return None                         # silence: no such parameter
        if msb in (MSB_DCA, MSB_MGRP):
            return self._nrpn_msg(msb, lsb, 0, 0)   # ID-1878: always "off"
        cur = self._current(key)
        if self._is_mute(msb):
            return self._nrpn_msg(msb, lsb, 0, cur & 0x01)
        return self._nrpn_msg(msb, lsb, (cur >> 7) & 0x7F, cur & 0x7F)

    @staticmethod
    def _nrpn_msg(msb: int, lsb: int, vc: int, vf: int) -> bytes:
        return bytes([0xB0, 0x63, msb & 0x7F, 0xB0, 0x62, lsb & 0x7F,
                      0xB0, 0x06, vc & 0x7F, 0xB0, 0x26, vf & 0x7F])

    # ── Simulator UI ────────────────────────────────────────────────────

    def _mirror(self, key: tuple[int, int]) -> None:
        """Show a parameter the driver changed on the UI control for it,
        without sending it back (the console does not echo)."""
        for ui_key, (addr, kind) in _UI_KEYS.items():
            if addr != key:
                continue
            raw = self._params.get(key, 0)
            value: Any = bool(raw & 0x01) if kind == "mute" else _raw_to_db(raw)
            self._from_wire = True
            try:
                self.set_state(ui_key, value)
            finally:
                self._from_wire = False

    def set_state(self, key: str, value: Any) -> None:
        old = self._state.get(key)
        super().set_state(key, value)
        if self._from_wire or old == value or key not in _UI_KEYS:
            return
        addr, kind = _UI_KEYS[key]
        if addr not in self._valid:
            return
        # A change in the Simulator UI is a move on the mixer's surface: the
        # console reports it. A toggle stores a real boolean.
        if kind == "mute":
            raw = 1 if bool(value) else 0
        else:
            raw = _db_to_raw(float(value or 0.0))
        self._params[addr] = raw
        self._send_soon(self._nrpn_msg(*addr, (raw >> 7) & 0x7F, raw & 0x7F))

    def _send_soon(self, data: bytes) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.push(data))

    async def push_param(self, msb: int, lsb: int, vc: int, vf: int) -> None:
        """Inject a move on the mixer: store it and send the NRPN, as the
        console does when somebody touches the surface."""
        key = (msb, lsb)
        if key not in self._valid:
            return
        self._store(key, ((vc & 0x7F) << 7) | (vf & 0x7F))
        await self.push(self._nrpn_msg(msb, lsb, vc, vf))


SIMULATOR_CLASS = AllenHeathCQSimulator
