"""
Generic VISCA-over-IP PTZ Camera — Simulator.

Implements the Sony-spec wire format on UDP 52381:

  - Each datagram is an 8-byte VISCA-over-IP header + 1..16 bytes of
    VISCA payload. The simulator unwraps incoming datagrams, dispatches
    the inner VISCA bytes (`81 01 ...` for commands, `81 09 ...` for
    inquiries), and wraps replies in the 0x0111 VISCA-reply header.
  - Replies to a Control RESET (0x0200 / 0x01) with a Control reply
    (0x0201 / 0x01) so the driver's connect handshake completes.
  - Honors the stable PTZ command surface: power, pan/tilt drive (continuous +
    absolute + home + reset), zoom (variable + direct + stop), focus
    (variable + direct + stop + mode + one-push), presets (set/recall/
    reset 0-127), AE mode, WB mode, backlight, WB one-push trigger.

The VISCA byte dispatch is a focused subset of the PTZOptics simulator's
logic — same protocol family, smaller command surface (no flip / lr_reverse
/ picture-flip / tally / save_settings), with the IP wrapper added.

Driver side: ``cameras/visca_ip.py``.
"""

from __future__ import annotations

import logging
import struct
from typing import Any

from openavc.simulator.udp_simulator import UDPSimulator

logger = logging.getLogger(__name__)


# ── VISCA-over-IP wire constants ──

PAYLOAD_VISCA_COMMAND = 0x0100
PAYLOAD_VISCA_INQUIRY = 0x0110
PAYLOAD_VISCA_REPLY = 0x0111
PAYLOAD_CONTROL_CMD = 0x0200
PAYLOAD_CONTROL_REPLY = 0x0201


def _wrap(payload: bytes, payload_type: int, sequence: int) -> bytes:
    return struct.pack(">HHI", payload_type, len(payload), sequence) + payload


def _unwrap(packet: bytes) -> tuple[int, int, bytes] | None:
    if len(packet) < 9:
        return None
    payload_type, payload_length, sequence = struct.unpack(">HHI", packet[:8])
    payload = packet[8:]
    if len(payload) != payload_length:
        return None
    return payload_type, sequence, payload


def _encode_4nibble(value: int) -> bytes:
    v = value & 0xFFFF
    return bytes(
        [
            (v >> 12) & 0x0F,
            (v >> 8) & 0x0F,
            (v >> 4) & 0x0F,
            v & 0x0F,
        ]
    )


def _decode_4nibble(data: bytes, signed: bool = False) -> int:
    v = (
        ((data[0] & 0x0F) << 12)
        | ((data[1] & 0x0F) << 8)
        | ((data[2] & 0x0F) << 4)
        | (data[3] & 0x0F)
    )
    if signed and v >= 0x8000:
        v -= 0x10000
    return v


_AE_MODE_TO_BYTE = {
    "full_auto": 0x00,
    "manual":    0x03,
    "shutter":   0x0A,
    "iris":      0x0B,
    "bright":    0x0D,
}
_BYTE_TO_AE_MODE = {v: k for k, v in _AE_MODE_TO_BYTE.items()}

_WB_MODE_TO_BYTE = {
    "auto1":    0x00,
    "indoor":   0x01,
    "outdoor":  0x02,
    "one_push": 0x03,
    "auto2":    0x04,
    "manual":   0x05,
}
_BYTE_TO_WB_MODE = {v: k for k, v in _WB_MODE_TO_BYTE.items()}


# Pan/tilt position envelope used by the sim — typical SRG/BRC values.
_PAN_MIN, _PAN_MAX = -2448, 2448      # ±170° at the documented step size
_TILT_MIN, _TILT_MAX = -432, 1296     # -30° to +90° on a typical PTZ
_ZOOM_MAX = 0x4000                    # 16384 = 30x optical


def _ack_completion() -> bytes:
    """ACK + Completion concatenated (driver line parser splits on 0xFF)."""
    return b"\x90\x41\xff\x90\x51\xff"


class VISCAIPSimulator(UDPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "visca_ip",
        "name": "Generic VISCA-IP Camera Simulator",
        "category": "camera",
        "transport": "udp",
        "default_port": 52381,
        "initial_state": {
            "power": "on",
            "pan_position": 0,
            "tilt_position": 0,
            "zoom_position": 0,
            "focus_position": 0x1000,
            "focus_mode": "auto",
            "ae_mode": "full_auto",
            "wb_mode": "auto1",
            "backlight": False,
        },
        "delays": {"command_response": 0.005},
        "controls": [
            {"type": "indicator", "key": "pan_position", "label": "Pan"},
            {"type": "indicator", "key": "tilt_position", "label": "Tilt"},
            {"type": "indicator", "key": "zoom_position", "label": "Zoom"},
            {"type": "indicator", "key": "focus_position", "label": "Focus"},
            {
                "type": "select",
                "key": "ae_mode",
                "label": "AE Mode",
                "options": ["full_auto", "manual", "shutter", "iris", "bright"],
            },
            {
                "type": "select",
                "key": "wb_mode",
                "label": "White Balance",
                "options": ["auto1", "indoor", "outdoor", "one_push", "auto2", "manual"],
            },
            {"type": "power", "key": "power", "label": "Power"},
            {"type": "toggle", "key": "backlight", "label": "Backlight"},
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._presets: dict[int, dict[str, Any]] = {}
        # Each transport callsite re-uses incoming sequence numbers for the
        # matching reply. We stash the last one here per-handler.
        self._last_sequence = 0

    def handle_command(self, data: bytes) -> bytes | None:
        unwrapped = _unwrap(data)
        if unwrapped is None:
            return None
        payload_type, sequence, payload = unwrapped
        self._last_sequence = sequence

        # Control RESET — clear sequence state, ack with Control reply.
        if payload_type == PAYLOAD_CONTROL_CMD:
            if payload == b"\x01":
                return _wrap(b"\x01", PAYLOAD_CONTROL_REPLY, sequence)
            # Unknown control payload — surface the spec's error reply form.
            return _wrap(b"\x0F\x02", PAYLOAD_CONTROL_REPLY, sequence)

        # VISCA command or inquiry.
        if payload_type in (PAYLOAD_VISCA_COMMAND, PAYLOAD_VISCA_INQUIRY):
            visca_reply = self._dispatch_visca(payload)
            if visca_reply is None:
                return None
            return _wrap(visca_reply, PAYLOAD_VISCA_REPLY, sequence)

        return None

    # ── VISCA byte-level dispatch ──

    def _dispatch_visca(self, packet: bytes) -> bytes | None:
        if len(packet) < 3 or packet[0] != 0x81 or packet[-1] != 0xFF:
            return b"\x90\x60\x02\xff"  # syntax error

        body = packet[1:-1]
        if not body:
            return b"\x90\x60\x02\xff"

        try:
            if body[0] == 0x09:
                return self._handle_inquiry(body[1:])
            if body[0] == 0x01:
                return self._handle_action(body[1:])
        except Exception:
            logger.exception("visca_ip_sim: error handling %s", packet.hex())
            return b"\x90\x60\x41\xff"

        return b"\x90\x60\x02\xff"

    def _handle_action(self, b: bytes) -> bytes | None:
        if not b:
            return b"\x90\x60\x02\xff"

        if b[0] == 0x04 and len(b) >= 2:
            return self._handle_cam(b[1:])
        if b[0] == 0x06 and len(b) >= 2:
            return self._handle_pt(b[1:])
        return b"\x90\x60\x02\xff"

    def _handle_cam(self, b: bytes) -> bytes | None:
        if not b:
            return b"\x90\x60\x02\xff"
        op = b[0]
        rest = b[1:]

        # Power: 00 02|03
        if op == 0x00 and len(rest) >= 1:
            self.set_state("power", "on" if rest[0] == 0x02 else "standby")
            return _ack_completion()

        # Zoom: 07 XX
        if op == 0x07 and len(rest) >= 1:
            sub = rest[0]
            if sub == 0x02:
                self._step_zoom(+512)
            elif sub == 0x03:
                self._step_zoom(-512)
            elif (sub & 0xF0) == 0x20:
                self._step_zoom(+128 * ((sub & 0x0F) + 1))
            elif (sub & 0xF0) == 0x30:
                self._step_zoom(-128 * ((sub & 0x0F) + 1))
            return _ack_completion()

        # Zoom Direct: 47 0p 0q 0r 0s
        if op == 0x47 and len(rest) >= 4:
            self.set_state("zoom_position", _decode_4nibble(rest[:4]))
            return _ack_completion()

        # Focus: 08 XX
        if op == 0x08 and len(rest) >= 1:
            sub = rest[0]
            if sub == 0x02:
                self._step_focus(+256)
            elif sub == 0x03:
                self._step_focus(-256)
            elif (sub & 0xF0) == 0x20:
                self._step_focus(+64 * ((sub & 0x0F) + 1))
            elif (sub & 0xF0) == 0x30:
                self._step_focus(-64 * ((sub & 0x0F) + 1))
            return _ack_completion()

        # Focus Direct: 48 0p 0q 0r 0s
        if op == 0x48 and len(rest) >= 4:
            self.set_state("focus_position", _decode_4nibble(rest[:4]))
            return _ack_completion()

        # Focus mode: 38 02|03|10
        if op == 0x38 and len(rest) >= 1:
            if rest[0] == 0x02:
                self.set_state("focus_mode", "auto")
            elif rest[0] == 0x03:
                self.set_state("focus_mode", "manual")
            elif rest[0] == 0x10:
                cur = self.get_state("focus_mode", "auto")
                self.set_state("focus_mode", "manual" if cur == "auto" else "auto")
            return _ack_completion()

        # Focus one-push trigger: 18 01
        if op == 0x18 and len(rest) >= 1 and rest[0] == 0x01:
            return _ack_completion()

        # WB mode: 35 XX
        if op == 0x35 and len(rest) >= 1:
            mode = _BYTE_TO_WB_MODE.get(rest[0])
            if mode:
                self.set_state("wb_mode", mode)
            return _ack_completion()

        # WB one-push trigger: 10 05
        if op == 0x10 and len(rest) >= 1 and rest[0] == 0x05:
            return _ack_completion()

        # AE mode: 39 XX
        if op == 0x39 and len(rest) >= 1:
            mode = _BYTE_TO_AE_MODE.get(rest[0])
            if mode:
                self.set_state("ae_mode", mode)
            return _ack_completion()

        # Backlight: 33 02|03
        if op == 0x33 and len(rest) >= 1:
            self.set_state("backlight", rest[0] == 0x02)
            return _ack_completion()

        # Presets: 3F 00|01|02 pp (0-0x7F; extended-VISCA models take the full byte)
        if op == 0x3F and len(rest) >= 2:
            sub, num = rest[0], rest[1]
            if 0 <= num <= 127:
                if sub == 0x00:
                    self._presets.pop(num, None)
                elif sub == 0x01:
                    self._presets[num] = {
                        "pan": self.get_state("pan_position", 0),
                        "tilt": self.get_state("tilt_position", 0),
                        "zoom": self.get_state("zoom_position", 0),
                        "focus": self.get_state("focus_position", 0),
                    }
                elif sub == 0x02:
                    p = self._presets.get(num)
                    if p:
                        self.set_state("pan_position", p["pan"])
                        self.set_state("tilt_position", p["tilt"])
                        self.set_state("zoom_position", p["zoom"])
                        self.set_state("focus_position", p["focus"])
                return _ack_completion()

        return b"\x90\x60\x02\xff"

    def _handle_pt(self, b: bytes) -> bytes | None:
        if not b:
            return b"\x90\x60\x02\xff"
        op = b[0]
        rest = b[1:]

        # PT drive: 01 vv ww p t
        if op == 0x01 and len(rest) >= 4:
            pan_speed, tilt_speed, pan_dir, tilt_dir = rest[:4]
            self._step_pt(pan_dir, tilt_dir, pan_speed, tilt_speed)
            return _ack_completion()

        # Absolute: 02 vv ww 0Y 0Y 0Y 0Y 0Z 0Z 0Z 0Z
        if op == 0x02 and len(rest) >= 10:
            pan = _decode_4nibble(rest[2:6], signed=True)
            tilt = _decode_4nibble(rest[6:10], signed=True)
            self.set_state("pan_position", max(_PAN_MIN, min(_PAN_MAX, pan)))
            self.set_state("tilt_position", max(_TILT_MIN, min(_TILT_MAX, tilt)))
            return _ack_completion()

        # Home: 04
        if op == 0x04:
            self.set_state("pan_position", 0)
            self.set_state("tilt_position", 0)
            return _ack_completion()

        # Reset: 05
        if op == 0x05:
            self.set_state("pan_position", 0)
            self.set_state("tilt_position", 0)
            return _ack_completion()

        return b"\x90\x60\x02\xff"

    def _handle_inquiry(self, b: bytes) -> bytes:
        if len(b) < 2:
            return b"\x90\x60\x02\xff"

        # CAM_xxx: 04 op
        if b[0] == 0x04:
            return self._inquiry_cam(b[1])

        # PT position: 06 12
        if b[0] == 0x06 and b[1] == 0x12:
            pan = self.get_state("pan_position", 0)
            tilt = self.get_state("tilt_position", 0)
            return (
                b"\x90\x50"
                + _encode_4nibble(pan)
                + _encode_4nibble(tilt)
                + b"\xff"
            )

        return b"\x90\x60\x02\xff"

    def _inquiry_cam(self, op: int) -> bytes:
        if op == 0x00:  # CAM_PowerInq
            on = self.get_state("power", "on") == "on"
            return b"\x90\x50" + (b"\x02" if on else b"\x03") + b"\xff"
        if op == 0x47:  # zoom position
            return b"\x90\x50" + _encode_4nibble(
                self.get_state("zoom_position", 0)
            ) + b"\xff"
        if op == 0x48:  # focus position
            return b"\x90\x50" + _encode_4nibble(
                self.get_state("focus_position", 0)
            ) + b"\xff"
        if op == 0x38:  # focus mode
            mode = self.get_state("focus_mode", "auto")
            return b"\x90\x50" + (b"\x02" if mode == "auto" else b"\x03") + b"\xff"
        if op == 0x35:  # WB mode
            mode = self.get_state("wb_mode", "auto1")
            return bytes([0x90, 0x50, _WB_MODE_TO_BYTE.get(mode, 0x00), 0xFF])
        if op == 0x39:  # AE mode
            mode = self.get_state("ae_mode", "full_auto")
            return bytes([0x90, 0x50, _AE_MODE_TO_BYTE.get(mode, 0x00), 0xFF])
        if op == 0x33:  # backlight
            on = bool(self.get_state("backlight", False))
            return b"\x90\x50" + (b"\x02" if on else b"\x03") + b"\xff"
        return b"\x90\x60\x02\xff"

    # ── State helpers ──

    def _step_zoom(self, delta: int) -> None:
        cur = self.get_state("zoom_position", 0)
        self.set_state("zoom_position", max(0, min(_ZOOM_MAX, cur + delta)))

    def _step_focus(self, delta: int) -> None:
        cur = self.get_state("focus_position", 0)
        self.set_state("focus_position", max(0, min(0xFFFF, cur + delta)))

    def _step_pt(self, pan_dir: int, tilt_dir: int, pan_speed: int, tilt_speed: int) -> None:
        dpan = 0
        dtilt = 0
        if pan_dir == 0x01:
            dpan = -max(1, pan_speed) * 8
        elif pan_dir == 0x02:
            dpan = +max(1, pan_speed) * 8
        if tilt_dir == 0x01:
            dtilt = +max(1, tilt_speed) * 8
        elif tilt_dir == 0x02:
            dtilt = -max(1, tilt_speed) * 8
        cur_pan = self.get_state("pan_position", 0)
        cur_tilt = self.get_state("tilt_position", 0)
        self.set_state(
            "pan_position", max(_PAN_MIN, min(_PAN_MAX, cur_pan + dpan))
        )
        self.set_state(
            "tilt_position", max(_TILT_MIN, min(_TILT_MAX, cur_tilt + dtilt))
        )
