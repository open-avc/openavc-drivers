"""
Hisense commercial signage display simulator.

Speaks the signage command set from the 55OP90FE User Manual (chapter 8) and
the 54DS User Manual ("Serial Protocol"): ``DD FF`` frames in, ``AB AB``
frames out, the length counting command, monitor ID, data and checksum, and
the checksum the XOR of length, command, ID and data.

A frame may arrive as bytes (RS-232) or as its hex digits in ASCII (the 54DS's
IP Control); the reply goes back the way the frame came. In standby the
display answers Query TV Status and Power, and nothing else. Power On in
standby is answered at once and again when the display has started, about two
seconds later, as the manual describes.
"""

from __future__ import annotations

import asyncio
import logging

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

INPUT_TO_STATUS = {0x0E: b"\x05\x05", 0x0F: b"\x05\x04", 0x12: b"\x05\x0b", 0x2A: b"\x05\x2a"}
STARTUP_S = 2.0


def _xor(data: bytes) -> int:
    value = 0
    for b in data:
        value ^= b
    return value


def _reply(command: bytes, monitor_id: int, data: bytes = b"") -> bytes:
    body = command + bytes([monitor_id]) + data
    length = (len(body) + 1).to_bytes(2, "big")
    return b"\xab\xab" + length + body + bytes([_xor(length + body)]) + b"\xcd\xcd"


class HisenseSignageSimulator(TCPSimulator):
    """A Hisense signage display: binary frames, or the same frames as hex text."""

    SIMULATOR_INFO = {
        "driver_id": "hisense_signage",
        "name": "Hisense Signage Display Simulator",
        "category": "display",
        "transport": "tcp",
        "default_port": 8000,
        "initial_state": {
            "power": "on",
            "screen_on": True,
            "input": "hdmi_1",
            "volume": 30,
            "mute": False,
            "signal": True,
            "brightness": 50,
            "backlight_mode": "custom",
            "backlight": 20,
            "picture_mode": 0,
            "sound_mode": 0,
            "eye_protection": False,
            "usb_enabled": True,
            "power_on_mode": 1,
            "network": True,
            "temperature": 41,
            "rotation": 0,
            "last_command": "",
        },
        "controls": [
            {"type": "power", "key": "power", "label": "Power"},
            {"type": "select", "key": "input", "label": "Input",
             "options": ["hdmi_1", "hdmi_2", "hdmi_3", "hdbt"]},
            {"type": "slider", "key": "volume", "label": "Volume", "min": 0, "max": 100},
            {"type": "toggle", "key": "mute", "label": "Mute"},
            {"type": "toggle", "key": "signal", "label": "Signal"},
            {"type": "toggle", "key": "screen_on", "label": "Screen On"},
            {"type": "slider", "key": "temperature", "label": "Temperature", "min": 0, "max": 90},
            {"type": "indicator", "key": "last_command", "label": "Last command"},
        ],
    }

    INPUTS = {"hdmi_1": 0x0E, "hdmi_2": 0x0F, "hdmi_3": 0x12, "hdbt": 0x2A}

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # Raw byte stream: frames are binary or hex text, neither line-framed.
        self._delimiter = None
        self._line_mode = False
        self._rx = b""

    # ── Framing ──────────────────────────────────────────────────────────────

    def _next_frame(self) -> tuple[bytes, bool] | None:
        """Take one whole request off the buffer: (frame bytes, came as text)."""
        buf = self._rx
        binary = buf.find(b"\xdd\xff")
        text = buf.upper().find(b"DDFF")
        if text >= 0 and (binary < 0 or text < binary):
            digits = bytearray()
            used = 0
            for ch in buf[text:]:
                used += 1
                if chr(ch) in "0123456789abcdefABCDEF":
                    digits.append(ch)
                elif ch not in b" \r\n\t":
                    self._rx = buf[text + used:]
                    return None
                if len(digits) >= 8:
                    total = 2 * (4 + int(digits[4:8], 16) + 2)
                    if len(digits) == total:
                        self._rx = buf[text + used:]
                        return bytes.fromhex(digits.decode()), True
            return None
        if binary < 0:
            self._rx = b""
            return None
        buf = buf[binary:]
        if len(buf) < 4:
            self._rx = buf
            return None
        total = 4 + int.from_bytes(buf[2:4], "big") + 2
        if len(buf) < total:
            self._rx = buf
            return None
        self._rx = buf[total:]
        return buf[:total], False

    def handle_command(self, data: bytes) -> bytes | None:
        self._rx += data
        out = b""
        while True:
            taken = self._next_frame()
            if taken is None:
                break
            frame, as_text = taken
            reply = self._answer(frame)
            if reply:
                out += reply.hex().upper().encode() if as_text else reply
        return out or None

    # ── Commands ─────────────────────────────────────────────────────────────

    def _answer(self, frame: bytes) -> bytes | None:
        if len(frame) < 11 or frame[-2:] != b"\xbb\xcc" or frame[-3] != _xor(frame[2:-3]):
            return None
        command, monitor_id, data = frame[4:8], frame[8], frame[9:-3]
        expected = int(self.config.get("monitor_id", 1)) & 0xFF
        if monitor_id != expected:
            return None
        self.set_state("last_command", command.hex().upper() + (" " + data.hex().upper() if data else ""))
        state = self.state
        on = state.get("power") == "on"
        if command == b"\xc1\x28\x00\x00":  # Query TV Status
            source = INPUT_TO_STATUS.get(self.INPUTS.get(state.get("input"), 0x0E), b"\x05\x05")
            payload = bytes([int(state.get("volume", 0)) & 0xFF]) + source + bytes([
                0x00 if on else 0xFF,
                0x01 if state.get("mute") else 0x00,
                0x01 if state.get("signal") and on else 0x00,
            ])
            return _reply(command, monitor_id, payload)
        if command == b"\xc1\x15\x00\x00":  # Power
            if data == b"\xbb\xbb" and not on:
                loop = asyncio.get_running_loop()
                loop.call_later(STARTUP_S, self._finish_start, monitor_id)
                return _reply(command, monitor_id, data)
            if data == b"\xaa\xaa":
                self.set_state("power", "off")
            return _reply(command, monitor_id, data)
        if not on:
            return None
        sets = {
            b"\xc1\x31\x00\x00": lambda d: self.set_state("screen_on", d[:1] == b"\x01"),
            b"\xc1\x26\x00\x00": lambda d: self.set_state("mute", d[:1] == b"\x01"),
            b"\xc1\x27\x00\x00": lambda d: self.set_state("volume", min(d[0], 100)),
            b"\xc1\x36\x00\x00": lambda d: self.set_state("brightness", d[0]),
            b"\xc1\x0f\x06\x00": lambda d: self.set_state("picture_mode", d[0]),
            b"\xc1\xff\x00\x03": lambda d: self.set_state("sound_mode", d[0]),
            b"\xc1\xff\x00\x1e": lambda d: self.set_state("eye_protection", d[:1] == b"\x01"),
            b"\xc1\xff\x00\x0e": lambda d: self.set_state("usb_enabled", d[:1] == b"\x01"),
            b"\xc1\xff\x00\x09": lambda d: self.set_state("power_on_mode", d[0]),
            b"\xc1\x35\x00\x00": lambda d: self.set_state("rotation", d[0]),
        }
        if command == b"\xc1\x08\x00\x00":  # Input
            name = next((k for k, v in self.INPUTS.items() if v == (data[0] if data else -1)), None)
            if name:
                self.set_state("input", name)
            return _reply(command, monitor_id, data)
        if command == b"\xc1\x32\x00\x00" and len(data) >= 2:  # Set Backlight Brightness
            self.set_state("backlight_mode", "custom")
            self.set_state("backlight", data[1])
            return _reply(command, monitor_id, data)
        if command == b"\xc1\x34\x00\x00" and data:  # Backlight auto adjust
            self.set_state("backlight_mode", "auto" if data[0] else "custom")
            return _reply(command, monitor_id, data)
        if command in sets and data:
            sets[command](data)
            return _reply(command, monitor_id, data)
        if command in (b"\xc1\x37\x00\x00", b"\xc1\x38\x00\x00", b"\xc1\x3a\x00\x00",
                       b"\xc1\x3b\x00\x00", b"\xc1\x3c\x00\x00", b"\xc1\x17\x00\x00",
                       b"\xc1\x41\x00\x00", b"\xc1\xff\x00\x1a", b"\xc1\xff\x00\x13",
                       b"\xc1\x1e\x00\x00"):
            return _reply(command, monitor_id, data)
        queries = {
            b"\xc1\x32\x00\x01": lambda: bytes([0x01 if state.get("screen_on") else 0x00]),
            b"\xc1\x36\x00\x01": lambda: bytes([int(state.get("brightness", 0))]),
            b"\xc1\xfe\x00\x00": lambda: bytes([int(state.get("temperature", 0))]),
            b"\xc1\x6d\x00\x00": lambda: bytes([int(state.get("picture_mode", 0))]),
            b"\xc1\xff\x00\x02": lambda: bytes([int(state.get("sound_mode", 0))]),
            b"\xc1\xff\x00\x1d": lambda: bytes([0x01 if state.get("eye_protection") else 0x00]),
            b"\xc1\x6e\x00\x00": lambda: bytes([0x01 if state.get("usb_enabled") else 0x00]),
            b"\xc1\xff\x00\x08": lambda: bytes([int(state.get("power_on_mode", 1))]),
            b"\xc1\xff\x00\x16": lambda: bytes([0x01 if state.get("network") else 0x00]),
            b"\xc1\x3e\x00\x24": self._backlight,
            b"\xc1\x1b\x00\x00": lambda: b"\x26\x05\x12",
            b"\xc1\xfe\x00\x01": lambda: b"hisense",
            b"\xc1\xfe\x00\x02": lambda: str(self.config.get("model", "55OP90FE")).encode(),
            b"\xc1\xff\x00\x0b": lambda: b"HS55OP90FE0000000000123",
            b"\xc1\x6c\x00\x00": lambda: bytes.fromhex("A0B1C2D3E4F5"),
            b"\xc1\x1b\x20\x00": lambda: bytes([192, 168, 1, 60, 255, 255, 255, 0, 192, 168, 1, 1, 192, 168, 1, 1]),
            b"\xc1\x7d\x00\x00": lambda: bytes([int(state.get("volume", 0))]),
        }
        if command in queries:
            return _reply(command, monitor_id, queries[command]())
        return None

    def _backlight(self) -> bytes:
        if self.state.get("backlight_mode") == "auto":
            return b"\x03"
        return bytes([0x06, int(self.state.get("backlight", 0))])

    def _finish_start(self, monitor_id: int) -> None:
        self.set_state("power", "on")
        task = asyncio.ensure_future(self.push(_reply(b"\xc1\x15\x00\x00", monitor_id, b"\xbb\xbb")))
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
