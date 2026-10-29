"""
Promethean ActivPanel 10 simulator (RS232 control codes over TCP 5000).

Implements the panel side of the control codes in Promethean support
article 2752, "RS232 Control Codes for ActivPanel 10":

- Every frame is ``F6 <command> <type> <data...> <checksum> 6F``, the
  checksum the sum of every byte before it, mod 256. Frames with a bad
  checksum are ignored.
- Gets answer with the same command and type bytes and the value as data:
  one byte for states and levels, ASCII text for the model, serial number
  and firmware, four octets for the network addresses, six bytes for the
  MAC and for the power-on time.
- Sets change state and answer nothing: the article shows no reply to a
  set.
- The article's polarities are kept as written: Blank and Remote Control
  Lock are set On with data 00 but read back 01 when on; Touch Lock is set
  On with 00 and reads back 00 when on; Freeze is set On 01 / Off 02 and
  reads back 01 when on.
- Power: 01 on, 02 standby, 03 sleep. A real panel in Sleep turns its
  network off; the simulator stays reachable (a server that vanished would
  end the session) but answers only the power get while asleep, and takes
  Power On.

Driver side: ``displays/promethean_activpanel10.py``.
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
    (0x31, 0x02): "ops_installed",
    (0x31, 0x03): "ops_power",
    (0x34, 0x03): "contrast",
    (0x34, 0x05): "brightness",
    (0x34, 0x07): "sharpness",
    (0x34, 0x0B): "backlight",
    (0x34, 0x0F): "auto_brightness",
    (0x34, 0x12): "backlight_mode",
    (0x39, 0x04): "standby_timer",
    (0x3A, 0x04): "sleep_timer",
    (0x20, 0x02): "network_mode",
}

TEXT_GETS = {
    (0x03, 0x01): "scaler_firmware",
    (0x03, 0x02): "touch_firmware",
    (0x03, 0x04): "serial_number",
    (0x03, 0x05): "model",
    (0x03, 0x07): "mcu_firmware",
}

ADDRESS_GETS = {
    (0x20, 0x04): "ip_address", (0x20, 0x0B): "ip_address",
    (0x20, 0x06): "gateway", (0x20, 0x0C): "gateway",
    (0x20, 0x08): "subnet_mask", (0x20, 0x0D): "subnet_mask",
}

# Level sets (0-100) by (command, type).
LEVEL_SETS = {
    (0x0C, 0x01): "volume",
    (0x34, 0x04): "contrast",
    (0x34, 0x06): "brightness",
    (0x34, 0x08): "sharpness",
    (0x34, 0x0C): "backlight",
}

INPUT_CODES = [0x13, 0x0C, 0x09, 0x0A, 0x14, 0x12]
BUTTONS = {1: "power", 2: "source", 3: "volume", 4: "flame", 5: "freeze", 6: "touch"}


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


class PrometheanActivPanel10Simulator(TCPSimulator):
    """Simulates an ActivPanel 10 answering RS232 control codes on TCP 5000."""

    SIMULATOR_INFO = {
        "driver_id": "promethean_activpanel10",
        "name": "Promethean ActivPanel 10 Simulator",
        "transport": "tcp",
        "default_port": 5000,
        "initial_state": {
            "power": 1,
            "input_code": 0x09,
            "volume": 30,
            "mute": False,
            "freeze": False,
            "blank": False,
            "touch_lock": False,
            "remote_lock": False,
            "ops_installed": 1,
            "ops_power": 1,
            "contrast": 50,
            "brightness": 50,
            "sharpness": 50,
            "backlight": 80,
            "auto_brightness": 1,
            "backlight_mode": 0,
            "standby_timer": 0x02,
            "sleep_timer": 0x03,
            "button_power": True,
            "button_source": True,
            "button_volume": True,
            "button_flame": True,
            "button_freeze": True,
            "button_touch": True,
            "camera": False,
            "model": "AP10-B75",
            "serial_number": "AB75000123",
            "scaler_firmware": "1.3.0.18",
            "mcu_firmware": "1.0.7",
            "touch_firmware": "2.1.4",
            "network_mode": 0,
            "ip_address": "192.168.1.38",
            "subnet_mask": "255.255.255.0",
            "gateway": "192.168.1.1",
            "mac_address": "6c:dc:6a:12:34:56",
        },
        "controls": [
            {"type": "select", "key": "power", "label": "Power (1 on, 2 standby, 3 sleep)", "options": [1, 2, 3]},
            {"type": "select", "key": "input_code", "label": "Input code (0x13 USB-C 1, 0x0C USB-C 2, 0x09 HDMI 1, 0x0A HDMI 2, 0x14 DP, 0x12 OPS)",
             "options": INPUT_CODES},
            {"type": "slider", "key": "volume", "label": "Volume", "min": 0, "max": 100, "step": 1},
            {"type": "toggle", "key": "mute", "label": "Mute"},
            {"type": "toggle", "key": "freeze", "label": "Freeze"},
            {"type": "toggle", "key": "blank", "label": "Blank"},
            {"type": "toggle", "key": "touch_lock", "label": "Touch Lock"},
            {"type": "slider", "key": "backlight", "label": "Backlight", "min": 0, "max": 100, "step": 1},
            {"type": "select", "key": "model", "label": "Model (AP9 to try the driver's refusal)",
             "options": ["AP10-A75", "AP10-B75", "AP9-B75"]},
            {"type": "indicator", "key": "camera", "label": "ePTZ Camera On"},
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
            # Asleep: the network is off on a real panel. Answer the power
            # get, and take Power On.
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
        if key in ADDRESS_GETS:
            octets = bytes(int(p) for p in str(state.get(ADDRESS_GETS[key], "0.0.0.0")).split("."))
            return _frame(bytes([command, ftype]) + octets)
        if key == (0x20, 0x09):
            mac = bytes.fromhex(str(state.get("mac_address", "")).replace(":", ""))
            return _frame(bytes([0x20, 0x09]) + mac)
        if key == (0x3D, 0x01):
            return _frame(bytes([0x3D, 0x01, 25, 10, 29, 8, 15, 42]))
        if key == (0x02, 0x02):
            return _frame(bytes([0x02, 0x02, 0x01 if state.get("mute") else 0x00]))
        if key == (0x32, 0x02):
            return _frame(bytes([0x32, 0x02, 0x01 if state.get("freeze") else 0x00]))
        if key == (0x35, 0x02):
            return _frame(bytes([0x35, 0x02, 0x01 if state.get("blank") else 0x00]))
        if key == (0x36, 0x02):
            # "00 = Touch Lock On, 01 = Touch Lock Off"
            return _frame(bytes([0x36, 0x02, 0x00 if state.get("touch_lock") else 0x01]))
        if key == (0x37, 0x02):
            return _frame(bytes([0x37, 0x02, 0x01 if state.get("remote_lock") else 0x00]))
        if command == 0x55 and ftype in BUTTONS:
            enabled = bool(state.get(f"button_{BUTTONS[ftype]}", True))
            return _frame(bytes([0x55, ftype, 0x01 if enabled else 0x02]))

        # Sets (no reply)
        if key in LEVEL_SETS and value <= 100:
            self.set_state(LEVEL_SETS[key], value)
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
        elif key == (0x31, 0x01) and value in (0x01, 0x02):
            if int(state.get("ops_installed", 0)):
                self.set_state("ops_power", 1 if value == 0x01 else 0)
        elif key == (0x32, 0x01) and value in (0x01, 0x02):
            self.set_state("freeze", value == 0x01)
        elif key == (0x35, 0x01) and value in (0x00, 0x01):
            self.set_state("blank", value == 0x00)
        elif key == (0x36, 0x01) and value in (0x00, 0x01):
            self.set_state("touch_lock", value == 0x00)
        elif key == (0x37, 0x01) and value in (0x00, 0x01):
            self.set_state("remote_lock", value == 0x00)
        elif key == (0x34, 0x0E) and value in (0x00, 0x01):
            self.set_state("auto_brightness", value)
        elif key == (0x34, 0x13) and value in (0x00, 0x01, 0x02):
            self.set_state("backlight_mode", value)
        elif key == (0x34, 0x0D):
            for name in ("contrast", "brightness", "sharpness"):
                self.set_state(name, 50)
        elif key == (0x39, 0x03) and value <= 0x07:
            self.set_state("standby_timer", value)
        elif key == (0x3A, 0x03) and value <= 0x07:
            self.set_state("sleep_timer", value)
        elif command == 0x54 and value in (0x01, 0x02):
            targets = BUTTONS.values() if ftype == 0x00 else [BUTTONS.get(ftype)]
            for name in targets:
                if name:
                    self.set_state(f"button_{name}", value == 0x01)
        elif command == 0x80 and data[:5] == bytes([0x81, 0x0A, 0x11, 0x55, 0x01]):
            self.set_state("camera", True)
        elif command == 0x80 and data[:5] == bytes([0x81, 0x0A, 0x11, 0x55, 0x00]):
            self.set_state("camera", False)
        else:
            logger.debug("%s: no handler for %02X %02X %s", self.name, command, ftype, data.hex(" "))
        return None
