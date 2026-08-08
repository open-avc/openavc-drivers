"""
Allen & Heath Avantis — Simulator.

Implements the Avantis MIDI-over-TCP protocol on port 51325 well enough
to exercise the driver:

  - Raw binary TCP (no delimiter), MIDI 1.0 wire format.
  - 5-MIDI-channel range (base..base+4) addressing for 96 inputs, 54
    mono / 27 stereo groups, 54 mono / 27 stereo aux, 54 mono / 27
    stereo matrix, 12 mono / 12 stereo FX sends, 12 FX returns, 3
    mains, 16 DCAs, 8 mute groups, 8 stereo UFX sends, 8 stereo UFX
    returns.
  - Note-On pair mutes (9N CH 7F / 9N CH 3F + trailing 9N CH 00).
  - NRPN parameter ID 17 (fader), 18 (main assign), 40 (DCA / mute-
    group assign), all using the 3-message form (no VF byte).
  - SysEx Channel Name Get / Set (cmd 01 / 03), Channel Colour Get /
    Set (cmd 04 / 06), Send Level Set (cmd 0D), MMC transport. Name and
    colour Gets answer in the documented reply shapes (0N 02 CH Name /
    0N 05 CH Col) — the driver's connect sweep and liveness probe rely
    on them. There is NO mute / fader Get in the Avantis protocol (the
    dLive Get-status family is absent), so none is simulated.
  - Bank Select + Program Change for scene recall (1..500).
  - UFX Global Key / Scale (CC 0C / 0D).

When the driver Sets a fader or mute, the simulator echoes the same
shape back so the driver's push parser can be exercised (the driver
itself applies writes optimistically and does not rely on the echo).
``push_mute`` / ``push_fader`` test hooks let a unit test simulate
console-side moves end-to-end.

Driver side: ``audio/allenheath_avantis.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)


# ── Channel address tables (mirror of the driver's CHANNEL_TYPES) ────────────

CHANNEL_TYPES: dict[str, tuple[int, int, int]] = {
    # ctype → (midi_ch_offset, base_note, count)
    "input":          (0, 0x00, 96),
    "mono_group":     (1, 0x00, 54),
    "stereo_group":   (1, 0x40, 27),
    "mono_aux":       (2, 0x00, 54),
    "stereo_aux":     (2, 0x40, 27),
    "mono_matrix":    (3, 0x00, 54),
    "stereo_matrix":  (3, 0x40, 27),
    "mono_fx_send":   (4, 0x00, 12),
    "stereo_fx_send": (4, 0x10, 12),
    "fx_return":      (4, 0x20, 12),
    "main":           (4, 0x30, 3),
    "dca":            (4, 0x36, 16),
    "mute_group":     (4, 0x46, 8),
    "ufx_send":       (4, 0x56, 8),
    "ufx_return":     (4, 0x5E, 8),
}

NRPN_PARAM_FADER = 0x17
NRPN_PARAM_MAIN_ASSIGN = 0x18
NRPN_PARAM_BUS_ASSIGN = 0x40

CC_BANK_SELECT_MSB = 0x00
CC_NRPN_MSB = 0x63
CC_NRPN_LSB = 0x62
CC_DATA_ENTRY_MSB = 0x06
CC_UFX_GLOBAL_KEY = 0x0C
CC_UFX_GLOBAL_SCALE = 0x0D

SYSEX_HEADER = bytes([0x00, 0x00, 0x1A, 0x50, 0x10, 0x01, 0x00])


def _build_addr_map() -> dict[tuple[int, int], tuple[str, int]]:
    out: dict[tuple[int, int], tuple[str, int]] = {}
    for ctype, (offset, base_note, count) in CHANNEL_TYPES.items():
        for n in range(1, count + 1):
            out[(offset, base_note + (n - 1))] = (ctype, n)
    return out


ADDR_MAP = _build_addr_map()


class AllenHeathAvantisSimulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "allenheath_avantis",
        "name": "Allen & Heath Avantis Mixer (sim)",
        "transport": "tcp",
        "default_port": 51325,
        "initial_state": {
            "current_scene": 0,
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # Raw binary mode — MIDI is not line-delimited.
        self._delimiter = None
        self._line_mode = False

        cfg = self.config or {}
        # Base MIDI channel is 1-based in config (default 12), 0-based
        # internally (default 11).
        self._base_midi = max(0, min(11, int(cfg.get("base_midi_channel", 12)) - 1))

        # Per-channel state stores. Keyed by (ctype, n).
        self._mute: dict[tuple[str, int], bool] = {}
        self._fader: dict[tuple[str, int], int] = {}      # 0..127 LV
        self._name: dict[tuple[str, int], bytes] = {}     # raw ASCII bytes
        self._colour: dict[tuple[str, int], int] = {}     # 0..7
        self._main_assign: dict[tuple[str, int], bool] = {}
        self._dca_assign: dict[tuple[tuple[str, int], int], bool] = {}
        self._mute_group_assign: dict[tuple[tuple[str, int], int], bool] = {}
        self._send_level: dict[tuple[tuple[str, int], tuple[str, int]], int] = {}

        self._current_scene = 1
        self._ufx_key = 0     # 0 = C
        self._ufx_scale = 0   # 0 = Major
        self._mmc_last = None

        # NRPN aggregator per MIDI channel.
        self._nrpn: dict[int, dict[str, int]] = {
            ch: {"msb": 0, "lsb": 0, "vc": 0} for ch in range(16)
        }
        self._last_bank: dict[int, int] = {ch: 0 for ch in range(16)}
        self._running_status = 0

    # ── REST/UI exposed state ───────────────────────────────────────────

    def get_state(self) -> dict[str, Any]:
        return {
            "current_scene": self._current_scene,
            "base_midi_channel": self._base_midi + 1,
            "mute_count": sum(1 for v in self._mute.values() if v),
            "fader_count": len(self._fader),
            "send_count": len(self._send_level),
            "ufx_key": self._ufx_key,
            "ufx_scale": self._ufx_scale,
            "mmc_last": self._mmc_last,
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
                            break  # incomplete
                        reply = self._handle_sysex(bytes(data[i:end + 1]))
                        if reply:
                            out.extend(reply)
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
                if high == 0x90:
                    reply = self._handle_note_on(ch, d1, d2)
                    if reply:
                        out.extend(reply)
                elif high == 0xB0:
                    reply = self._handle_cc(ch, d1, d2)
                    if reply:
                        out.extend(reply)
            elif high in (0xC0, 0xD0):
                if i >= len(data):
                    break
                d1 = data[i]
                i += 1
                if d1 & 0x80:
                    running = 0
                    continue
                if high == 0xC0:
                    reply = self._handle_program_change(ch, d1)
                    if reply:
                        out.extend(reply)
            else:
                i += 1

        self._running_status = running
        return bytes(out) if out else None

    # ── Helpers ─────────────────────────────────────────────────────────

    def _ch_offset(self, ch: int) -> int | None:
        """Return type offset 0..4 for an incoming MIDI channel byte, or
        None if outside our base..base+4 window.
        """
        offset = (ch - self._base_midi) & 0x0F
        if 0 <= offset <= 4:
            return offset
        return None

    def _midi_ch_byte(self, offset: int) -> int:
        return (self._base_midi + offset) & 0x0F

    def _resolve(self, ch: int, ch_note: int) -> tuple[str, int] | None:
        offset = self._ch_offset(ch)
        if offset is None:
            return None
        return ADDR_MAP.get((offset, ch_note))

    # ── Message handlers ────────────────────────────────────────────────

    def _handle_note_on(self, ch: int, note: int, velocity: int) -> bytes | None:
        if velocity == 0x00:
            return None  # trailing Note-Off in a mute pair
        target = self._resolve(ch, note)
        if target is None:
            return None
        on = velocity >= 0x40
        self._mute[target] = on
        # Echo: same Note-On pair (no Note-Off velocity wrapping needed
        # because the driver's parser ignores velocity-0 messages).
        n = 0x90 | (ch & 0x0F)
        return bytes([n, note & 0x7F, 0x7F if on else 0x3F, n, note & 0x7F, 0x00])

    def _handle_cc(self, ch: int, controller: int, value: int) -> bytes | None:
        if controller == CC_BANK_SELECT_MSB:
            self._last_bank[ch] = value
            return None

        if controller == CC_UFX_GLOBAL_KEY:
            if ch == self._midi_ch_byte(0):
                self._ufx_key = value & 0x0F
            return None

        if controller == CC_UFX_GLOBAL_SCALE:
            if ch == self._midi_ch_byte(0):
                self._ufx_scale = value & 0x03
            return None

        nrpn = self._nrpn[ch]
        if controller == CC_NRPN_MSB:
            nrpn["msb"] = value
            return None
        if controller == CC_NRPN_LSB:
            nrpn["lsb"] = value
            return None
        if controller == CC_DATA_ENTRY_MSB:
            return self._handle_nrpn_set(ch, nrpn["msb"], nrpn["lsb"], value)
        return None

    def _handle_nrpn_set(self, ch: int, ch_note: int, param_id: int,
                         value: int) -> bytes | None:
        target = self._resolve(ch, ch_note)
        if target is None:
            return None

        if param_id == NRPN_PARAM_FADER:
            ctype, _ = target
            if ctype == "mute_group":
                return None  # mute groups have no fader
            self._fader[target] = value & 0x7F
            return self._build_nrpn(ch, ch_note, NRPN_PARAM_FADER, value & 0x7F)

        if param_id == NRPN_PARAM_MAIN_ASSIGN:
            on = value >= 0x40
            self._main_assign[target] = on
            echo_value = 0x7F if on else 0x3F
            return self._build_nrpn(ch, ch_note, NRPN_PARAM_MAIN_ASSIGN, echo_value)

        if param_id == NRPN_PARAM_BUS_ASSIGN:
            # value 40..4F = DCA 1..16 ON, 00..0F = OFF
            # value 50..57 = Mute Group 1..8 ON, 10..17 = OFF
            if 0x40 <= value <= 0x4F:
                self._dca_assign[(target, (value - 0x40) + 1)] = True
            elif 0x00 <= value <= 0x0F:
                self._dca_assign[(target, (value - 0x00) + 1)] = False
            elif 0x50 <= value <= 0x57:
                self._mute_group_assign[(target, (value - 0x50) + 1)] = True
            elif 0x10 <= value <= 0x17:
                self._mute_group_assign[(target, (value - 0x10) + 1)] = False
            return self._build_nrpn(ch, ch_note, NRPN_PARAM_BUS_ASSIGN, value & 0x7F)

        return None

    def _handle_program_change(self, ch: int, program: int) -> bytes | None:
        if ch != self._midi_ch_byte(0):
            return None
        bank = self._last_bank.get(ch, 0)
        scene = bank * 128 + program + 1
        if 1 <= scene <= 500:
            self._current_scene = scene
            # Echo: bank select + program change on the same channel.
            b = 0xB0 | (ch & 0x0F)
            c = 0xC0 | (ch & 0x0F)
            return bytes([b, CC_BANK_SELECT_MSB, bank, c, program])
        return None

    def _handle_sysex(self, message: bytes) -> bytes | None:
        # Strip the F0 prefix and F7 suffix, then dispatch.
        if len(message) < 2 or message[0] != 0xF0 or message[-1] != 0xF7:
            return None
        body = message[1:-1]
        # MMC (F0 7F 7F 06 TC F7) does NOT use the Allen & Heath vendor
        # header — handle it first.
        if body[:3] == bytes([0x7F, 0x7F, 0x06]):
            if len(body) >= 4:
                self._mmc_last = body[3]
            return None
        # Everything else is vendor-specific and starts with our header.
        if not body.startswith(SYSEX_HEADER):
            return None
        body = body[len(SYSEX_HEADER):]
        if len(body) < 3:
            return None

        midi_ch_byte = body[0] & 0x0F
        cmd = body[1]
        ch_note = body[2] & 0x7F
        rest = body[3:]

        target = self._resolve(midi_ch_byte, ch_note)

        if cmd == 0x01:  # Name Get
            if target is None:
                return None
            name = self._name.get(target, b"")
            return self._build_sysex(bytes([midi_ch_byte, 0x02, ch_note]) + name)

        if cmd == 0x03:  # Name Set
            if target is not None:
                self._name[target] = bytes(rest)
            return None

        if cmd == 0x04:  # Colour Get
            if target is None:
                return None
            col = self._colour.get(target, 0)
            return self._build_sysex(bytes([midi_ch_byte, 0x05, ch_note, col & 0x7F]))

        if cmd == 0x06:  # Colour Set
            if target is not None and rest:
                self._colour[target] = rest[0] & 0x7F
            return None

        if cmd == 0x0D:  # Send Level Set
            #   <SrcCH> <SndN> <SndCH> <LV>
            if len(rest) < 3:
                return None
            snd_n = rest[0] & 0x0F
            snd_ch = rest[1] & 0x7F
            lv = rest[2] & 0x7F
            tgt = self._resolve(snd_n, snd_ch)
            if target is not None and tgt is not None:
                self._send_level[(target, tgt)] = lv
            # No echo documented for send levels — keep silent. The
            # driver doesn't track them as state either.
            return None

        return None

    # ── Wire builders ───────────────────────────────────────────────────

    @staticmethod
    def _build_nrpn(ch: int, ch_note: int, param_id: int, value: int) -> bytes:
        b = 0xB0 | (ch & 0x0F)
        return bytes([
            b, CC_NRPN_MSB, ch_note & 0x7F,
            b, CC_NRPN_LSB, param_id & 0x7F,
            b, CC_DATA_ENTRY_MSB, value & 0x7F,
        ])

    @staticmethod
    def _build_sysex(body: bytes) -> bytes:
        return bytes([0xF0]) + SYSEX_HEADER + body + bytes([0xF7])

    # ── Push API ────────────────────────────────────────────────────────

    async def _broadcast_scene(self) -> None:
        idx = self._current_scene - 1
        bank, program = idx // 128, idx % 128
        ch = self._midi_ch_byte(0)
        msg = bytes([0xB0 | ch, CC_BANK_SELECT_MSB, bank, 0xC0 | ch, program])
        await self.push(msg)

    async def push_mute(self, ctype: str, n: int, on: bool) -> None:
        """Test hook — simulate a console-side mute change."""
        offset, base_note, count = CHANNEL_TYPES[ctype]
        if n < 1 or n > count:
            raise ValueError(f"{ctype} {n} out of range")
        ch = self._midi_ch_byte(offset)
        note = (base_note + (n - 1)) & 0x7F
        self._mute[(ctype, n)] = on
        msg_status = 0x90 | ch
        msg = bytes([msg_status, note, 0x7F if on else 0x3F, msg_status, note, 0x00])
        await self.push(msg)

    async def push_fader(self, ctype: str, n: int, lv: int) -> None:
        """Test hook — simulate a console-side fader move."""
        offset, base_note, count = CHANNEL_TYPES[ctype]
        if n < 1 or n > count:
            raise ValueError(f"{ctype} {n} out of range")
        ch = self._midi_ch_byte(offset)
        note = (base_note + (n - 1)) & 0x7F
        lv = max(0, min(0x7F, lv))
        self._fader[(ctype, n)] = lv
        await self.push(self._build_nrpn(ch, note, NRPN_PARAM_FADER, lv))


SIMULATOR_CLASS = AllenHeathAvantisSimulator
