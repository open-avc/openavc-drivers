"""
Allen & Heath Qu-5/6/7 — Simulator.

Implements enough of the Qu-5/6/7 MIDI-over-TCP protocol on port 51325 to
exercise the driver:

  - Raw binary TCP (no delimiter), MIDI 1.0 byte stream.
  - NRPN (CC 63 / 62 / 06 / 26) for mutes, levels, pans and assignments.
  - "Get" (CC 60 = 0x7F) replies with the current value as a full 4-message
    absolute set on the same MIDI channel.
  - Increment / decrement (CC 60 / 61 = 0x00) toggles a mute or steps a level
    by 1 dB along the console's fader-law curve.
  - Bank Select + Program Change for scene recall.
  - Note On/Off for Soft Keys (momentary triggers; no state).

Three behaviours here are modelled on purpose, because each one is a case the
driver has to get right and a friendlier simulator would hide:

  1. **A parameter the console does not have is answered with silence.** That
     is what lets the driver discover the channel roster, so the simulator
     carries a real roster and refuses to invent channels outside it. The
     default roster is the one measured on a real Qu-5 -- notably only eight
     addressable mix destinations, and matrices addressable in stereo pairs
     (Mtx1 and Mtx3) rather than all four.
  2. **A change received over MIDI is NOT echoed.** Allen & Heath consoles
     transmit surface moves, not the changes a controller sends them. A
     simulator that echoed would let a driver mirror what it sent and appear
     correct, which is exactly the bug this driver avoids by reading back.
  3. **Audio Taper quantises a level to 64-count steps.** So the value read
     back after a write is routinely not the value written, which is the whole
     reason the driver confirms rather than assumes.

Console-side moves can be injected with ``push_param()`` so the driver's push
parser can be exercised end to end.

Driver side: ``audio/allenheath_qu567.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

VALUE_MAX = 0x3FFF
AUDIO_STEP = 64          # Audio Taper's quantisation, measured on hardware
PAN_CENTRE = 0x1FFF      # CTR = 3F 7F

# Plane bases, mirroring the driver.
PLANE_MUTE, PLANE_LEVEL, PLANE_PAN, PLANE_ASSIGN = 0x00, 0x40, 0x50, 0x60
MSB_DCA, MSB_MGRP = 0x02, 0x04

# 1 dB in raw counts, near unity, for each law. Audio Taper's real curve is
# not constant; 448 is what a Qu-5 was measured to move at 0 dB and is close
# enough for a simulator whose job is to prove the driver reads back.
_DB_STEP = {"audio": 448, "linear": 119}


def _addr(base: int, offset: int) -> tuple[int, int]:
    return (base + (offset >> 7)) & 0x7F, offset & 0x7F


class AllenHeathQu567Simulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "allenheath_qu567",
        "name": "Allen & Heath Qu-5/6/7 Mixer (sim)",
        "transport": "tcp",
        "default_port": 51325,
        "initial_state": {
            "current_scene": 1,
            "lr_mute": False,
            "lr_fader": 0.766,       # 62 00 -- 0 dB on the Audio Taper curve
            "lr_balance": 0.0,
        },
    }

    # Roster of the console being simulated. Defaults match the Qu-5 that the
    # driver was verified against.
    NUM_INPUTS = 32
    NUM_STEREO = 2
    NUM_FX_RETURNS = 6
    NUM_FX_SENDS = 4
    NUM_MIXES = 12
    NUM_GROUPS = 12
    NUM_DCAS = 8
    NUM_MUTE_GROUPS = 8
    # Addressable send destinations. A Qu mix bus is configurable as an Aux or
    # a Group and may be mono or stereo, so the count of destinations a source
    # can reach is NOT the mix count. Eight is what the bench Qu-5 exposed.
    NUM_SEND_DESTS = 8
    # Matrices are stereo pairs, so only the odd (left) index is addressable.
    MATRIX_IDS = (1, 3)

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # Raw binary mode — MIDI is not line-delimited.
        self._delimiter = None
        self._line_mode = False

        cfg = self.config or {}
        self._midi_channel = max(0, min(15, int(cfg.get("midi_channel", 1)) - 1))
        law = str(cfg.get("fader_law", "audio")).lower()
        self._law = law if law in _DB_STEP else "audio"
        for name in ("NUM_SEND_DESTS", "NUM_MIXES", "NUM_INPUTS"):
            if name.lower() in cfg:
                setattr(self, name, int(cfg[name.lower()]))

        self._valid: set[tuple[int, int]] = self._build_roster()
        self._params: dict[tuple[int, int], int] = {}
        self._current_scene = 1
        self._softkey_presses = 0

        self._nrpn: dict[int, dict[str, int]] = {
            ch: {"msb": 0, "lsb": 0, "vc": 0} for ch in range(16)
        }
        self._last_bank: dict[int, int] = {ch: 0 for ch in range(16)}
        self._running_status = 0

        # Masters rest at unity like a console out of the box, so a driver that
        # reads them back sees a plausible desk rather than everything at -inf.
        for addr in self._master_addrs():
            self._params[addr] = 12544 if self._law == "audio" else 15196

    # ── Roster ──────────────────────────────────────────────────────────
    #
    # Every address this console answers for. Anything outside this set is
    # met with silence, which is what the driver's channel discovery reads.

    def _source_offsets(self) -> dict[str, list[int]]:
        return {
            "input": [n - 1 for n in range(1, self.NUM_INPUTS + 1)],
            "stereo": [0x20 + (n - 1) * 2 for n in range(1, self.NUM_STEREO + 1)],
            "usb": [0x24],
            "group": [0x30 + (n - 1) for n in range(1, self.NUM_GROUPS + 1)],
            "fx_return": [0x3C + (n - 1) for n in range(1, self.NUM_FX_RETURNS + 1)],
            "lr": [0x44],
            "mix": [0x45 + (n - 1) for n in range(1, self.NUM_MIXES + 1)],
            "fx_send": [0x51 + (n - 1) for n in range(1, self.NUM_FX_SENDS + 1)],
            "matrix": [0x55 + (n - 1) for n in self.MATRIX_IDS],
        }

    def _master_addrs(self) -> list[tuple[int, int]]:
        out = [_addr(PLANE_LEVEL + 0x0F, 0x00)]                       # LR
        out += [_addr(PLANE_LEVEL + 0x0F, 0x01 + n) for n in range(self.NUM_MIXES)]
        out += [_addr(PLANE_LEVEL + 0x0F, 0x0D + n) for n in range(self.NUM_FX_SENDS)]
        out += [_addr(PLANE_LEVEL + 0x0F, 0x11 + (n - 1)) for n in self.MATRIX_IDS]
        out += [_addr(PLANE_LEVEL + 0x0F, 0x20 + n) for n in range(self.NUM_DCAS)]
        return out

    def _build_roster(self) -> set[tuple[int, int]]:
        valid: set[tuple[int, int]] = set()
        src = self._source_offsets()

        # Mutes. Groups deliberately have none: on this generation a mix bus is
        # configurable as an Aux or a Group, so "Grp" is a send source rather
        # than a bus of its own. Measured on hardware.
        muted = (src["input"] + src["stereo"] + src["usb"] + src["fx_return"]
                 + src["lr"] + src["mix"] + src["fx_send"] + src["matrix"])
        for off in muted:
            valid.add(_addr(PLANE_MUTE, off))
        for n in range(self.NUM_DCAS):
            valid.add(_addr(MSB_DCA, n))
        for n in range(self.NUM_MUTE_GROUPS):
            valid.add(_addr(MSB_MGRP, n))

        # Sends, pans and assignments share one base map across three planes.
        senders = (src["input"] + src["stereo"] + src["usb"] + src["group"]
                   + src["fx_return"])
        for plane in (PLANE_LEVEL, PLANE_PAN, PLANE_ASSIGN):
            for off in senders:
                valid.add(_addr(plane, off))                      # -> LR
                for d in range(self.NUM_SEND_DESTS):              # -> MIX
                    valid.add(_addr(plane, 0x44 + off * self.NUM_MIXES + d))
                for f in range(self.NUM_FX_SENDS):                # -> FX send
                    valid.add(_addr(plane + 0x0C,
                                    0x14 + off * self.NUM_FX_SENDS + f))
            # LR and each mix into each matrix.
            for mix_index in range(0, self.NUM_MIXES + 1):
                for m in self.MATRIX_IDS:
                    valid.add(_addr(plane + 0x0E, 0x24 + mix_index * 3 + (m - 1)))

        # Output masters: level and pan, but DCAs have level only.
        for plane in (PLANE_LEVEL, PLANE_PAN):
            valid.add(_addr(plane + 0x0F, 0x00))
            for n in range(self.NUM_MIXES):
                valid.add(_addr(plane + 0x0F, 0x01 + n))
            for n in range(self.NUM_FX_SENDS):
                valid.add(_addr(plane + 0x0F, 0x0D + n))
            for n in self.MATRIX_IDS:
                valid.add(_addr(plane + 0x0F, 0x11 + (n - 1)))
        for n in range(self.NUM_DCAS):
            valid.add(_addr(PLANE_LEVEL + 0x0F, 0x20 + n))

        return valid

    # ── REST/exposed state ──────────────────────────────────────────────

    def get_state(self) -> dict[str, Any]:
        return {
            "current_scene": self._current_scene,
            "addressable_parameters": len(self._valid),
            "parameters_set": len(self._params),
            "midi_channel": self._midi_channel + 1,
            "fader_law": self._law,
            "softkey_presses": self._softkey_presses,
        }

    async def set_state_value(self, key: str, value: Any) -> dict[str, Any]:
        if key == "current_scene":
            self._current_scene = int(value)
            await self._broadcast_scene()
            return {"ok": True}
        return {"ok": False, "error": f"unknown key: {key}"}

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
                        running = 0
                        continue
                    i += 1
                    running = 0
                    continue
                running = b
                i += 1
                continue

            if not running:
                i += 1
                continue

            high = running & 0xF0
            ch = running & 0x0F

            if high in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
                if i + 1 >= len(data):
                    break
                d1, d2 = data[i], data[i + 1]
                i += 2
                if d1 & 0x80 or d2 & 0x80:
                    running = 0
                    continue
                if high == 0xB0:
                    reply = self._handle_cc(ch, d1, d2)
                    if reply:
                        out.extend(reply)
                elif high == 0x90 and d2:
                    self._softkey_presses += 1
            elif high in (0xC0, 0xD0):
                if i >= len(data):
                    break
                d1 = data[i]
                i += 1
                if d1 & 0x80:
                    running = 0
                    continue
                if high == 0xC0:
                    self._handle_program_change(ch, d1)
            else:
                i += 1

        self._running_status = running
        return bytes(out) if out else None

    # ── CC dispatch ─────────────────────────────────────────────────────

    def _handle_cc(self, ch: int, controller: int, value: int) -> bytes | None:
        if controller == 0x00:
            self._last_bank[ch] = value
            return None

        nrpn = self._nrpn[ch]
        if controller == 0x63:
            nrpn["msb"] = value
            return None
        if controller == 0x62:
            nrpn["lsb"] = value
            return None
        if controller == 0x06:
            nrpn["vc"] = value
            return None
        if controller == 0x26:
            self._on_absolute(nrpn["msb"], nrpn["lsb"], nrpn["vc"], value)
            return None                 # deliberately no echo -- see the header
        if controller == 0x60:
            if value == 0x7F:
                return self._reply_get(ch, nrpn["msb"], nrpn["lsb"])
            if value == 0x00:
                self._on_step(nrpn["msb"], nrpn["lsb"], +1)
                return None
        if controller == 0x61 and value == 0x00:
            self._on_step(nrpn["msb"], nrpn["lsb"], -1)
        return None

    def _on_absolute(self, msb: int, lsb: int, vc: int, vf: int) -> None:
        key = (msb, lsb)
        if key not in self._valid:
            return                      # the console has no such parameter
        if self._is_switch(msb):
            self._params[key] = vf & 0x01
        else:
            self._params[key] = self._quantise(msb, ((vc & 0x7F) << 7) | (vf & 0x7F))

    def _on_step(self, msb: int, lsb: int, sign: int) -> None:
        key = (msb, lsb)
        if key not in self._valid:
            return
        if self._is_switch(msb):
            self._params[key] = 0 if self._current(key) else 1
            return
        step = _DB_STEP[self._law]
        new = max(0, min(VALUE_MAX, self._current(key) + sign * step))
        self._params[key] = self._quantise(msb, new)

    def _quantise(self, msb: int, raw: int) -> int:
        """Audio Taper resolves a level to 64-count steps; pans do not."""
        if self._law != "audio" or self._is_pan(msb):
            return raw
        return min(VALUE_MAX, (raw // AUDIO_STEP) * AUDIO_STEP)

    def _current(self, key: tuple[int, int]) -> int:
        if key in self._params:
            return self._params[key]
        return PAN_CENTRE if self._is_pan(key[0]) else 0

    def _reply_get(self, ch: int, msb: int, lsb: int) -> bytes | None:
        key = (msb, lsb)
        if key not in self._valid:
            return None                 # silence: this console has no such parameter
        cur = self._current(key)
        if self._is_switch(msb):
            return self._build_nrpn(ch, msb, lsb, 0, cur & 0x01)
        return self._build_nrpn(ch, msb, lsb, (cur >> 7) & 0x7F, cur & 0x7F)

    @staticmethod
    def _is_switch(msb: int) -> bool:
        """Mutes (MSB 00/02/04) and assignments (MSB 60-6F) are on/off."""
        return msb in (PLANE_MUTE, MSB_DCA, MSB_MGRP) or (
            PLANE_ASSIGN <= msb <= PLANE_ASSIGN + 0x0F)

    @staticmethod
    def _is_pan(msb: int) -> bool:
        return PLANE_PAN <= msb <= PLANE_PAN + 0x0F

    def _handle_program_change(self, ch: int, program: int) -> None:
        if ch != self._midi_channel:
            return
        scene = self._last_bank.get(ch, 0) * 128 + program + 1
        if 1 <= scene <= 300:
            self._current_scene = scene

    # ── Push API (console-side moves) ───────────────────────────────────

    async def _broadcast_scene(self) -> None:
        idx = self._current_scene - 1
        ch = self._midi_channel
        await self.push(bytes([0xB0 | ch, 0x00, idx // 128,
                               0xC0 | ch, idx % 128]))

    async def push_param(self, msb: int, lsb: int, vc: int, vf: int) -> None:
        """Inject a console-side move: store it and transmit the NRPN, the way
        the console does when somebody touches the surface."""
        key = (msb, lsb)
        if key not in self._valid:
            return
        if self._is_switch(msb):
            self._params[key] = vf & 0x01
        else:
            self._params[key] = self._quantise(msb, ((vc & 0x7F) << 7) | (vf & 0x7F))
        await self.push(self._build_nrpn(self._midi_channel, msb, lsb, vc, vf))

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _build_nrpn(ch: int, msb: int, lsb: int, vc: int, vf: int) -> bytes:
        b = 0xB0 | (ch & 0x0F)
        return bytes([b, 0x63, msb & 0x7F, b, 0x62, lsb & 0x7F,
                      b, 0x06, vc & 0x7F, b, 0x26, vf & 0x7F])


SIMULATOR_CLASS = AllenHeathQu567Simulator
