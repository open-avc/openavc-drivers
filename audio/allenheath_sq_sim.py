"""
Allen & Heath SQ — Simulator.

Implements the SQ MIDI-over-TCP protocol on port 51325 well enough to
exercise the driver:

  - Raw binary TCP (no delimiter).
  - MIDI 1.0 byte stream with NRPN (CC 63 / 62 / 06 / 26) for mutes,
    levels, pans and assignments.
  - Bank Select + Program Change for scene recall.
  - Note On/Off for SoftKey triggers (no state mutation — SoftKeys
    are momentary triggers on real hardware).
  - "Get" requests (CC 60 = 0x7F) echo back the current value as a
    full 4-message NRPN absolute set on the same MIDI channel.
  - Console-side spontaneous pushes can be triggered through the
    sim REST API (set_state) — value mutations broadcast as NRPN
    sequences to all connected clients, so the driver's push parser
    can be tested end-to-end.

Driver side: ``audio/allenheath_sq.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)


VALUE_MAX = 0x3FFF


class AllenHeathSQSimulator(TCPSimulator):
    SIMULATOR_INFO = {
        "id": "allenheath_sq",
        "name": "Allen & Heath SQ Mixer (sim)",
        "type": "tcp",
        "default_port": 51325,
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # Raw binary mode — MIDI is not line-delimited.
        self._delimiter = None
        self._line_mode = False

        cfg = self.config or {}
        self._midi_channel = max(0, min(15, int(cfg.get("midi_channel", 1)) - 1))

        # Parameter store: keyed by (msb, lsb), value = 14-bit int.
        # Mutes use 0/1 in the value (vc always 0, vf is the on/off bit).
        self._params: dict[tuple[int, int], int] = {}
        self._current_scene = 1

        # NRPN aggregator per channel (mirror of the driver).
        self._nrpn: dict[int, dict[str, int]] = {
            ch: {"msb": 0, "lsb": 0, "vc": 0} for ch in range(16)
        }
        self._last_bank: dict[int, int] = {ch: 0 for ch in range(16)}
        self._running_status = 0

    # ── REST/exposed state ──────────────────────────────────────────────

    def get_state(self) -> dict[str, Any]:
        return {
            "current_scene": self._current_scene,
            "param_count": len(self._params),
            "midi_channel": self._midi_channel + 1,
        }

    async def set_state_value(self, key: str, value: Any) -> dict[str, Any]:
        """Hook for the simulator UI to mutate a parameter and have the
        sim push the change to all connected clients (mimics a console-
        side fader move).

        Currently exposes only `current_scene` directly — full per-
        parameter REST control is not needed for driver verification.
        """
        if key == "current_scene":
            self._current_scene = int(value)
            await self._broadcast_scene()
            return {"ok": True}
        return {"ok": False, "error": f"unknown key: {key}"}

    # ── Frame handler (called by base for each chunk) ───────────────────

    def handle_command(self, data: bytes) -> bytes | None:
        """Synchronously parse + respond. Returns concatenated outbound bytes."""
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
                # Note On/Off (SoftKeys) and Aftertouch — no-op state-wise.
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
            return self._on_absolute(ch, nrpn["msb"], nrpn["lsb"], nrpn["vc"], value)
        if controller == 0x60:
            if value == 0x7F:
                # Get request — reply with the current value.
                return self._reply_get(ch, nrpn["msb"], nrpn["lsb"])
            if value == 0x00:
                # Increment / toggle — flip mute or +1 dB on level.
                return self._on_increment(ch, nrpn["msb"], nrpn["lsb"])
        if controller == 0x61 and value == 0x00:
            return self._on_decrement(ch, nrpn["msb"], nrpn["lsb"])
        return None

    def _on_absolute(self, ch: int, msb: int, lsb: int, vc: int, vf: int) -> bytes | None:
        # Mute & assign use VC=0, VF=0/1 — store 0 or 1.
        # Levels & pans use 14-bit (VC<<7)|VF — store full value.
        if vc == 0 and vf in (0, 1):
            self._params[(msb, lsb)] = vf
        else:
            self._params[(msb, lsb)] = ((vc & 0x7F) << 7) | (vf & 0x7F)
        # Echo so the driver's push parser sees the change.
        return self._build_nrpn(ch, msb, lsb, vc, vf)

    def _on_increment(self, ch: int, msb: int, lsb: int) -> bytes | None:
        cur = self._params.get((msb, lsb), 0)
        # If current looks like a mute (0/1), toggle it.
        if cur in (0, 1) and self._is_mute_param(msb, lsb):
            new = 0 if cur else 1
            self._params[(msb, lsb)] = new
            return self._build_nrpn(ch, msb, lsb, 0, new)
        # Otherwise treat as +1 step on a 14-bit value (rough — real
        # console steps in dB). 256 ≈ ~1 fader-position percent, fine
        # enough for sim verification.
        new = min(VALUE_MAX, cur + 256)
        self._params[(msb, lsb)] = new
        return self._build_nrpn(ch, msb, lsb, (new >> 7) & 0x7F, new & 0x7F)

    def _on_decrement(self, ch: int, msb: int, lsb: int) -> bytes | None:
        cur = self._params.get((msb, lsb), 0)
        if cur in (0, 1) and self._is_mute_param(msb, lsb):
            new = 0 if cur else 1
            self._params[(msb, lsb)] = new
            return self._build_nrpn(ch, msb, lsb, 0, new)
        new = max(0, cur - 256)
        self._params[(msb, lsb)] = new
        return self._build_nrpn(ch, msb, lsb, (new >> 7) & 0x7F, new & 0x7F)

    def _reply_get(self, ch: int, msb: int, lsb: int) -> bytes:
        cur = self._params.get((msb, lsb))
        if cur is None:
            # Untouched parameter: pans/balances rest at center on a real
            # console (CTR = 3F 7F); everything else starts at 0.
            cur = 0x1FFF if self._is_pan_param(msb, lsb) else 0
        if self._is_mute_param(msb, lsb):
            return self._build_nrpn(ch, msb, lsb, 0, cur & 0x01)
        return self._build_nrpn(ch, msb, lsb, (cur >> 7) & 0x7F, cur & 0x7F)

    @staticmethod
    def _is_mute_param(msb: int, lsb: int) -> bool:
        # Mute parameters are MSB 00 (channels 0x00..0x57), 02 (DCAs),
        # 04 (Mute Groups) — and assigns sit at MSB 0x60+. Levels at
        # 0x40-0x4F, pans 0x50-0x5F.
        return msb in (0x00, 0x02, 0x04) or (0x60 <= msb <= 0x6F)

    @staticmethod
    def _is_pan_param(msb: int, lsb: int) -> bool:
        # Pan/balance parameter numbers all live at MSB 0x50-0x5F.
        return 0x50 <= msb <= 0x5F

    def _handle_program_change(self, ch: int, program: int) -> None:
        if ch != self._midi_channel:
            return
        bank = self._last_bank.get(ch, 0)
        scene = bank * 128 + program + 1
        if 1 <= scene <= 300:
            self._current_scene = scene

    # ── Push API ────────────────────────────────────────────────────────

    async def _broadcast_scene(self) -> None:
        """Push the current scene as a Bank Select + Program Change to all clients."""
        idx = self._current_scene - 1
        bank, program = idx // 128, idx % 128
        ch = self._midi_channel
        msg = bytes([0xB0 | ch, 0x00, bank, 0xC0 | ch, program])
        await self.push(msg)

    async def push_param(self, msb: int, lsb: int, vc: int, vf: int) -> None:
        """Test hook: push an arbitrary parameter change as if a console
        fader/mute had moved. Stores the value and broadcasts NRPN.
        """
        if vc == 0 and vf in (0, 1):
            self._params[(msb, lsb)] = vf
        else:
            self._params[(msb, lsb)] = ((vc & 0x7F) << 7) | (vf & 0x7F)
        await self.push(self._build_nrpn(self._midi_channel, msb, lsb, vc, vf))

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _build_nrpn(ch: int, msb: int, lsb: int, vc: int, vf: int) -> bytes:
        b = 0xB0 | (ch & 0x0F)
        return bytes([b, 0x63, msb & 0x7F, b, 0x62, lsb & 0x7F,
                      b, 0x06, vc & 0x7F, b, 0x26, vf & 0x7F])


SIMULATOR_CLASS = AllenHeathSQSimulator
