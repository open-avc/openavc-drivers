"""
Shure Networked Audio — Simulator.

Implements the Shure Device Control Strings (DCS) protocol from the per-model
command-strings references (MXA910 / MXA920 / P300 / SCM820 / ANI). Messages
are ``< ... >`` framed over TCP port 2202; ``>`` is the delimiter.

Models a configurable number of channels, each with a mute, a digital gain
(0-1400 wire units), and a name, plus device-level mute, LED brightness,
firmware, and device name. Answers GET with REP, applies SET and echoes the
resulting REP (as real hardware does — the REP a controller sees for its own
change is the same REP the device pushes to other controllers). An index-0
GET fans out one REP per channel. When metering is enabled via
``SET METER_RATE``, replies with an immediate SAMPLE row.
"""

from __future__ import annotations

import logging

from simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)


DEFAULT_CHANNEL_COUNT = 8

# Wire gain 0-1400 = -110..+30 dB; 1100 = 0 dB (unity).
_GAIN_WIRE_DEFAULT = 1100
_GAIN_WIRE_MIN = 0
_GAIN_WIRE_MAX = 1400


class ShureNetworkSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "shure_network",
        "name": "Shure Networked Audio Simulator",
        "category": "audio",
        "transport": "tcp",
        "default_port": 2202,
        "delimiter": ">",
        "initial_state": {
            "device_name": "MXA920-SIM",
            "mute": False,
            "led_brightness": 2,
            "firmware": "4.6.11",
        },
        "controls": [
            {"type": "toggle", "key": "mute", "label": "Device Mute"},
            {"type": "indicator", "key": "led_brightness", "label": "LED Brightness"},
            {"type": "indicator", "key": "device_name", "label": "Device Name"},
            {"type": "indicator", "key": "firmware", "label": "Firmware"},
        ],
        "delays": {"command_response": 0.01},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        cfg = self.config
        self._count = int(cfg.get("channel_count", DEFAULT_CHANNEL_COUNT))
        self._device_name = str(
            self.state.get("device_name", "MXA920-SIM"))
        self._firmware = str(self.state.get("firmware", "4.6.11"))
        self._mute = bool(self.state.get("mute", False))
        self._brightness = int(self.state.get("led_brightness", 2))
        self._meter_ms = 0
        # Per-channel values, 1-based.
        self._ch_mute: dict[int, bool] = {n: False for n in range(1, self._count + 1)}
        self._ch_gain: dict[int, int] = {
            n: _GAIN_WIRE_DEFAULT for n in range(1, self._count + 1)}
        self._ch_name: dict[int, str] = {
            n: f"Channel {n}" for n in range(1, self._count + 1)}

    # ── Per-frame handling ──

    def handle_command(self, data: bytes) -> bytes | None:
        text = data.decode("ascii", errors="replace").strip()
        # Strip framing: leading "<", trailing delimiter ">".
        text = text.lstrip("<").rstrip(">").strip()
        if not text:
            return None

        parts = text.split()
        verb = parts[0].upper() if parts else ""

        if verb == "GET":
            return self._handle_get(parts)
        if verb == "SET":
            return self._handle_set(parts)
        return None

    # ── GET ──

    def _handle_get(self, parts: list[str]) -> bytes | None:
        # < GET PROP >  or  < GET index PROP >
        if len(parts) >= 3 and parts[1].isdigit():
            idx = int(parts[1])
            prop = parts[2].upper()
            return self._get_channel(idx, prop)
        if len(parts) >= 2:
            prop = parts[1].upper()
            return self._get_device(prop)
        return None

    def _get_device(self, prop: str) -> bytes | None:
        if prop == "DEVICE_ID":
            return self._frame(f"REP DEVICE_ID {{{self._device_name}}}")
        if prop == "DEVICE_AUDIO_MUTE":
            return self._frame(
                f"REP DEVICE_AUDIO_MUTE {'ON' if self._mute else 'OFF'}")
        if prop == "FW_VER":
            return self._frame(f"REP FW_VER {{{self._firmware}}}")
        if prop == "LED_BRIGHTNESS":
            return self._frame(f"REP LED_BRIGHTNESS {self._brightness}")
        if prop == "ALL":
            out = (
                self._frame(f"REP DEVICE_ID {{{self._device_name}}}")
                + self._frame(
                    f"REP DEVICE_AUDIO_MUTE {'ON' if self._mute else 'OFF'}")
                + self._frame(f"REP FW_VER {{{self._firmware}}}")
                + self._frame(f"REP LED_BRIGHTNESS {self._brightness}")
                + self._fanout("CHAN_NAME")
                + self._fanout("AUDIO_MUTE")
            )
            return out
        return self._frame("REP ERR")

    def _get_channel(self, idx: int, prop: str) -> bytes | None:
        if idx == 0:
            # Broadcast query fans out one REP per configured channel.
            return self._fanout(prop) or self._frame("REP ERR")
        if not (1 <= idx <= self._count):
            return self._frame("REP ERR")
        if prop == "AUDIO_MUTE":
            return self._ch_mute_rep(idx)
        if prop == "CHAN_NAME":
            return self._ch_name_rep(idx)
        if prop == "AUDIO_GAIN_HI_RES":
            return self._ch_gain_rep(idx)
        return self._frame("REP ERR")

    def _fanout(self, prop: str) -> bytes:
        prop = prop.upper()
        out = b""
        for n in range(1, self._count + 1):
            if prop == "CHAN_NAME":
                out += self._ch_name_rep(n)
            elif prop == "AUDIO_MUTE":
                out += self._ch_mute_rep(n)
            elif prop == "AUDIO_GAIN_HI_RES":
                out += self._ch_gain_rep(n)
        return out

    # ── SET ──

    def _handle_set(self, parts: list[str]) -> bytes | None:
        # < SET PROP val >  or  < SET index PROP val... >
        if len(parts) >= 3 and parts[1].isdigit():
            return self._set_channel(int(parts[1]), parts[2].upper(), parts[3:])
        if len(parts) >= 2:
            return self._set_device(parts[1].upper(), parts[2:])
        return None

    def _set_device(self, prop: str, args: list[str]) -> bytes | None:
        if prop == "DEVICE_AUDIO_MUTE":
            cmd = args[0].upper() if args else ""
            if cmd == "ON":
                self._mute = True
            elif cmd == "OFF":
                self._mute = False
            elif cmd == "TOGGLE":
                self._mute = not self._mute
            else:
                return self._frame("REP ERR")
            self.set_state("mute", self._mute)
            return self._frame(
                f"REP DEVICE_AUDIO_MUTE {'ON' if self._mute else 'OFF'}")
        if prop == "LED_BRIGHTNESS":
            try:
                self._brightness = int(args[0])
            except (IndexError, ValueError):
                return self._frame("REP ERR")
            self.set_state("led_brightness", self._brightness)
            return self._frame(f"REP LED_BRIGHTNESS {self._brightness}")
        if prop == "FLASH":
            st = args[0].upper() if args else ""
            return self._frame(f"REP FLASH {st}")
        if prop == "PRESET":
            try:
                pr = int(args[0])
            except (IndexError, ValueError):
                return self._frame("REP ERR")
            return self._frame(f"REP PRESET {pr}")
        if prop.startswith("LED_STATE_"):
            which = prop[len("LED_STATE_"):]
            st = args[0].upper() if args else ""
            return self._frame(f"REP LED_STATE_{which} {st}")
        if prop.startswith("LED_COLOR_"):
            which = prop[len("LED_COLOR_"):]
            color = args[0].upper() if args else ""
            return self._frame(f"REP LED_COLOR_{which} {color}")
        if prop in ("METER_RATE", "METER_RATE_IN"):
            try:
                self._meter_ms = int(args[0])
            except (IndexError, ValueError):
                return self._frame("REP ERR")
            out = self._frame(f"REP {prop} {self._meter_ms}")
            if self._meter_ms > 0:
                out += self._sample_frame()
            return out
        if prop == "METER_RATE_OUT":
            # Present on the P300; acknowledged but this sim doesn't stream
            # output meters.
            try:
                return self._frame(f"REP METER_RATE_OUT {int(args[0])}")
            except (IndexError, ValueError):
                return self._frame("REP ERR")
        return self._frame("REP ERR")

    def _set_channel(self, idx: int, prop: str, args: list[str]) -> bytes | None:
        if not (1 <= idx <= self._count):
            return self._frame("REP ERR")
        if prop == "AUDIO_MUTE":
            cmd = args[0].upper() if args else ""
            if cmd == "ON":
                self._ch_mute[idx] = True
            elif cmd == "OFF":
                self._ch_mute[idx] = False
            elif cmd == "TOGGLE":
                self._ch_mute[idx] = not self._ch_mute[idx]
            else:
                return self._frame("REP ERR")
            return self._ch_mute_rep(idx)
        if prop == "AUDIO_GAIN_HI_RES":
            wire = self._apply_gain(idx, args)
            if wire is None:
                return self._frame("REP ERR")
            return self._ch_gain_rep(idx)
        return self._frame("REP ERR")

    def _apply_gain(self, idx: int, args: list[str]) -> int | None:
        if not args:
            return None
        first = args[0].lower()
        try:
            if first in ("inc", "dec") and len(args) >= 2:
                step = int(args[1])
                cur = self._ch_gain[idx]
                new = cur + step if first == "inc" else cur - step
            else:
                new = int(args[0])
        except ValueError:
            return None
        new = max(_GAIN_WIRE_MIN, min(_GAIN_WIRE_MAX, new))
        self._ch_gain[idx] = new
        return new

    # ── REP builders ──

    def _ch_mute_rep(self, idx: int) -> bytes:
        return self._frame(
            f"REP {idx:02d} AUDIO_MUTE {'ON' if self._ch_mute[idx] else 'OFF'}")

    def _ch_name_rep(self, idx: int) -> bytes:
        # 31-char space-padded, brace-wrapped — as real hardware reports.
        return self._frame(f"REP {idx:02d} CHAN_NAME {{{self._ch_name[idx]:<31}}}")

    def _ch_gain_rep(self, idx: int) -> bytes:
        return self._frame(f"REP {idx:02d} AUDIO_GAIN_HI_RES {self._ch_gain[idx]}")

    def _sample_frame(self) -> bytes:
        # One value per channel (000-060). Deterministic ramp so tests can
        # assert the positional fan-out into channel meter props.
        vals = " ".join(f"{min(60, 10 + n):03d}" for n in range(1, self._count + 1))
        return self._frame(f"SAMPLE {vals}")

    def _frame(self, body: str) -> bytes:
        return f"< {body} >".encode("ascii")
