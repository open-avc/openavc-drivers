"""
Allen & Heath Qu — Simulator.

Implements enough of the Qu MIDI-over-TCP protocol (port 51325) to exercise
the driver end to end:

  - Raw binary TCP (no delimiter), MIDI 1.0 byte stream.
  - MIDI Active Sensing: emits FE on connect and every ~2 s so the driver's
    liveness watchdog stays satisfied.
  - All-Call system-state request (SysEx 0x10) -> replies with the configured
    model's BoxID + firmware, pushes a little initial state, then End Sync
    (0x14), so the driver auto-identifies the model and populates children.
  - Get Name (SysEx 0x01) -> Name reply (0x02) so children get real labels.
  - Note On/Off mutes and NRPN fader/pan sets are stored and echoed back, so
    the driver's push parser mirrors console-side changes into child state.
  - Bank Select + Program Change updates the current scene.

Driver side: ``audio/allenheath_qu.py``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

# Vendor SysEx framing (mirror of the driver).
SYSEX_HEAD = bytes([0x00, 0x00, 0x1A, 0x50, 0x11, 0x01, 0x00])
SX_GET_NAME = 0x01
SX_NAME_REPLY = 0x02
SX_SET_NAME = 0x03
SX_SYSTEM_STATE_REQ = 0x10
SX_SYSTEM_STATE_REPLY = 0x11
SX_END_SYNC = 0x14

MODEL_BOX_IDS = {"Qu-16": 1, "Qu-24": 2, "Qu-32": 3, "Qu-Pac": 4, "Qu-SB": 5}

ID_FADER = 0x17
ID_PAN = 0x16
ACTIVE_SENSE = 0xFE
ACTIVE_SENSE_INTERVAL = 2.0


class AllenHeathQuSimulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "allenheath_qu",
        "name": "Allen & Heath Qu Mixer (sim)",
        "transport": "tcp",
        "default_port": 51325,
        "initial_state": {
            "model": "Qu-32",
            "firmware": "1.90",
            "midi_channel": 1,
            "identified": False,
            "current_scene": 0,
            "channel_count": 32,
            "lr_mute": False,
            "lr_fader": 0.75,
            "lr_fader_db": 0.0,
            "lr_pan": 0.0,
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # Raw binary mode — MIDI is not line-delimited.
        self._delimiter = None
        self._line_mode = False

        cfg = self.config or {}
        model = str(cfg.get("model", "Qu-16"))
        self._model = model if model in MODEL_BOX_IDS else "Qu-16"
        self._midi_n = max(0, min(15, int(cfg.get("midi_channel", 1)) - 1))

        # Parameter store: NRPN (ch_select, id) -> value byte; notes -> on/off.
        self._nrpn_store: dict[tuple[int, int], int] = {}
        self._mutes: dict[int, bool] = {}
        self._current_scene = 1

        # MIDI parse state.
        self._nrpn: dict[int, dict[str, int]] = {
            ch: {"ch": 0, "id": 0, "va": 0} for ch in range(16)
        }
        self._running_status = 0
        self._sense_task: asyncio.Task | None = None

    # ── Connection + keep-alive ─────────────────────────────────────────────

    async def on_client_connected(self, client_id: str) -> bytes | None:
        if self._sense_task is None or self._sense_task.done():
            self._sense_task = asyncio.ensure_future(self._active_sense_loop())
        return bytes([ACTIVE_SENSE])

    async def _active_sense_loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(ACTIVE_SENSE_INTERVAL)
                await self.push(bytes([ACTIVE_SENSE]))
        except asyncio.CancelledError:
            pass

    # ── REST/exposed state ──────────────────────────────────────────────────

    def get_state(self) -> dict[str, Any]:
        return {
            "model": self._model,
            "midi_channel": self._midi_n + 1,
            "current_scene": self._current_scene,
            "nrpn_count": len(self._nrpn_store),
            "mutes_on": sum(1 for v in self._mutes.values() if v),
        }

    async def set_state_value(self, key: str, value: Any) -> dict[str, Any]:
        """UI hook to mutate state and push it, mimicking a console move."""
        if key == "current_scene":
            self._current_scene = int(value)
            b = self._status(0xB0)
            c = self._status(0xC0)
            await self.push(bytes([b, 0x00, 0x00, b, 0x20, 0x00,
                                   c, (self._current_scene - 1) & 0x7F]))
            return {"ok": True}
        if key == "model" and str(value) in MODEL_BOX_IDS:
            self._model = str(value)
            return {"ok": True}
        return {"ok": False, "error": f"unknown key: {key}"}

    # ── Framing helpers ─────────────────────────────────────────────────────

    def _status(self, high: int) -> int:
        return high | (self._midi_n & 0x0F)

    def _sysex(self, body: bytes) -> bytes:
        return bytes([0xF0]) + SYSEX_HEAD + bytes([self._midi_n & 0x7F]) + \
            body + bytes([0xF7])

    def _nrpn_msg(self, ch: int, param_id: int, va: int, vx: int) -> bytes:
        b = self._status(0xB0)
        return bytes([b, 0x63, ch & 0x7F, b, 0x62, param_id & 0x7F,
                      b, 0x06, va & 0x7F, b, 0x26, vx & 0x7F])

    def _note_pair(self, note: int, velocity: int) -> bytes:
        n = self._status(0x90)
        return bytes([n, note & 0x7F, velocity & 0x7F, n, note & 0x7F, 0x00])

    # ── Frame handler ───────────────────────────────────────────────────────

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
                    reply = self._handle_note_on(d1, d2)
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
                    self._current_scene = (d1 & 0x7F) + 1
            else:
                i += 1
        self._running_status = running
        return bytes(out) if out else None

    # ── Handlers ────────────────────────────────────────────────────────────

    def _handle_note_on(self, note: int, velocity: int) -> bytes | None:
        if velocity == 0x00:
            return None                                # trailing Note-Off
        on = velocity >= 0x40
        self._mutes[note] = on
        # Echo the mute back so the driver mirrors it.
        return self._note_pair(note, 0x7F if on else 0x3F)

    def _handle_cc(self, ch: int, controller: int, value: int) -> bytes | None:
        if controller in (0x00, 0x20):
            return None                                # bank select
        agg = self._nrpn[ch]
        if controller == 0x63:
            agg["ch"] = value
        elif controller == 0x62:
            agg["id"] = value
        elif controller == 0x06:
            agg["va"] = value
        elif controller == 0x26:
            return self._on_nrpn(agg["ch"], agg["id"], agg["va"], value)
        return None

    def _on_nrpn(self, ch: int, param_id: int, va: int, vx: int) -> bytes | None:
        self._nrpn_store[(ch, param_id)] = va
        # Echo fader / main-pan sets so the driver's push parser sees them.
        if param_id == ID_FADER:
            return self._nrpn_msg(ch, ID_FADER, va, vx)
        if param_id == ID_PAN and vx == 0x07:
            return self._nrpn_msg(ch, ID_PAN, va, vx)
        return None

    def _handle_sysex(self, msg: bytes) -> bytes | None:
        if len(msg) < 11 or bytes(msg[1:8]) != SYSEX_HEAD:
            return None
        kind = msg[9]
        if kind == SX_SYSTEM_STATE_REQ:
            return self._system_state_reply()
        if kind == SX_GET_NAME:
            ch = msg[10]
            return self._sysex(bytes([SX_NAME_REPLY, ch & 0x7F]) +
                               self._name_for(ch).encode("ascii", "replace"))
        return None

    def _system_state_reply(self) -> bytes:
        box_id = MODEL_BOX_IDS.get(self._model, 1)
        out = bytearray()
        out += self._sysex(bytes([SX_SYSTEM_STATE_REPLY, box_id, 0x01, 0x09]))
        # A little initial state so children populate on connect.
        out += self._note_pair(0x20, 0x7F)             # Input 1 mute on
        out += self._nrpn_msg(0x20, ID_FADER, 0x62, 0x07)   # Input 1 at 0 dB
        out += self._nrpn_msg(0x67, ID_FADER, 0x7F, 0x07)   # LR at +10 dB
        out += self._sysex(bytes([SX_END_SYNC]))
        return bytes(out)

    @staticmethod
    def _name_for(ch: int) -> str:
        if 0x20 <= ch <= 0x3F:
            return f"In {ch - 0x20 + 1}"
        if 0x40 <= ch <= 0x42:
            return f"ST{ch - 0x40 + 1}"
        if 0x60 <= ch <= 0x66:
            return f"Mix {ch - 0x60 + 1}"
        if ch == 0x67:
            return "LR"
        if 0x10 <= ch <= 0x13:
            return f"DCA {ch - 0x10 + 1}"
        return f"Ch {ch:02X}"


SIMULATOR_CLASS = AllenHeathQuSimulator
