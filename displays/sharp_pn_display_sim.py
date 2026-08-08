"""
Sharp PN-series display simulator (pre-merger RS-232C / LAN protocol,
TCP data port 10008).

Implements the monitor side of Sharp's 4+4 ASCII grammar as documented
in the PN-L603B operation manual:

- LAN login gate: prompts ``Login:`` / ``Password:`` (no line ending,
  like the real monitor), accepts any credentials EXCEPT the ``invalid``
  sentinel (matching the platform's telnet-login failure convention),
  answers ``OK`` on success and re-prompts + drops on rejection.
- Reads (parameter containing ``?``) answer the BARE value with no
  command echo — the ambiguity that forced the driver to a serialized
  request/response design.
- Slow commands (POWR, INPS, WIDE, MWIN, MWIP writes) answer an interim
  ``WAIT`` line before the final ``OK``, exercising the driver's
  deadline extension.
- The documented standby restriction list (MUTE, TPEN, PXCK, RESO, ...)
  answers ``ERR`` while in standby; POWR and the other reads keep
  working (STANDARD standby-mode behavior).
- The ``locked`` control simulates the operation lock: every command
  answers ``LOCKED``.
- A ``monitor_id`` config (RS-232C daisy chains) suffixes acks and
  values with the responder ID ("OK 001"); WAIT carries no suffix,
  exactly like the manual.

Driver side: ``displays/sharp_pn_display.py``.
"""

from __future__ import annotations

import asyncio
import logging

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

# Write command -> (state key, min, max) for plain numeric ranges.
NUMERIC_WRITES = {
    "VOLM": ("volume", 0, 31),
    "VLMP": ("brightness", 0, 31),
    "CONT": ("contrast", 0, 60),
    "BLVL": ("black_level", 0, 60),
    "TINT": ("tint", 0, 60),
    "COLR": ("color", 0, 60),
    "SHRP": ("sharpness", 0, 24),
    "AUTR": ("treble", -5, 5),
    "AUBS": ("bass", -5, 5),
    "AUBL": ("balance", -10, 10),
    "MUTE": ("mute", 0, 1),
    "MWIN": ("pip_mode", 0, 3),
    "MPSZ": ("pip_size", 1, 64),
    "MWAD": ("pip_sound", 1, 2),
    "TPEN": ("touch_enabled", 0, 1),
    "STBM": ("standby_mode", 0, 1),
    "ALCK": ("adjustment_lock", 0, 2),
    "LOSD": ("osd_display", 0, 2),
    "OFLD": ("led_off", 0, 1),
    "SCSV": ("screen_motion", 0, 4),
}

# Read command -> state key for plain numerics.
NUMERIC_READS = {
    "POWR": "power",
    "VOLM": "volume",
    "MUTE": "mute",
    "VLMP": "brightness",
    "CONT": "contrast",
    "BLVL": "black_level",
    "TINT": "tint",
    "COLR": "color",
    "SHRP": "sharpness",
    "AUTR": "treble",
    "AUBS": "bass",
    "AUBL": "balance",
    "WIDE": "screen_size",
    "MWIN": "pip_mode",
    "MWIP": "pip_source",
    "MWAD": "pip_sound",
    "TPEN": "touch_enabled",
    "STBM": "standby_mode",
    "ALCK": "adjustment_lock",
    "LOSD": "osd_display",
    "OFLD": "led_off",
    "DSTA": "temp_status",
    "ERRT": "temperature",
    "STCA": "standby_cause",
    "INPS": "input_code",
}

INPUT_CODES = {1, 2, 3, 4, 9, 10, 12, 13, 14, 16, 17, 18}
PC_INPUTS = {2, 10, 13, 14, 16, 18}
AV_INPUTS = {1, 3, 4, 9, 12, 17}

# Commands the manual bars in standby mode.
STANDBY_BLOCKED = {
    "TPEN", "ASNC", "CLCK", "PHSE", "HPOS", "VPOS", "HSIZ", "VSIZ",
    "HRES", "VRES", "ARST", "CPTU", "AGIN", "TOMD", "PXCK", "PXSL",
    "RESO", "RSET", "MUTE",
}

# Writes that answer an interim WAIT before the final response.
WAIT_COMMANDS = {"POWR", "INPS", "WIDE", "MWIN", "MWIP"}


class SharpPnDisplaySimulator(TCPSimulator):
    """Simulates a pre-merger Sharp PN monitor on the 4+4 ASCII protocol."""

    SIMULATOR_INFO = {
        "driver_id": "sharp_pn_display",
        "name": "Sharp PN Display Simulator",
        "delimiter": "\r",
        "initial_state": {
            "power": 1,
            "input_code": 10,
            "volume": 15,
            "mute": 0,
            "brightness": 20,
            "contrast": 30,
            "black_level": 30,
            "tint": 30,
            "color": 30,
            "sharpness": 12,
            "treble": 0,
            "bass": 0,
            "balance": 0,
            "screen_size": 1,
            "pip_mode": 0,
            "pip_source": 10,
            "pip_size": 32,
            "pip_sound": 1,
            "touch_enabled": 1,
            "standby_mode": 0,
            "adjustment_lock": 0,
            "osd_display": 0,
            "led_off": 0,
            "screen_motion": 0,
            "temp_status": 0,
            "temperature": 33,
            "standby_cause": 0,
            "model_name": "PN-L603B",
            "serial_number": "8B0123456",
            "pc_resolution": "1920, 1080",
            "av_resolution": "1080p",
            "locked": 0,
        },
        "controls": [
            {"type": "select", "key": "power", "label": "Power (1=on, 0=standby, 2=signal wait)",
             "options": [0, 1, 2]},
            {"type": "select", "key": "input_code", "label": "Input Code (INPS)",
             "options": sorted(INPUT_CODES)},
            {"type": "slider", "key": "volume", "label": "Volume", "min": 0, "max": 31, "step": 1},
            {"type": "select", "key": "mute", "label": "Mute (1=muted)", "options": [0, 1]},
            {"type": "slider", "key": "brightness", "label": "Brightness", "min": 0, "max": 31, "step": 1},
            {"type": "slider", "key": "temperature", "label": "Temperature (C)", "min": 20, "max": 80, "step": 1},
            {"type": "select", "key": "temp_status", "label": "Temp Status (DSTA)",
             "options": [0, 1, 2, 3, 4]},
            {"type": "select", "key": "locked", "label": "Operation Lock (1=LOCKED)",
             "options": [0, 1]},
            {"type": "indicator", "key": "model_name", "label": "Model"},
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None) -> None:
        super().__init__(device_id, config)
        # 0 = no ID assigned (LAN / one-to-one serial): no reply suffix.
        self._monitor_id = int((config or {}).get("monitor_id", 0) or 0)

    # ── LAN login gate ─────────────────────────────────────────────────────

    async def authenticate_client(self, reader, writer, client_id) -> bool:
        try:
            writer.write(b"Login:")
            await writer.drain()
            user = (await asyncio.wait_for(reader.readline(), timeout=10)).decode(
                "ascii", errors="replace").strip()
            writer.write(b"Password:")
            await writer.drain()
            password = (await asyncio.wait_for(reader.readline(), timeout=10)).decode(
                "ascii", errors="replace").strip()
        except (asyncio.TimeoutError, ConnectionError, OSError):
            return False
        if user == "invalid" or password == "invalid":
            # A real unit re-prompts; reject the session.
            try:
                writer.write(b"Login:")
                await writer.drain()
            except (ConnectionError, OSError):
                pass
            return False
        writer.write(b"OK\r\n")
        await writer.drain()
        return True

    # ── Reply helpers ──────────────────────────────────────────────────────

    def _suffix(self) -> str:
        return f" {self._monitor_id:03d}" if self._monitor_id else ""

    def _line(self, text: str) -> bytes:
        return (text + self._suffix() + "\r\n").encode("ascii")

    def _ok(self) -> bytes:
        return self._line("OK")

    def _err(self) -> bytes:
        return self._line("ERR")

    # ── Dispatch ───────────────────────────────────────────────────────────

    def handle_command(self, data: bytes) -> bytes | None:
        frame = data.strip(b"\r\n")
        if len(frame) < 4:
            return None
        try:
            cmd = frame[:4].decode("ascii").upper()
            param = frame[4:].decode("ascii", errors="replace").strip()
        except UnicodeDecodeError:
            return self._err()

        if int(self.state["locked"]):
            # Operation lock: LOCKED carries no ID suffix in the manual's
            # response-format figures, matching WAIT.
            return b"LOCKED\r\n"

        if int(self.state["power"]) == 0 and cmd in STANDBY_BLOCKED:
            return self._err()

        is_read = "?" in param
        if is_read:
            return self._handle_read(cmd)
        return self._handle_write(cmd, param)

    # ── Reads ──────────────────────────────────────────────────────────────

    def _handle_read(self, cmd: str) -> bytes:
        if cmd == "INF1":
            return self._line(str(self.state["model_name"]))
        if cmd == "SRNO":
            return self._line(str(self.state["serial_number"]))
        if cmd == "PXCK":
            if int(self.state["input_code"]) in PC_INPUTS:
                return self._line(str(self.state["pc_resolution"]))
            return self._err()
        if cmd == "RESO":
            if int(self.state["input_code"]) in AV_INPUTS:
                return self._line(str(self.state["av_resolution"]))
            return self._err()
        key = NUMERIC_READS.get(cmd)
        if key is None:
            return self._err()
        return self._line(str(int(self.state[key])))

    # ── Writes ─────────────────────────────────────────────────────────────

    def _wait_wrap(self, cmd: str, final: bytes) -> bytes:
        if cmd in WAIT_COMMANDS:
            # WAIT never carries an ID suffix.
            return b"WAIT\r\n" + final
        return final

    def _handle_write(self, cmd: str, param: str) -> bytes:
        try:
            value = int(param)
        except ValueError:
            return self._err()

        if cmd == "POWR":
            if value not in (0, 1):
                return self._err()
            self.set_state("power", value)
            return self._wait_wrap(cmd, self._ok())

        if cmd == "INPS":
            if value == 0:
                codes = sorted(INPUT_CODES)
                current = int(self.state["input_code"])
                idx = codes.index(current) if current in codes else -1
                self.set_state("input_code", codes[(idx + 1) % len(codes)])
                return self._wait_wrap(cmd, self._ok())
            if value not in INPUT_CODES:
                return self._err()
            self.set_state("input_code", value)
            return self._wait_wrap(cmd, self._ok())

        if cmd == "MWIP":
            if value not in INPUT_CODES:
                return self._err()
            self.set_state("pip_source", value)
            return self._wait_wrap(cmd, self._ok())

        if cmd == "WIDE":
            if value == 0:
                self.set_state("screen_size", int(self.state["screen_size"]) % 5 + 1)
                return self._wait_wrap(cmd, self._ok())
            if not 1 <= value <= 5:
                return self._err()
            self.set_state("screen_size", value)
            return self._wait_wrap(cmd, self._ok())

        entry = NUMERIC_WRITES.get(cmd)
        if entry is not None:
            key, minimum, maximum = entry
            if not minimum <= value <= maximum:
                return self._err()
            self.set_state(key, value)
            return self._wait_wrap(cmd, self._ok())

        return self._err()
