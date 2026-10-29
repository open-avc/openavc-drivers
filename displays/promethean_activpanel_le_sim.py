"""
Promethean ActivPanel LE simulator (RS232 control codes).

Implements the panel side of the control codes in Promethean support
article 2905, "RS232 Control Codes for ActivPanel LE". The panel is serial
only; the simulator serves the same bytes over TCP, which is how a serial
device is simulated.

- Every frame is ``F6 <command> <type> <data...> <checksum> 6F``, the
  checksum the sum of every byte before it, mod 256. Frames with a bad
  checksum are ignored.
- Gets answer with the same command and type bytes and the value as data:
  one byte for power, volume, mute and input, ASCII text for the model,
  serial number and firmware.
- Sets change state and answer nothing: the article shows no reply to a
  set.
- Power: 01 on, 02 standby, 03 sleep. Asleep, the simulator answers only the
  power get and takes Power On (which gets the panel answers in sleep is not
  documented).
- The LE's own input codes: 09 HDMI (Front), 0A HDMI 1, 13 HDMI 2,
  0C USB-C (Front), 0D USB-C 2, 14 DP, 12 OPS, 0B Android.

Driver side: ``displays/promethean_activpanel_le.py``.
"""

from __future__ import annotations

import logging

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

HEAD = 0xF6
END = 0x6F

# Gets answering one byte from a state key, by (command, type).
BYTE_GETS = {
    (0x01, 0x02): "power",
    (0x0C, 0x02): "volume",
    (0x30, 0x02): "input_code",
}

TEXT_GETS = {
    (0x03, 0x01): "scaler_firmware",
    (0x03, 0x02): "touch_firmware",
    (0x03, 0x04): "serial_number",
    (0x03, 0x05): "model",
}

INPUT_CODES = [0x09, 0x0A, 0x13, 0x0C, 0x0D, 0x14, 0x12, 0x0B]


def _frame(body: bytes) -> bytes:
    head = bytes([HEAD]) + body
    return head + bytes([sum(head) & 0xFF, END])


def _split(buffer: bytes) -> tuple[bytes | None, bytes]:
    start = buffer.find(bytes([HEAD]))
    if start < 0:
        return None, b""
    buffer = buffer[start:]
    total = 0
    for i in range(min(len(buffer), 64)):
        if i >= 5 and buffer[i] == END and buffer[i - 1] == (total - buffer[i - 1]) & 0xFF:
            return buffer[: i + 1], buffer[i + 1:]
        total += buffer[i]
    if len(buffer) >= 64:
        return None, buffer[1:]
    return None, buffer


class PrometheanActivPanelLESimulator(TCPSimulator):
    """Simulates an ActivPanel LE answering RS232 control codes."""

    SIMULATOR_INFO = {
        "driver_id": "promethean_activpanel_le",
        "name": "Promethean ActivPanel LE Simulator",
        "transport": "tcp",
        "default_port": 0,
        "initial_state": {
            "power": 1,
            "input_code": 0x0A,
            "volume": 30,
            "mute": False,
            "model": "APLE-75",
            "serial_number": "BE75000123",
            "scaler_firmware": "1.1.0.12",
            "touch_firmware": "2.0.3",
        },
        "controls": [
            {"type": "select", "key": "power", "label": "Power (1 on, 2 standby, 3 sleep)", "options": [1, 2, 3]},
            {"type": "select", "key": "input_code",
             "label": "Input code (0x09 HDMI Front, 0x0A HDMI 1, 0x13 HDMI 2, 0x0C USB-C Front, 0x0D USB-C 2, 0x14 DP, 0x12 OPS, 0x0B Android)",
             "options": INPUT_CODES},
            {"type": "slider", "key": "volume", "label": "Volume", "min": 0, "max": 100, "step": 1},
            {"type": "toggle", "key": "mute", "label": "Mute"},
            {"type": "select", "key": "model", "label": "Model (AP10 to try the driver's refusal)",
             "options": ["APLE-65", "APLE-75", "APLE-86", "AP10-A75"]},
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None) -> None:
        super().__init__(device_id, config)
        self._delimiter = None
        self._line_mode = False
        self._rx = b""

    # ── Dispatch ─────────────────────────────────────────────────────────────

    def handle_command(self, data: bytes) -> bytes | None:
        self._rx += data
        out = b""
        while True:
            before = len(self._rx)
            frame, self._rx = _split(self._rx)
            if frame is None:
                if len(self._rx) >= before:
                    break
                continue
            if frame[-2] != sum(frame[:-2]) & 0xFF:
                continue
            reply = self._handle_frame(frame[1], frame[2], frame[3:-2])
            if reply:
                out += reply
        return out or None

    def _handle_frame(self, command: int, ftype: int, data: bytes) -> bytes | None:
        state = self.state
        key = (command, ftype)
        value = data[0] if data else 0

        if int(state.get("power", 1)) == 3:
            # Asleep: answer the power get, and take Power On.
            if key == (0x01, 0x02):
                return _frame(bytes([0x01, 0x02, 0x03]))
            if key == (0x01, 0x01) and value == 0x01:
                self.set_state("power", 1)
            return None

        # Gets
        if key in BYTE_GETS:
            return _frame(bytes([command, ftype, int(state.get(BYTE_GETS[key], 0)) & 0xFF]))
        if key in TEXT_GETS:
            return _frame(bytes([command, ftype]) + str(state.get(TEXT_GETS[key], "")).encode("ascii"))
        if key == (0x02, 0x02):
            return _frame(bytes([0x02, 0x02, 0x01 if state.get("mute") else 0x00]))

        # Sets (no reply)
        if key == (0x0C, 0x01) and value <= 100:
            self.set_state("volume", value)
        elif key == (0x01, 0x01) and value in (0x00, 0x01):
            self.set_state("power", 1 if value == 0x01 else 2)
        elif key == (0x01, 0x05):
            self.set_state("power", 3)
        elif key == (0x0C, 0x00):
            level = int(state.get("volume", 0)) + (1 if value == 0x01 else -1)
            self.set_state("volume", max(0, min(100, level)))
        elif key == (0x02, 0x00) and value in (0x00, 0x01):
            self.set_state("mute", value == 0x01)
        elif key == (0x30, 0x01) and value in INPUT_CODES:
            self.set_state("input_code", value)
        elif key in ((0x03, 0x06), (0x3B, 0x01)):
            logger.info("%s: %s (no effect in simulation)", self.name,
                        "factory reset" if command == 0x03 else "reset apps and data")
        else:
            logger.debug("%s: no handler for %02X %02X %s", self.name, command, ftype, data.hex(" "))
        return None
