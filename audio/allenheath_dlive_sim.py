"""
Allen & Heath dLive — Simulator.

Implements the dLive MIDI-over-TCP protocol on plain-TCP port 51325
well enough to exercise the driver:

  - Raw binary TCP (no delimiter), MIDI 1.0 wire format, running status.
  - 5-MIDI-channel range (base..base+4) addressing for 128 inputs, 62
    mono / 31 stereo groups, 62 mono / 31 stereo aux, 62 mono / 31
    stereo matrix, 16 mono / 16 stereo FX sends, 16 FX returns, 6
    mains, 24 DCAs, 8 mute groups (at CH 4E-55), 8 stereo UFX sends, 8
    stereo UFX returns.
  - Note-On pair mutes (9N CH 7F / 9N CH 3F + trailing 9N CH 00).
  - NRPN parameter ID 17 (fader), 18 (main assign), 30 (HPF freq), 31
    (HPF on/off), 40 (DCA / mute-group assign).
  - SysEx Channel Name Get / Set (cmd 01 / 03), Channel Colour Get /
    Set (cmd 04 / 06), Pad Get / Reply / Set (cmd 07 / 08 / 09), 48V
    Get / Reply / Set (cmd 0A / 0B / 0C), Send Level Set (cmd 0D),
    Send Assign Set (cmd 0E), MMC transport.
  - The Get-status family (cmd 05): mute Get (sub 09) answers with the
    Note-On mute message, fader Get (sub 0B param 17) with the NRPN
    fader message, and preamp-gain Get (sub 0B param 19) with the
    Pitchbend EN MP GV message — the shapes the driver's connect sweep
    and liveness probe rely on.
  - Pitchbend (EN MP GV) for socket preamp gain across 128 sockets.
  - Bank Select + Program Change for scene recall (1..500, 4 banks)
    AND cue list recall (1..2000, 16 banks). The simulator updates
    both `current_scene` and `current_cue` from the same incoming
    Bank+PC stream — real hardware sends this from MixRack (scene) and
    Surface (cue) separately; the sim collapses both for test
    purposes.
  - UFX Global Key / Scale (CC 0C / 0D).

When the driver Sets a fader or mute, the simulator echoes the same
shape back so the driver's push parser updates state. ``push_*`` test
hooks let a unit test simulate console-side moves end-to-end.

Driver side: ``audio/allenheath_dlive.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)


# ── Channel address tables (mirror of the driver's CHANNEL_TYPES) ────────────

CHANNEL_TYPES: dict[str, tuple[int, int, int]] = {
    "input":          (0, 0x00, 128),
    "mono_group":     (1, 0x00, 62),
    "stereo_group":   (1, 0x40, 31),
    "mono_aux":       (2, 0x00, 62),
    "stereo_aux":     (2, 0x40, 31),
    "mono_matrix":    (3, 0x00, 62),
    "stereo_matrix":  (3, 0x40, 31),
    "mono_fx_send":   (4, 0x00, 16),
    "stereo_fx_send": (4, 0x10, 16),
    "fx_return":      (4, 0x20, 16),
    "main":           (4, 0x30, 6),
    "dca":            (4, 0x36, 24),
    "mute_group":     (4, 0x4E, 8),
    "ufx_send":       (4, 0x56, 8),
    "ufx_return":     (4, 0x5E, 8),
}

NRPN_PARAM_FADER = 0x17
NRPN_PARAM_MAIN_ASSIGN = 0x18
NRPN_PARAM_PREAMP_GAIN = 0x19
NRPN_PARAM_HPF_FREQ = 0x30
NRPN_PARAM_HPF_ONOFF = 0x31
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


class AllenHeathDLiveSimulator(TCPSimulator):
    SIMULATOR_INFO = {
        "id": "allenheath_dlive",
        "name": "Allen & Heath dLive Mixer (sim)",
        "type": "tcp",
        "default_port": 51325,
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._delimiter = None
        self._line_mode = False

        cfg = self.config or {}
        self._base_midi = max(0, min(11, int(cfg.get("base_midi_channel", 1)) - 1))

        self._mute: dict[tuple[str, int], bool] = {}
        self._fader: dict[tuple[str, int], int] = {}
        self._name: dict[tuple[str, int], bytes] = {}
        self._colour: dict[tuple[str, int], int] = {}
        self._main_assign: dict[tuple[str, int], bool] = {}
        self._dca_assign: dict[tuple[tuple[str, int], int], bool] = {}
        self._mute_group_assign: dict[tuple[tuple[str, int], int], bool] = {}
        self._send_level: dict[tuple[tuple[str, int], tuple[str, int]], int] = {}
        self._send_assign: dict[tuple[tuple[str, int], tuple[str, int]], bool] = {}
        self._hpf_freq: dict[tuple[str, int], int] = {}
        self._hpf_on: dict[tuple[str, int], bool] = {}
        # Preamp per-socket (MP 0..127).
        self._gain: dict[int, int] = {}
        self._pad: dict[int, bool] = {}
        self._48v: dict[int, bool] = {}

        self._current_scene = 1
        self._current_cue = 0
        self._ufx_key = 0
        self._ufx_scale = 0
        self._mmc_last = None

        self._nrpn: dict[int, dict[str, int]] = {
            ch: {"msb": 0, "lsb": 0, "vc": 0} for ch in range(16)
        }
        self._last_bank: dict[int, int] = {ch: 0 for ch in range(16)}
        self._running_status = 0

    # ── REST/UI exposed state ───────────────────────────────────────────

    def get_state(self) -> dict[str, Any]:
        return {
            "current_scene": self._current_scene,
            "current_cue": self._current_cue,
            "base_midi_channel": self._base_midi + 1,
            "mute_count": sum(1 for v in self._mute.values() if v),
            "fader_count": len(self._fader),
            "send_count": len(self._send_level),
            "send_assign_count": sum(1 for v in self._send_assign.values() if v),
            "hpf_on_count": sum(1 for v in self._hpf_on.values() if v),
            "preamp_gain_count": len(self._gain),
            "ufx_key": self._ufx_key,
            "ufx_scale": self._ufx_scale,
            "mmc_last": self._mmc_last,
        }

    async def set_state_value(self, key: str, value: Any) -> dict[str, Any]:
        if key == "current_scene":
            self._current_scene = int(value)
            await self._broadcast_scene()
            return {"ok": True}
        if key == "current_cue":
            self._current_cue = int(value)
            await self._broadcast_cue()
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
                elif high == 0xE0:
                    self._handle_pitchbend(ch, d1, d2)
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
            return None
        target = self._resolve(ch, note)
        if target is None:
            return None
        on = velocity >= 0x40
        self._mute[target] = on
        n = 0x90 | (ch & 0x0F)
        return bytes([n, note & 0x7F, 0x7F if on else 0x3F, n, note & 0x7F, 0x00])

    def _handle_cc(self, ch: int, controller: int, value: int) -> bytes | None:
        if controller == CC_BANK_SELECT_MSB:
            self._last_bank[ch] = value
            return None

        if controller == CC_UFX_GLOBAL_KEY and ch == self._midi_ch_byte(0):
            self._ufx_key = value & 0x0F
            return None

        if controller == CC_UFX_GLOBAL_SCALE and ch == self._midi_ch_byte(0):
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
                return None
            self._fader[target] = value & 0x7F
            return self._build_nrpn(ch, ch_note, NRPN_PARAM_FADER, value & 0x7F)

        if param_id == NRPN_PARAM_MAIN_ASSIGN:
            on = value >= 0x40
            self._main_assign[target] = on
            echo_value = 0x7F if on else 0x3F
            return self._build_nrpn(ch, ch_note, NRPN_PARAM_MAIN_ASSIGN, echo_value)

        if param_id == NRPN_PARAM_BUS_ASSIGN:
            # value 40..57 = DCA 1..24 ON, 00..17 = OFF
            # value 58..5F = Mute Group 1..8 ON, 18..1F = OFF
            if 0x40 <= value <= 0x57:
                self._dca_assign[(target, (value - 0x40) + 1)] = True
            elif 0x00 <= value <= 0x17:
                self._dca_assign[(target, (value - 0x00) + 1)] = False
            elif 0x58 <= value <= 0x5F:
                self._mute_group_assign[(target, (value - 0x58) + 1)] = True
            elif 0x18 <= value <= 0x1F:
                self._mute_group_assign[(target, (value - 0x18) + 1)] = False
            return self._build_nrpn(ch, ch_note, NRPN_PARAM_BUS_ASSIGN, value & 0x7F)

        if param_id == NRPN_PARAM_HPF_FREQ:
            self._hpf_freq[target] = value & 0x7F
            return self._build_nrpn(ch, ch_note, NRPN_PARAM_HPF_FREQ, value & 0x7F)

        if param_id == NRPN_PARAM_HPF_ONOFF:
            on = value >= 0x40
            self._hpf_on[target] = on
            return self._build_nrpn(ch, ch_note, NRPN_PARAM_HPF_ONOFF, 0x7F if on else 0x00)

        return None

    def _handle_pitchbend(self, ch: int, lsb: int, msb: int) -> None:
        # dLive preamp gain is a single-byte value in the MP byte
        # (Pitchbend EN MP GV) where the message is EN (status) MP GV
        # — i.e. d1 = MP, d2 = GV. Standard MIDI pitchbend would
        # interpret these as 14-bit LSB+MSB; dLive overrides this.
        if ch != self._midi_ch_byte(0):
            return
        mp = lsb & 0x7F
        gv = msb & 0x7F
        self._gain[mp] = gv

    def _handle_program_change(self, ch: int, program: int) -> bytes | None:
        if ch != self._midi_ch_byte(0):
            return None
        bank = self._last_bank.get(ch, 0)
        echoed = False
        if 0 <= bank <= 3:
            scene = bank * 128 + program + 1
            if 1 <= scene <= 500:
                self._current_scene = scene
                echoed = True
        cue = bank * 128 + program + 1
        if 1 <= cue <= 2000:
            self._current_cue = cue
            echoed = True
        if echoed:
            b = 0xB0 | (ch & 0x0F)
            c = 0xC0 | (ch & 0x0F)
            return bytes([b, CC_BANK_SELECT_MSB, bank, c, program])
        return None

    def _handle_sysex(self, message: bytes) -> bytes | None:
        if len(message) < 2 or message[0] != 0xF0 or message[-1] != 0xF7:
            return None
        body = message[1:-1]
        # MMC (F0 7F 7F 06 TC F7) doesn't use the vendor header.
        if body[:3] == bytes([0x7F, 0x7F, 0x06]):
            if len(body) >= 4:
                self._mmc_last = body[3]
            return None
        if not body.startswith(SYSEX_HEADER):
            return None
        body = body[len(SYSEX_HEADER):]
        if len(body) < 2:
            return None

        midi_ch_byte = body[0] & 0x0F
        cmd = body[1]
        rest = body[2:]

        # Per-channel SysEx commands (cmd 01..06): rest = CH [ + payload ]
        if cmd in (0x01, 0x03, 0x04, 0x06):
            if len(rest) < 1:
                return None
            ch_note = rest[0] & 0x7F
            payload = rest[1:]
            target = self._resolve(midi_ch_byte, ch_note)
            if cmd == 0x01:  # Name Get
                if target is None:
                    return None
                name = self._name.get(target, b"")
                return self._build_sysex(bytes([midi_ch_byte, 0x02, ch_note]) + name)
            if cmd == 0x03:  # Name Set
                if target is not None:
                    self._name[target] = bytes(payload)
                return None
            if cmd == 0x04:  # Colour Get
                if target is None:
                    return None
                col = self._colour.get(target, 0)
                return self._build_sysex(bytes([midi_ch_byte, 0x05, ch_note, col & 0x7F]))
            if cmd == 0x06:  # Colour Set
                if target is not None and payload:
                    self._colour[target] = payload[0] & 0x7F
                return None

        # Per-socket SysEx commands (cmd 07..0C): rest = MP [ + payload ]
        if cmd in (0x07, 0x09, 0x0A, 0x0C):
            if len(rest) < 1:
                return None
            mp = rest[0] & 0x7F
            payload = rest[1:]
            if cmd == 0x07:  # Pad Get
                pad = self._pad.get(mp, False)
                return self._build_sysex(bytes([
                    midi_ch_byte, 0x08, mp, 0x7F if pad else 0x00,
                ]))
            if cmd == 0x09:  # Pad Set
                if payload:
                    on = payload[0] >= 0x40
                    self._pad[mp] = on
                return None
            if cmd == 0x0A:  # 48V Get
                v48 = self._48v.get(mp, False)
                return self._build_sysex(bytes([
                    midi_ch_byte, 0x0B, mp, 0x7F if v48 else 0x00,
                ]))
            if cmd == 0x0C:  # 48V Set
                if payload:
                    on = payload[0] >= 0x40
                    self._48v[mp] = on
                return None

        if cmd == 0x0D:  # Send Level Set
            #   <SrcCH> <SndN> <SndCH> <LV>
            if len(rest) < 4:
                return None
            src_ch = rest[0] & 0x7F
            snd_n = rest[1] & 0x0F
            snd_ch = rest[2] & 0x7F
            lv = rest[3] & 0x7F
            src = self._resolve(midi_ch_byte, src_ch)
            tgt = self._resolve(snd_n, snd_ch)
            if src is not None and tgt is not None:
                self._send_level[(src, tgt)] = lv
            return None

        if cmd == 0x0E:  # Send Assign Set
            if len(rest) < 4:
                return None
            src_ch = rest[0] & 0x7F
            snd_n = rest[1] & 0x0F
            snd_ch = rest[2] & 0x7F
            v = rest[3] & 0x7F
            src = self._resolve(midi_ch_byte, src_ch)
            tgt = self._resolve(snd_n, snd_ch)
            if src is not None and tgt is not None:
                self._send_assign[(src, tgt)] = v >= 0x40
            return None

        # Get-status family (cmd 0x05): the driver's connect sweep and
        # liveness probe issue these; reply with the current value in the
        # same shape a real console uses.
        if cmd == 0x05 and rest:
            sub = rest[0]
            if sub == 0x09 and len(rest) >= 2:
                # Get Mute Status — reply is the Note-On mute message.
                ch_note = rest[1] & 0x7F
                target = self._resolve(midi_ch_byte, ch_note)
                if target is None:
                    return None
                on = self._mute.get(target, False)
                n = 0x90 | (midi_ch_byte & 0x0F)
                return bytes([n, ch_note, 0x7F if on else 0x3F,
                              n, ch_note, 0x00])
            if sub == 0x0B and len(rest) >= 3:
                param = rest[1] & 0x7F
                ch_note = rest[2] & 0x7F
                if param == NRPN_PARAM_FADER:
                    # Reply is the NRPN fader message. Untouched faders
                    # read back 0 (bottom of travel).
                    target = self._resolve(midi_ch_byte, ch_note)
                    if target is None:
                        return None
                    lv = self._fader.get(target, 0)
                    return self._build_nrpn(midi_ch_byte, ch_note,
                                            NRPN_PARAM_FADER, lv)
                if param == NRPN_PARAM_PREAMP_GAIN:
                    # Get Socket Preamp Gain — reply is the Pitchbend
                    # EN MP GV message on the same channel.
                    mp = ch_note
                    gv = self._gain.get(mp, 0)
                    return bytes([0xE0 | (midi_ch_byte & 0x0F), mp, gv])
                # Main assign / HPF / PEQ Gets would answer on real
                # hardware; the driver doesn't issue them.
                return None
            # Send-level / send-assign Gets (sub 0x0F) — not issued.
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

    # ── Push API (test hooks) ───────────────────────────────────────────

    async def _broadcast_scene(self) -> None:
        idx = self._current_scene - 1
        bank, program = idx // 128, idx % 128
        ch = self._midi_ch_byte(0)
        msg = bytes([0xB0 | ch, CC_BANK_SELECT_MSB, bank, 0xC0 | ch, program])
        await self.push(msg)

    async def _broadcast_cue(self) -> None:
        idx = self._current_cue - 1
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

    async def push_gain(self, socket: int, gv: int) -> None:
        """Test hook — simulate a console-side preamp gain move
        (Pitchbend EN MP GV on the base channel)."""
        mp = (socket - 1) & 0x7F
        gv = max(0, min(0x7F, gv))
        self._gain[mp] = gv
        await self.push(bytes([0xE0 | self._midi_ch_byte(0), mp, gv]))


SIMULATOR_CLASS = AllenHeathDLiveSimulator
