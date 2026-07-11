"""
BenQ commercial display simulator (RS232 & LAN protocol, TCP 4660).

Implements the display side of BenQ's framed set/get grammar as
documented in the RM6503 RS232 & LAN Protocol Installation Guide:

- Set commands answer the 5-byte ACK ('+' valid / '-' out of range or
  unknown), gets answer the framed 'r' reply that echoes the command
  code — including the high-bit codes (0x81+/0xB1+) that forced the
  driver to Python.
- The model-info (0x20) and network (0xE1) queries answer their binary
  selector-byte payloads (ASCII text / raw MAC bytes with NUL padding).
- Packets addressed to a Monitor ID other than the simulator's get no
  reply, mirroring a real RS-232 chain and exercising the driver's ID
  filter.
- Power model: "001" on, "000" screen off, "002" standby, "003" reboot.
  A real display drops its LAN control port in standby; the simulator
  stays reachable (a TCP server that vanishes would end the test
  session) but answers only the power get while "in standby", which is
  the closest testable approximation.
- Remote keys mutate what they really change (volume up/down); blank
  and freeze have no read-back in the protocol, so they only ACK.

Driver side: ``displays/benq_display.py``.
"""

from __future__ import annotations

import logging

from simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

# Set code -> (state key, decoder) for plain three-digit numerics.
NUMERIC_SETS = {
    0x23: "contrast",
    0x24: "brightness",
    0x25: "sharpness",
    0x35: "volume",
    0x37: "treble",
    0x38: "bass",
    0x39: "balance",
    0x82: "saturation",
    0x83: "hue",
    0x84: "backlight",
}

# Get code -> state key for plain three-digit numerics.
NUMERIC_GETS = {
    0x61: "contrast",
    0x62: "brightness",
    0x63: "sharpness",
    0x66: "volume",
    0x37: "treble",
    0x38: "bass",
    0x39: "balance",
    0xB2: "saturation",
    0xB3: "hue",
    0xB4: "backlight",
}

# Enum-coded settings: set code, get code, state key, allowed wire values.
ENUM_FUNCTIONS = [
    (0x22, 0x6A, "source_code", {"000", "001", "002", "021", "007", "051", "101", "102", "107", "108"}),
    (0x33, 0x65, "sound_mode_code", {"000", "001", "002", "003", "004"}),
    (0x31, 0x77, "aspect_code", {"000", "002"}),
    (0x81, 0xB1, "picture_mode_code", {"000", "001", "002", "003", "005", "006", "007"}),
    (0x86, 0xB6, "color_temp_code", {"000", "001", "002"}),
    (0x85, 0xB5, "dcr_code", {"000", "001"}),
    (0x36, 0x67, "mute_code", {"000", "001"}),
    (0x42, 0x68, "ir_lock_code", {"000", "001"}),
    (0x45, 0x73, "keypad_lock_code", {"000", "001"}),
    (0xA9, 0xD9, "power_save_code", {"000", "001", "002"}),
    (0xAB, 0xDA, "switch_on_code", {"000", "001", "002"}),
    (0xF0, 0xF0, "wol_code", {"000", "001"}),
]
ENUM_BY_SET = {entry[0]: entry for entry in ENUM_FUNCTIONS}
ENUM_BY_GET = {entry[1]: entry for entry in ENUM_FUNCTIONS}


class BenqDisplaySimulator(TCPSimulator):
    """Simulates a BenQ Board / Smart Signage display on the framed protocol."""

    SIMULATOR_INFO = {
        "driver_id": "benq_display",
        "name": "BenQ Display Simulator",
        "delimiter": "\r",
        "initial_state": {
            "power_code": "001",
            "source_code": "001",
            "signal_code": "001",
            "mute_code": "000",
            "volume": 30,
            "contrast": 50,
            "brightness": 50,
            "sharpness": 50,
            "saturation": 50,
            "hue": 50,
            "backlight": 80,
            "treble": 50,
            "bass": 50,
            "balance": 50,
            "sound_mode_code": "001",
            "aspect_code": "000",
            "picture_mode_code": "000",
            "color_temp_code": "001",
            "dcr_code": "000",
            "ir_lock_code": "001",
            "keypad_lock_code": "001",
            "power_save_code": "000",
            "switch_on_code": "002",
            "wol_code": "001",
            "operation_time": 1786,
            "model_name": "RM6503",
            "firmware_version": "1.02",
            "serial_number": "ETC1M00001SL0",
            "mac_address": "80:65:e9:12:34:56",
        },
        "controls": [
            {"type": "select", "key": "power_code", "label": "Power (001=on, 000=screen off, 002=standby)",
             "options": ["000", "001", "002"]},
            {"type": "select", "key": "source_code", "label": "Source Code",
             "options": ["000", "001", "002", "021", "007", "051", "101", "102", "107", "108"]},
            {"type": "slider", "key": "volume", "label": "Volume", "min": 0, "max": 100, "step": 1},
            {"type": "slider", "key": "backlight", "label": "Backlight", "min": 0, "max": 100, "step": 1},
            {"type": "select", "key": "mute_code", "label": "Mute (001=muted)",
             "options": ["000", "001"]},
            {"type": "indicator", "key": "model_name", "label": "Model"},
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None) -> None:
        super().__init__(device_id, config)
        self._monitor_id = int((config or {}).get("monitor_id", 1) or 1)

    # ── Frame helpers ──────────────────────────────────────────────────────

    def _ack(self, ok: bool) -> bytes:
        body = f"{self._monitor_id:02d}".encode() + (b"+" if ok else b"-")
        return bytes([0x30 + len(body) + 1]) + body + b"\r"

    def _reply(self, code: int, value: bytes) -> bytes:
        body = f"{self._monitor_id:02d}".encode() + b"r" + bytes([code]) + value
        return bytes([0x30 + len(body) + 1]) + body + b"\r"

    # ── Dispatch ───────────────────────────────────────────────────────────

    def handle_command(self, data: bytes) -> bytes | None:
        frame = data.strip(b"\r\n")
        if len(frame) < 5:
            return None
        mid = frame[1:3]
        try:
            if int(mid.decode("ascii")) != self._monitor_id:
                return None  # not addressed to this display: chain silence
        except (UnicodeDecodeError, ValueError):
            return None
        ftype = frame[3:4]
        code = frame[4]
        payload = frame[5:]

        if ftype == b"s":
            return self._handle_set(code, payload)
        if ftype == b"g":
            return self._handle_get(code, payload)
        return self._ack(False)

    # ── Sets ───────────────────────────────────────────────────────────────

    def _handle_set(self, code: int, payload: bytes) -> bytes:
        value = payload.decode("ascii", errors="replace")

        # In standby only the power function answers (real units go further
        # and drop LAN entirely).
        if self.state["power_code"] == "002" and code != 0x21:
            return self._ack(False)

        if code == 0x21:  # power
            if value not in ("000", "001", "002", "003"):
                return self._ack(False)
            if value != "003":  # reboot leaves the state as-is
                self.set_state("power_code", value)
            return self._ack(True)

        if code in NUMERIC_SETS:
            if not value.isdigit() or not 0 <= int(value) <= 100:
                return self._ack(False)
            self.set_state(NUMERIC_SETS[code], int(value))
            return self._ack(True)

        if code in ENUM_BY_SET:
            _set, _get, key, allowed = ENUM_BY_SET[code]
            if value not in allowed:
                return self._ack(False)
            self.set_state(key, value)
            return self._ack(True)

        if code == 0x40:  # remote key
            if value == "000":
                self.set_state("volume", min(100, int(self.state["volume"]) + 1))
            elif value == "001":
                self.set_state("volume", max(0, int(self.state["volume"]) - 1))
            elif value not in ("010", "011", "012", "013", "014", "020", "022", "031", "032"):
                return self._ack(False)
            return self._ack(True)

        if code in (0x26, 0x3B):  # picture / sound reset
            return self._ack(True)

        return self._ack(False)

    # ── Gets ───────────────────────────────────────────────────────────────

    def _handle_get(self, code: int, payload: bytes) -> bytes:
        if self.state["power_code"] == "002" and code != 0x6C:
            return self._ack(False)

        if code == 0x6C:
            return self._reply(code, str(self.state["power_code"]).encode())
        if code == 0x6A:
            return self._reply(code, str(self.state["source_code"]).encode())
        if code == 0x22:
            return self._reply(code, str(self.state["signal_code"]).encode())
        if code == 0x67:
            return self._reply(code, str(self.state["mute_code"]).encode())
        if code in NUMERIC_GETS:
            return self._reply(code, f"{int(self.state[NUMERIC_GETS[code]]):03d}".encode())
        if code in ENUM_BY_GET:
            _set, _get, key, _allowed = ENUM_BY_GET[code]
            return self._reply(code, str(self.state[key]).encode())
        if code == 0x76:  # operation time, five-digit value
            return self._reply(code, f"{int(self.state['operation_time']):05d}".encode())
        if code == 0x20 and payload:  # model info by selector byte
            sub = payload[0]
            text = {
                0x02: str(self.state["model_name"]),
                0x04: str(self.state["firmware_version"]),
                0x06: str(self.state["serial_number"]),
            }.get(sub)
            if text is None:
                return self._ack(False)
            block = text.encode("ascii")[:14]
            return self._reply(code, bytes([sub]) + block + b"\x00" * (14 - len(block)))
        if code == 0xE1 and payload:  # network info by selector byte
            sub = payload[0]
            if sub in (0x06, 0x07):
                mac = bytes(int(p, 16) for p in str(self.state["mac_address"]).split(":"))
                return self._reply(code, bytes([sub]) + mac + b"\x00" * 2)
            return self._ack(False)

        return self._ack(False)
