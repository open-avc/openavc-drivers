"""
Sharp/NEC Large-Format Display — External Control Simulator.

Implements the display side of the NEC External Control protocol on
TCP 7142: SOH-framed ASCII-hex packets with an XOR block check and CR
delimiter. Faithful to the manual's message set:

- Header addressing by Monitor ID (1-100 -> 0x41..0xA4); the sim
  models a daisy chain — one socket, several Monitor IDs (config
  ``display_ids``), each with independent state. Packets addressed to
  an ID that is not on the chain get NO reply (matches a real chain,
  and exercises the driver's timeout path).
- Get parameter ('C' -> 'D') and Set parameter ('E' -> 'F') with the
  documented reply layout (result, opcode, type, max, current value).
  Unknown opcodes answer result code 01 (Unsupported).
- Commands ('A' -> 'B'): power status read (01D6), power control
  (C203D6), self-diagnosis (B1 -> A1 + status codes), serial number
  (C216 -> C316, hex-pair encoded), model name (C217 -> C317), and
  save current settings (0C).
- Bad check codes are dropped without a reply (real monitors ignore
  corrupt packets).
- The INPUT opcode accepts the documented "HDMI (Set only)" alias 4
  but reports 17 on reads, exercising the driver's dual mapping.

Driver side: ``displays/sharp_nec_display.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)


SOH = 0x01
STX = 0x02
ETX = 0x03

# Opcodes the simulated panel supports: (page, code) -> state key.
OPCODE_KEYS = {
    (0x00, 0x60): "input",
    (0x00, 0x62): "volume",
    (0x00, 0x8D): "mute",
    (0x10, 0xB6): "video_mute",
    (0x00, 0x10): "backlight",
    (0x00, 0x92): "brightness",
    (0x00, 0x12): "contrast",
    (0x00, 0x8C): "sharpness",
    (0x02, 0x70): "aspect",
    (0x02, 0x1A): "picture_mode",
    (0x02, 0x78): "temp_sensor",
    (0x02, 0x79): "temperature_raw",  # read-only
}

# Max values reported in Get/Set replies, per the opcode table.
OPCODE_MAX = {
    "input": 128,
    "volume": 100,
    "mute": 2,
    "video_mute": 2,
    "backlight": 100,
    "brightness": 100,
    "contrast": 100,
    "sharpness": 24,
    "aspect": 7,
    "picture_mode": 9,
    "temp_sensor": 3,
    "temperature_raw": 0xFFFF,
}

READ_ONLY = {"temperature_raw"}

# The set-only HDMI alias: setting 4 lands the panel on HDMI, which is
# reported as 17 on reads.
HDMI_SET_ALIAS = 4
HDMI_READ_CODE = 17


def _encode_monitor_id(monitor_id: int) -> int:
    return 0x41 + monitor_id - 1


def _decode_monitor_id(byte: int) -> int | None:
    if 0x41 <= byte <= 0xA4:
        return byte - 0x40
    return None


def _bcc(body: bytes) -> int:
    value = 0
    for b in body:
        value ^= b
    return value


def _reply(monitor_id: int, msg_type: bytes, message: bytes) -> bytes:
    """Assemble a monitor->controller packet (dest '0', source = ID)."""
    header = (
        b"0"
        + b"0"
        + bytes([_encode_monitor_id(monitor_id)])
        + msg_type
        + f"{len(message):02X}".encode("ascii")
    )
    body = header + message
    return bytes([SOH]) + body + bytes([_bcc(body)]) + b"\r"


def _hex_pairs(text: str) -> bytes:
    """Encode a string the way serial/model replies do (12.1): each byte
    of the ASCII string becomes two ASCII-hex characters."""
    return text.encode("ascii").hex().upper().encode("ascii")


def _extract_frame(buf: bytes) -> tuple[bytes | None, bytes]:
    """Length-aware packet extraction (the BCC byte can equal CR, so
    delimiter splitting is unsound — same contract as the driver side:
    garbage is consumed via empty-message returns)."""
    if not buf:
        return None, buf
    start = buf.find(bytes([SOH]))
    if start < 0:
        return b"", b""
    if start > 0:
        return b"", buf[start:]
    if len(buf) < 7:
        return None, buf
    try:
        msg_len = int(buf[5:7], 16)
    except ValueError:
        return b"", buf[1:]
    total = 7 + msg_len + 2  # header + message + BCC + CR
    if len(buf) < total:
        return None, buf
    return buf[:total - 1], buf[total:]


class SharpNECDisplaySimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "sharp_nec_display",
        "name": "Sharp/NEC Display Simulator",
        "category": "display",
        "transport": "tcp",
        "default_port": 7142,
        "initial_state": {
            # Prefixed per-display state is created in __init__ from
            # display_ids; these are the chain-wide extras.
            "display_ids": "1",
        },
        "controls": [
            {
                "type": "select",
                "key": "d1_power",
                "options": ["on", "standby", "suspend", "off"],
                "label": "Display 1 Power",
            },
            {
                "type": "select",
                "key": "d1_input",
                "options": ["3", "5", "15", "17", "18"],
                "label": "Display 1 Input Code",
            },
            {
                "type": "slider",
                "key": "d1_volume",
                "min": 0,
                "max": 100,
                "step": 1,
                "label": "Display 1 Volume",
            },
            {
                "type": "slider",
                "key": "d1_temperature",
                "min": -10,
                "max": 90,
                "step": 0.5,
                "label": "Display 1 Temp (°C)",
            },
            {"type": "indicator", "key": "d1_diagnosis", "label": "Diag"},
        ],
        "delays": {"command_response": 0.005},
        "error_modes": {
            "fan_fault": {
                "description": (
                    "Self-diagnosis reports a cooling fan 1 abnormality "
                    "(code 80) on every display until cleared"
                ),
                "set_state": {"force_diag_code": "80"},
            },
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # Raw (no-delimiter) mode: packets are length-framed because the
        # BCC byte can collide with the CR delimiter.
        self._delimiter = None
        self._line_mode = False
        self._rx = b""
        ids_raw = str(self.config.get("display_ids", "1"))
        self._ids: list[int] = []
        for part in ids_raw.replace(";", ",").split(","):
            part = part.strip()
            if part.isdigit() and 1 <= int(part) <= 100:
                self._ids.append(int(part))
        if not self._ids:
            self._ids = [1]

        for monitor_id in self._ids:
            p = f"d{monitor_id}_"
            defaults: dict[str, Any] = {
                p + "power": "standby",
                p + "input": 17,           # HDMI (canonical read code)
                p + "volume": 30,
                p + "mute": 2,             # 1 = muted, 2 = unmuted
                p + "video_mute": 2,
                p + "backlight": 70,
                p + "brightness": 50,
                p + "contrast": 50,
                p + "sharpness": 12,
                p + "aspect": 2,           # FULL
                p + "picture_mode": 4,     # STANDARD
                p + "temp_sensor": 1,
                p + "temperature": 38.5,
                p + "diagnosis": "00",
                p + "model": f"P554-{monitor_id}",
                p + "serial": f"8Z0{monitor_id:05d}",
                p + "saved": False,
            }
            for key, value in defaults.items():
                # NB: BaseSimulator.state returns a COPY — mutate through
                # set_state, never through the property.
                if self.get_state(key) is None:
                    self.set_state(key, value)

    # ── Dispatch ──

    def handle_command(self, data: bytes) -> bytes | None:
        """Buffer raw chunks, extract complete packets, reply to each."""
        self._rx += bytes(data)
        out = b""
        while True:
            packet, self._rx = _extract_frame(self._rx)
            if packet is None:
                break
            reply = self._handle_packet(packet)
            if reply:
                out += reply
        return out or None

    def _handle_packet(self, packet: bytes) -> bytes | None:
        if len(packet) < 9 or packet[0] != SOH:
            return None
        body = packet[1:-1]
        if _bcc(body) != packet[-1]:
            return None  # corrupt packet: real monitors stay silent
        monitor_id = _decode_monitor_id(packet[2])
        if monitor_id is None or monitor_id not in self._ids:
            return None  # not on this chain: no reply (driver times out)
        msg_type = chr(packet[4])
        try:
            msg_len = int(packet[5:7].decode("ascii"), 16)
        except (ValueError, UnicodeDecodeError):
            return None
        message = packet[7:7 + msg_len]
        if (
            msg_len < 2
            or len(message) != msg_len
            or message[0] != STX
            or message[-1] != ETX
        ):
            return None
        body = message[1:-1]

        if msg_type == "C":
            return self._handle_get(monitor_id, body)
        if msg_type == "E":
            return self._handle_set(monitor_id, body)
        if msg_type == "A":
            return self._handle_command_msg(monitor_id, body)
        return None

    def _key(self, monitor_id: int, name: str) -> str:
        return f"d{monitor_id}_{name}"

    # ── Get / Set parameter ──

    def _parse_op(self, body: bytes) -> tuple[int, int] | None:
        try:
            return int(body[0:2], 16), int(body[2:4], 16)
        except ValueError:
            return None

    def _param_reply(
        self, monitor_id: int, msg_type: bytes, page: int, code: int,
        result: int, value: int,
    ) -> bytes:
        key = OPCODE_KEYS.get((page, code), "")
        max_value = OPCODE_MAX.get(key, 0)
        message = (
            bytes([STX])
            + f"{result:02X}".encode("ascii")
            + f"{page:02X}{code:02X}".encode("ascii")
            + b"00"
            + f"{max_value & 0xFFFF:04X}".encode("ascii")
            + f"{value & 0xFFFF:04X}".encode("ascii")
            + bytes([ETX])
        )
        return _reply(monitor_id, msg_type, message)

    def _handle_get(self, monitor_id: int, body: bytes) -> bytes | None:
        op = self._parse_op(body)
        if op is None:
            return None
        page, code = op
        key = OPCODE_KEYS.get((page, code))
        if key is None:
            return self._param_reply(monitor_id, b"D", page, code, 1, 0)
        if key == "temperature_raw":
            temp = float(self.state.get(self._key(monitor_id, "temperature"), 25.0))
            raw = int(round(temp * 2)) & 0xFFFF
            return self._param_reply(monitor_id, b"D", page, code, 0, raw)
        value = int(self.state.get(self._key(monitor_id, key), 0))
        return self._param_reply(monitor_id, b"D", page, code, 0, value)

    def _handle_set(self, monitor_id: int, body: bytes) -> bytes | None:
        op = self._parse_op(body)
        if op is None or len(body) < 8:
            return None
        page, code = op
        try:
            value = int(body[4:8], 16)
        except ValueError:
            return None
        key = OPCODE_KEYS.get((page, code))
        if key is None or key in READ_ONLY:
            return self._param_reply(monitor_id, b"F", page, code, 1, value)
        if value > OPCODE_MAX.get(key, 0xFFFF):
            return self._param_reply(monitor_id, b"F", page, code, 1, value)
        if key == "input" and value == HDMI_SET_ALIAS:
            value = HDMI_READ_CODE
        self.set_state(self._key(monitor_id, key), value)
        return self._param_reply(monitor_id, b"F", page, code, 0, value)

    # ── Commands ──

    def _handle_command_msg(
        self, monitor_id: int, body: bytes,
    ) -> bytes | None:
        power_key = self._key(monitor_id, "power")

        if body == b"01D6":
            mode = {"on": 1, "standby": 2, "suspend": 3, "off": 4}[
                str(self.state.get(power_key, "standby"))
            ]
            message = (
                bytes([STX]) + b"0200D600" + b"0004"
                + f"{mode:04X}".encode("ascii") + bytes([ETX])
            )
            return _reply(monitor_id, b"B", message)

        if body.startswith(b"C203D6") and len(body) >= 10:
            try:
                mode = int(body[6:10], 16)
            except ValueError:
                return None
            if mode == 1:
                self.set_state(power_key, "on")
            elif mode == 4:
                self.set_state(power_key, "off")
            else:
                # 2/3 are documented "Do not set".
                message = (
                    bytes([STX]) + b"01C203D6"
                    + f"{mode:04X}".encode("ascii") + bytes([ETX])
                )
                return _reply(monitor_id, b"B", message)
            message = (
                bytes([STX]) + b"00C203D6"
                + f"{mode:04X}".encode("ascii") + bytes([ETX])
            )
            return _reply(monitor_id, b"B", message)

        if body == b"B1":
            code = str(
                self.state.get("force_diag_code")
                or self.state.get(self._key(monitor_id, "diagnosis"), "00")
            )
            message = bytes([STX]) + b"A1" + code.encode("ascii") + bytes([ETX])
            return _reply(monitor_id, b"B", message)

        if body == b"C216":
            serial = str(self.state.get(self._key(monitor_id, "serial"), ""))
            message = bytes([STX]) + b"C316" + _hex_pairs(serial) + bytes([ETX])
            return _reply(monitor_id, b"B", message)

        if body == b"C217":
            model = str(self.state.get(self._key(monitor_id, "model"), ""))
            message = bytes([STX]) + b"C317" + _hex_pairs(model) + bytes([ETX])
            return _reply(monitor_id, b"B", message)

        if body == b"0C":
            self.set_state(self._key(monitor_id, "saved"), True)
            message = bytes([STX]) + b"000C" + bytes([ETX])
            return _reply(monitor_id, b"B", message)

        return None
