"""
ViewSonic commercial display simulator (LFD RS-232 & LAN protocol, TCP 5000).

Implements the display side of ViewSonic's framed set/get grammar as
documented in the LFD RS-232 & LAN Protocol Specification v3.3.2:

- Set commands answer the 5-byte ACK ('+' valid / '-' out of range or
  unknown); gets answer the framed 'r' reply that echoes the command
  code. The backlight level answers its dedicated 'A'/'a' command-type
  pair (code 'B').
- Info queries (device name, MAC, IP, serial, firmware, operation
  hours, smart hub) answer the fixed 32-byte NUL-padded format.
- The Get-Input reply packs the signal-detect digit ahead of the
  source code's last two characters, exactly like the spec's table.
- Auto-reply (*3.2.1): a state change made "by the user" (the
  Simulator UI controls) pushes the updated power / input /
  brightness / backlight / volume / mute frame to connected clients
  unsolicited; changes made by the controller's own set commands do
  not push, matching the spec's wording.
- Packets addressed to a Monitor ID other than the simulator's get no
  reply, mirroring a real RS-232 chain and exercising the driver's ID
  filter.
- Power model: "001" on, "000" standby. Real displays may close the
  LAN port entirely in standby (setting-dependent); the simulator
  stays reachable but answers only the power set/get and the Get-ACK
  link test while "in standby", which is the closest testable
  approximation.

Driver side: ``displays/viewsonic_cde.py``.
"""

from __future__ import annotations

import asyncio
import logging

from simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

# Set code -> state key for plain 0-100 numerics (type 's').
NUMERIC_SETS = {
    "#": "contrast",
    "$": "brightness",
    "%": "sharpness",
    "&": "color",
    "'": "tint",
    "5": "volume",
    ".": "bass",
    "/": "treble",
    "0": "balance",
}

# Get code -> state key for plain 0-100 numerics (type 'g').
NUMERIC_GETS = {
    "a": "contrast",
    "b": "brightness",
    "c": "sharpness",
    "d": "color",
    "e": "tint",
    "f": "volume",
}

# Enum-coded set/get pairs: set code, get code, state key, allowed values.
ENUM_FUNCTIONS = [
    ("6", "g", "mute_code", {"000", "001"}),
    ("(", "h", "backlight_on_code", {"000", "001"}),
    ("*", "i", "freeze_code", {"000", "001"}),
    ("4", "o", "power_lock_code", {"000", "001"}),
    ("8", "p", "button_lock_code", {"000", "001"}),
    (">", "q", "menu_lock_code", {"000", "001"}),
    ("B", "n", "rcu_mode_code", {"000", "001", "002"}),
    ("9", "t", "pip_mode_code", {"000", "001", "002"}),
    ("P", "v", "tiling_mode_code", {"000", "001"}),
    ("Q", "w", "tiling_comp_code", {"000", "001"}),
]
ENUM_BY_SET = {entry[0]: entry for entry in ENUM_FUNCTIONS}
ENUM_BY_GET = {entry[1]: entry for entry in ENUM_FUNCTIONS}

INPUT_CODES = {
    "000", "001", "002", "003", "004", "014", "024", "034",
    "005", "006", "016", "026", "007", "008", "009", "029",
    "019", "039", "00A",
}
INPUT_CYCLE_ORDER = ["004", "014", "007", "00A", "006"]

# Set codes that are valid but have no read-back: ack-only.
ACK_ONLY_SETS = {
    ")": {"000", "001", "002", "003"},   # color mode
    "1": {"000", "001", "002"},          # picture size
    "2": {"000", "001", "002"},          # OSD language
    "-": {"000", "001"},                 # surround
    ":": {"000", "001"},                 # PIP sound
    ";": {"000", "001", "002", "003"},   # PIP position
    "~": {"000"},                        # restore default
}

# Function On_Off ids -> the enum state each one mirrors.
FUNCTION_IDS = {"01": "backlight_on_code", "02": "freeze_code", "03": "touch_code"}

# Sim state key -> get code for the *3.2.1 auto-reply push set.
AUTO_REPLY_KEYS = {
    "power_code": "l",
    "input_code": "j",
    "brightness": "b",
    "backlight": "B",
    "volume": "f",
    "mute_code": "g",
}


class ViewSonicCdeSimulator(TCPSimulator):
    """Simulates a ViewSonic CDE display on the LFD RS-232 & LAN protocol."""

    SIMULATOR_INFO = {
        "driver_id": "viewsonic_cde",
        "name": "ViewSonic Commercial Display Simulator",
        "delimiter": "\r",
        "initial_state": {
            "power_code": "001",
            "input_code": "004",
            "signal_code": "1",
            "volume": 30,
            "mute_code": "000",
            "brightness": 50,
            "contrast": 50,
            "sharpness": 50,
            "color": 50,
            "tint": 50,
            "bass": 50,
            "treble": 50,
            "balance": 50,
            "backlight": 80,
            "backlight_on_code": "001",
            "freeze_code": "000",
            "touch_code": "001",
            "power_lock_code": "000",
            "button_lock_code": "000",
            "menu_lock_code": "000",
            "rcu_mode_code": "001",
            "pip_mode_code": "000",
            "pip_input_code": "004",
            "tiling_mode_code": "000",
            "tiling_comp_code": "000",
            "tiling_hv": "011",
            "tiling_pos_code": "001",
            "thermal_c": 42,
            "operation_hours": 1234,
            "device_name": "CDE5530",
            "mac_address": "04:0e:c2:12:34:56",
            "ip_address": "192.168.1.50",
            "serial_number": "ABC180212345",
            "firmware_version": "3.02.001",
            "hub_temp": "023.5",
            "hub_humidity": "045.0",
            "hub_light": "00080",
            "hub_pir": "00001",
        },
        "controls": [
            {"type": "select", "key": "power_code", "label": "Power (001=on, 000=standby)",
             "options": ["000", "001"]},
            {"type": "select", "key": "input_code", "label": "Input Code",
             "options": sorted(INPUT_CODES)},
            {"type": "select", "key": "signal_code", "label": "Signal Detected (1=yes)",
             "options": ["0", "1"]},
            {"type": "slider", "key": "volume", "label": "Volume", "min": 0, "max": 100, "step": 1},
            {"type": "select", "key": "mute_code", "label": "Mute (001=muted)",
             "options": ["000", "001"]},
            {"type": "slider", "key": "brightness", "label": "Brightness", "min": 0, "max": 100, "step": 1},
            {"type": "slider", "key": "backlight", "label": "Backlight", "min": 0, "max": 100, "step": 1},
            {"type": "slider", "key": "thermal_c", "label": "Temperature (C)", "min": -20, "max": 100, "step": 1},
            {"type": "select", "key": "hub_pir", "label": "Smart Hub PIR (00001=presence)",
             "options": ["00000", "00001"]},
            {"type": "indicator", "key": "device_name", "label": "Model"},
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None) -> None:
        super().__init__(device_id, config)
        self._monitor_id = int((config or {}).get("monitor_id", 1) or 1)
        self._suppress_push = False

    # ── Frame helpers ──────────────────────────────────────────────────────

    def _ack(self, ok: bool) -> bytes:
        body = f"{self._monitor_id:02d}".encode() + (b"+" if ok else b"-")
        return bytes([0x30 + len(body) + 1]) + body + b"\r"

    def _reply(self, code: str, value: str) -> bytes:
        body = (f"{self._monitor_id:02d}" + "r" + code + value).encode("ascii")
        return bytes([0x30 + len(body) + 1]) + body + b"\r"

    def _reply32(self, code: str, value: str) -> bytes:
        # The "32-byte format": header, ASCII payload, NUL padding to a
        # fixed 32 bytes including the CR. The length byte is the
        # spec-pictured '2'; the driver parses positionally either way.
        header = ("2" + f"{self._monitor_id:02d}" + "r" + code).encode("ascii")
        payload = value.encode("ascii")[:26]
        return header + payload + b"\x00" * (26 - len(payload)) + b"\r"

    # ── Auto-reply push (*3.2.1) ───────────────────────────────────────────

    def set_state(self, key: str, value) -> None:
        changed = self.get_state(key) != value
        super().set_state(key, value)
        if not changed or self._suppress_push or key not in AUTO_REPLY_KEYS:
            return
        frame = self._auto_reply_frame(key)
        if frame is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (e.g. synchronous tests): nothing to push to
        loop.create_task(self.push(frame))

    def _auto_reply_frame(self, key: str) -> bytes | None:
        code = AUTO_REPLY_KEYS[key]
        if key == "input_code":
            return self._reply(code, self._input_reply())
        value = self.state[key]
        if key in ("volume", "brightness", "backlight"):
            return self._reply(code, f"{int(value):03d}")
        return self._reply(code, str(value))

    def _input_reply(self) -> str:
        return str(self.state["signal_code"]) + str(self.state["input_code"])[1:]

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
        try:
            ftype = frame[3:4].decode("ascii")
            code = frame[4:5].decode("ascii")
            payload = frame[5:].decode("ascii", errors="replace")
        except UnicodeDecodeError:
            return self._ack(False)

        # Controller-driven changes don't trigger the user-change push.
        self._suppress_push = True
        try:
            if ftype == "s":
                return self._handle_set(code, payload)
            if ftype == "g":
                return self._handle_get(code, payload)
            if ftype == "A" and code == "B":
                return self._handle_backlight_set(payload)
            if ftype == "a" and code == "B":
                if self.state["power_code"] != "001":
                    return self._ack(False)
                return self._reply("B", f"{int(self.state['backlight']):03d}")
            return self._ack(False)
        finally:
            self._suppress_push = False

    # ── Sets ───────────────────────────────────────────────────────────────

    def _handle_backlight_set(self, value: str) -> bytes:
        if self.state["power_code"] != "001":
            return self._ack(False)
        if not value.isdigit() or not 0 <= int(value) <= 100:
            return self._ack(False)
        self.set_state("backlight", int(value))
        return self._ack(True)

    def _handle_set(self, code: str, value: str) -> bytes:
        # In standby only the power function answers (real units may go
        # further and drop LAN entirely, depending on their standby mode).
        if self.state["power_code"] == "000" and code != "!":
            return self._ack(False)

        if code == "!":  # power
            if value not in ("000", "001"):
                return self._ack(False)
            self.set_state("power_code", value)
            return self._ack(True)

        if code == '"':  # input select
            if value == "00Z":
                current = str(self.state["input_code"])
                try:
                    idx = INPUT_CYCLE_ORDER.index(current)
                except ValueError:
                    idx = -1
                self.set_state("input_code", INPUT_CYCLE_ORDER[(idx + 1) % len(INPUT_CYCLE_ORDER)])
                return self._ack(True)
            if value not in INPUT_CODES:
                return self._ack(False)
            self.set_state("input_code", value)
            return self._ack(True)

        if code in ("$", "5") and value in ("900", "901"):
            key = "brightness" if code == "$" else "volume"
            delta = 1 if value == "901" else -1
            self.set_state(key, max(0, min(100, int(self.state[key]) + delta)))
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

        if code == "7":  # PIP input
            if value not in INPUT_CODES:
                return self._ack(False)
            self.set_state("pip_input_code", value)
            return self._ack(True)

        if code == "=":  # Function On_Off: [1/0][function id]
            state_key = FUNCTION_IDS.get(value[1:3]) if len(value) == 3 else None
            if state_key is None or value[0] not in ("0", "1"):
                return self._ack(False)
            self.set_state(state_key, "001" if value[0] == "1" else "000")
            return self._ack(True)

        if code == "R":  # tiling H x V
            if len(value) == 3 and value[0] == "0" and value[1:].isdigit() \
                    and "1" <= value[1] <= "9" and "1" <= value[2] <= "9":
                self.set_state("tiling_hv", value)
                return self._ack(True)
            return self._ack(False)

        if code == "S":  # tiling position
            if value.isdigit() and 1 <= int(value) <= 25:
                self.set_state("tiling_pos_code", value)
                return self._ack(True)
            return self._ack(False)

        if code == "A":  # keypad nav
            return self._ack(value in {f"00{i}" for i in range(8)})

        if code == "@":  # number key
            return self._ack(value.isdigit() and 0 <= int(value) <= 9)

        if code == "X":  # customized hot key
            return self._ack(value.isdigit() and 1 <= int(value) <= 999)

        if code in ACK_ONLY_SETS:
            return self._ack(value in ACK_ONLY_SETS[code])

        return self._ack(False)

    # ── Gets ───────────────────────────────────────────────────────────────

    def _handle_get(self, code: str, payload: str) -> bytes:
        if code == "z":  # communication-link test answers in any state
            return self._reply("z", "000")
        if code == "l":
            return self._reply("l", str(self.state["power_code"]))
        if self.state["power_code"] == "000":
            return self._ack(False)

        if code == "j":
            return self._reply("j", self._input_reply())
        if code in NUMERIC_GETS:
            return self._reply(code, f"{int(self.state[NUMERIC_GETS[code]]):03d}")
        if code in ENUM_BY_GET:
            _set, _get, key, _allowed = ENUM_BY_GET[code]
            return self._reply(code, str(self.state[key]))
        if code == "u":
            return self._reply("u", str(self.state["pip_input_code"]))
        if code == "x":
            return self._reply("x", str(self.state["tiling_hv"]))
        if code == "y":
            if self.state["tiling_mode_code"] != "001":
                return self._reply("y", "000")
            return self._reply("y", f"{int(self.state['tiling_pos_code']):03d}")
        if code == "=":
            state_key = FUNCTION_IDS.get(payload[1:3]) if len(payload) == 3 else None
            if state_key is None:
                return self._ack(False)
            on = str(self.state[state_key]) == "001"
            return self._reply("=", ("1" if on else "0") + payload[1:3])
        if code == "0":
            temp = int(self.state["thermal_c"])
            value = f"{temp:03d}" if temp >= 0 else f"-{abs(temp):02d}"
            return self._reply("0", value)
        if code == "1":
            return self._reply32("1", f"{int(self.state['operation_hours']):06d}")
        if code == "4":
            return self._reply32("4", str(self.state["device_name"]))
        if code == "5":
            mac = str(self.state["mac_address"]).replace(":", "").lower()
            return self._reply32("5", mac)
        if code == "6":
            return self._reply32("6", str(self.state["ip_address"]))
        if code == "7":
            return self._reply32("7", str(self.state["serial_number"]))
        if code == "8":
            return self._reply32("8", str(self.state["firmware_version"]))
        if code == ":":
            fields = {
                "00A": "A" + str(self.state["hub_temp"]),
                "00B": "B" + str(self.state["hub_humidity"]),
                "00C": "C" + str(self.state["hub_light"]),
                "00D": "D" + str(self.state["hub_pir"]),
            }
            if payload == "000":
                return self._reply32(":", "".join(fields.values()))
            if payload in fields:
                return self._reply32(":", fields[payload])
            return self._ack(False)

        return self._ack(False)
