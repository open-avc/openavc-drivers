"""
PTZOptics Camera — Simulator.

Implements the server side of the PTZOptics VISCA-over-IP protocol on
TCP 5678: parses bare ``81 ... FF`` packets, mutates state, and replies
with ACK + Completion for action commands or a single Completion-with-
data for inquiries.

Driver: ptzoptics
Transport: tcp (raw VISCA, 0xFF terminator)
"""

from __future__ import annotations

import logging
from typing import Any

from simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)


# Camera-specific limits modeled on PTZOptics Move-series specs. Real
# cameras vary; these are reasonable defaults so the simulator doesn't
# pretend to support an infinite range.
_PAN_MIN, _PAN_MAX = -2448, 2448
_TILT_MIN, _TILT_MAX = -1296, 1296
_ZOOM_MAX = 0x4000  # 16384 — full telephoto

_AE_MODE_FROM_BYTE = {
    0x00: "full_auto",
    0x03: "manual",
    0x0A: "shutter",
    0x0B: "iris",
    0x0D: "bright",
}
_AE_MODE_TO_BYTE = {v: k for k, v in _AE_MODE_FROM_BYTE.items()}

_WB_MODE_FROM_BYTE = {
    0x00: "auto",
    0x01: "indoor",
    0x02: "outdoor",
    0x03: "one_push",
    0x05: "manual",
    0x20: "color_temp",
}
_WB_MODE_TO_BYTE = {v: k for k, v in _WB_MODE_FROM_BYTE.items()}

_FLIP_FROM_BYTE = {0x00: "off", 0x01: "h", 0x02: "v", 0x03: "hv"}
_FLIP_TO_BYTE = {v: k for k, v in _FLIP_FROM_BYTE.items()}


def _decode_4nibble(data: bytes, signed: bool = False) -> int:
    val = (
        ((data[0] & 0x0F) << 12)
        | ((data[1] & 0x0F) << 8)
        | ((data[2] & 0x0F) << 4)
        | (data[3] & 0x0F)
    )
    if signed and (val & 0x8000):
        val -= 0x10000
    return val


def _encode_4nibble(val: int) -> bytes:
    if val < 0:
        val = (val + 0x10000) & 0xFFFF
    val &= 0xFFFF
    return bytes([
        (val >> 12) & 0x0F,
        (val >> 8) & 0x0F,
        (val >> 4) & 0x0F,
        val & 0x0F,
    ])


def _ack_completion() -> bytes:
    """Standard ACK + Completion reply for action commands."""
    # Socket 1 is fine; real cameras alternate but the driver doesn't
    # care which socket replies belong to.
    return b"\x90\x41\xff\x90\x51\xff"


class PTZOpticsSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "ptzoptics",
        "name": "PTZOptics Camera Simulator",
        "category": "camera",
        "transport": "tcp",
        "default_port": 5678,
        "initial_state": {
            "power": True,
            "pan_position": 0,
            "tilt_position": 0,
            "zoom_position": 0,
            "focus_position": 0x1000,
            "focus_mode": "auto",
            "ae_mode": "full_auto",
            "wb_mode": "auto",
            "backlight": False,
            "flip": "off",
            "lr_reverse": False,
            "picture_flip": False,
            "preset_speed": 24,
            "tally": "off",
        },
        "delays": {"command_response": 0.005},
        "error_modes": {
            "syntax_error": {
                "description": "Camera replies to every command with VISCA syntax error 0x02",
                "behavior": "syntax_error",
            },
        },
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
                "options": [
                    "auto", "indoor", "outdoor", "one_push", "manual", "color_temp"
                ],
            },
            {
                "type": "select",
                "key": "flip",
                "label": "Image Flip",
                "options": ["off", "h", "v", "hv"],
            },
            {"type": "indicator", "key": "tally", "label": "Tally"},
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # Frame on 0xFF — each handle_command call gets one VISCA packet
        # with the trailing 0xFF still attached.
        self._delimiter = b"\xff"
        self._line_mode = False
        self._presets: dict[int, dict[str, Any]] = {}

    # ── Per-message handling ──

    def handle_command(self, data: bytes) -> bytes | None:
        if self.has_error_behavior("syntax_error"):
            return b"\x90\x60\x02\xff"

        if len(data) < 3 or data[0] != 0x81 or data[-1] != 0xFF:
            return b"\x90\x60\x02\xff"  # syntax error

        body = data[1:-1]  # strip 0x81 prefix and 0xff suffix
        if not body:
            return b"\x90\x60\x02\xff"

        try:
            response = self._dispatch(body)
        except Exception:
            logger.exception("ptzoptics_sim: error handling %s", data.hex())
            return b"\x90\x60\x41\xff"  # not executable

        return response

    def _dispatch(self, body: bytes) -> bytes | None:
        # Inquiries: `09 ...`
        if body[0] == 0x09:
            return self._handle_inquiry(body[1:])

        # Action: `01 ...` or `0a ...` or `0b ...` or `2a ...`
        if body[0] == 0x01:
            return self._handle_action_01(body[1:])
        if body[0] == 0x0A:
            return self._handle_action_0a(body[1:])
        if body[0] == 0x0B:
            # NDI mode / multicast — accept but no state change here
            return _ack_completion()
        if body[0] == 0x2A:
            # UAC toggle — accept, no state
            return _ack_completion()

        return b"\x90\x60\x02\xff"  # syntax error

    # ── Action commands (`81 01 ...`) ──

    def _handle_action_01(self, b: bytes) -> bytes | None:
        # b = remaining bytes after the leading 0x01
        if not b:
            return b"\x90\x60\x02\xff"

        # CAM_xxx commands: `04 ...`
        if b[0] == 0x04 and len(b) >= 2:
            return self._handle_cam(b[1:])

        # Pan/Tilt: `06 ...`
        if b[0] == 0x06 and len(b) >= 2:
            return self._handle_pt(b[1:])

        return b"\x90\x60\x02\xff"

    def _handle_cam(self, b: bytes) -> bytes | None:
        if not b:
            return b"\x90\x60\x02\xff"

        op = b[0]
        rest = b[1:]

        # CAM_Power: `00 02|03`
        if op == 0x00 and len(rest) >= 1:
            self.set_state("power", rest[0] == 0x02)
            return _ack_completion()

        # CAM_Zoom: `07 XX`
        if op == 0x07 and len(rest) >= 1:
            sub = rest[0]
            if sub == 0x00:
                pass  # stop
            elif sub == 0x02:
                self._step_zoom(+512)
            elif sub == 0x03:
                self._step_zoom(-512)
            elif (sub & 0xF0) == 0x20:
                self._step_zoom(+128 * ((sub & 0x0F) + 1))
            elif (sub & 0xF0) == 0x30:
                self._step_zoom(-128 * ((sub & 0x0F) + 1))
            return _ack_completion()

        # CAM_Zoom Direct: `47 0p 0q 0r 0s`
        if op == 0x47 and len(rest) >= 4:
            self.set_state("zoom_position", _decode_4nibble(rest[:4]))
            return _ack_completion()

        # CAM_Focus: `08 XX`
        if op == 0x08 and len(rest) >= 1:
            sub = rest[0]
            if sub == 0x00:
                pass  # stop
            elif sub == 0x02:
                self._step_focus(+256)
            elif sub == 0x03:
                self._step_focus(-256)
            elif (sub & 0xF0) == 0x20:
                self._step_focus(+64 * ((sub & 0x0F) + 1))
            elif (sub & 0xF0) == 0x30:
                self._step_focus(-64 * ((sub & 0x0F) + 1))
            return _ack_completion()

        # CAM_Focus Direct: `48 0p 0q 0r 0s`
        if op == 0x48 and len(rest) >= 4:
            self.set_state("focus_position", _decode_4nibble(rest[:4]))
            return _ack_completion()

        # CAM_FocusMode (Auto/Manual): `38 02|03|10`
        if op == 0x38 and len(rest) >= 1:
            if rest[0] == 0x02:
                self.set_state("focus_mode", "auto")
            elif rest[0] == 0x03:
                self.set_state("focus_mode", "manual")
            elif rest[0] == 0x10:
                # Toggle
                cur = self.get_state("focus_mode")
                self.set_state(
                    "focus_mode", "manual" if cur == "auto" else "auto"
                )
            return _ack_completion()

        # CAM_Focus One Push: `18 01`
        if op == 0x18 and len(rest) >= 1 and rest[0] == 0x01:
            return _ack_completion()

        # CAM_WB: `35 XX`
        if op == 0x35 and len(rest) >= 1:
            mode = _WB_MODE_FROM_BYTE.get(rest[0])
            if mode:
                self.set_state("wb_mode", mode)
            return _ack_completion()

        # CAM_WB One Push Trigger: `10 05`
        if op == 0x10 and len(rest) >= 1 and rest[0] == 0x05:
            return _ack_completion()

        # CAM_AE: `39 XX`
        if op == 0x39 and len(rest) >= 1:
            mode = _AE_MODE_FROM_BYTE.get(rest[0])
            if mode:
                self.set_state("ae_mode", mode)
            return _ack_completion()

        # CAM_Backlight: `33 02|03`
        if op == 0x33 and len(rest) >= 1:
            self.set_state("backlight", rest[0] == 0x02)
            return _ack_completion()

        # CAM_Memory (presets): `3F 00|01|02 pp`
        # Also CAM_OSD Open/Close: `3F 02 5F`
        if op == 0x3F and len(rest) >= 2:
            sub, num = rest[0], rest[1]
            if num == 0x5F and sub == 0x02:
                # OSD open/close
                return _ack_completion()
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

        # CAM_LR_Reverse: `61 02|03`
        if op == 0x61 and len(rest) >= 1:
            self.set_state("lr_reverse", rest[0] == 0x02)
            return _ack_completion()

        # CAM_PictureFlip: `66 02|03`
        if op == 0x66 and len(rest) >= 1:
            self.set_state("picture_flip", rest[0] == 0x02)
            return _ack_completion()

        # CAM_Flip: `A4 XX`
        if op == 0xA4 and len(rest) >= 1:
            mode = _FLIP_FROM_BYTE.get(rest[0])
            if mode:
                self.set_state("flip", mode)
            return _ack_completion()

        # CAM_SettingSave: `A5 10`
        if op == 0xA5 and len(rest) >= 1 and rest[0] == 0x10:
            return _ack_completion()

        # Many CAM_xxx commands we don't model in detail (R/B gain,
        # ColorTemp, Iris, Shutter, Gain, Bright, ExpComp, Aperture,
        # PictureEffect, ColorGain, ColorHue, AFZone, AWBSensitivity,
        # Brightness, Contrast). Accept them silently so the driver
        # gets a clean ACK + Completion.
        if op in (0x02, 0x03, 0x04, 0x05, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E,
                  0x20, 0x23, 0x2C, 0x3E, 0x42, 0x43, 0x44, 0x49, 0x4A,
                  0x4B, 0x4E, 0x4F, 0x63, 0xA1, 0xA2, 0xA9, 0xAA):
            return _ack_completion()

        return b"\x90\x60\x02\xff"

    def _handle_pt(self, b: bytes) -> bytes | None:
        if not b:
            return b"\x90\x60\x02\xff"

        op = b[0]
        rest = b[1:]

        # Pan/Tilt drive: `01 VV WW <p> <t>`. Also "preset speed" when
        # only one byte trails (`01 pp`).
        if op == 0x01:
            if len(rest) == 1:
                # Preset speed
                self.set_state("preset_speed", max(1, min(24, rest[0])))
                return _ack_completion()
            if len(rest) >= 4:
                pan_speed, tilt_speed, pan_dir, tilt_dir = rest[:4]
                self._step_pt(pan_dir, tilt_dir, pan_speed, tilt_speed)
                return _ack_completion()
            return b"\x90\x60\x02\xff"

        # Pan/Tilt absolute: `02 VV WW 0Y 0Y 0Y 0Y 0Z 0Z 0Z 0Z`
        if op == 0x02 and len(rest) >= 10:
            pan = _decode_4nibble(rest[2:6], signed=True)
            tilt = _decode_4nibble(rest[6:10], signed=True)
            self.set_state(
                "pan_position", max(_PAN_MIN, min(_PAN_MAX, pan))
            )
            self.set_state(
                "tilt_position", max(_TILT_MIN, min(_TILT_MAX, tilt))
            )
            return _ack_completion()

        # Pan/Tilt relative: `03 VV WW 0Y 0Y 0Y 0Y 0Z 0Z 0Z 0Z`
        if op == 0x03 and len(rest) >= 10:
            dpan = _decode_4nibble(rest[2:6], signed=True)
            dtilt = _decode_4nibble(rest[6:10], signed=True)
            self.set_state(
                "pan_position",
                max(_PAN_MIN, min(_PAN_MAX, self.get_state("pan_position", 0) + dpan)),
            )
            self.set_state(
                "tilt_position",
                max(_TILT_MIN, min(_TILT_MAX, self.get_state("tilt_position", 0) + dtilt)),
            )
            return _ack_completion()

        # Pan/Tilt Home: `04`
        if op == 0x04:
            self.set_state("pan_position", 0)
            self.set_state("tilt_position", 0)
            return _ack_completion()

        # Pan/Tilt Reset: `05`
        if op == 0x05:
            self.set_state("pan_position", 0)
            self.set_state("tilt_position", 0)
            return _ack_completion()

        # Pan/Tilt Limit: `07 ...`
        if op == 0x07:
            return _ack_completion()

        # OSD Enter/Return: `06 04|05`
        if op == 0x06 and len(rest) >= 1 and rest[0] in (0x04, 0x05):
            return _ack_completion()

        return b"\x90\x60\x02\xff"

    # ── Extended action commands (`81 0A ...`) ──

    def _handle_action_0a(self, b: bytes) -> bytes | None:
        if not b:
            return b"\x90\x60\x02\xff"

        # Tally: `02 02 0p`
        if b[:2] == b"\x02\x02" and len(b) >= 3:
            p = b[2]
            tally = {0x01: "flashing", 0x02: "on", 0x03: "off"}.get(p)
            if tally:
                self.set_state("tally", tally)
            return _ack_completion()

        # AF Calibration: `01 03 10`
        if b[:3] == b"\x01\x03\x10":
            return _ack_completion()

        # Focus Lock/Unlock: `04 68 02|03`
        if b[:2] == b"\x04\x68" and len(b) >= 3:
            return _ack_completion()

        # PTZ Motion Sync etc.: `11 ...`
        if b[0] == 0x11:
            return _ack_completion()

        return b"\x90\x60\x02\xff"

    # ── Inquiries (`81 09 ...`) ──

    def _handle_inquiry(self, b: bytes) -> bytes:
        if len(b) < 2:
            return b"\x90\x60\x02\xff"

        # CAM_xxx inquiries: `04 op`
        if b[0] == 0x04:
            return self._inquiry_cam(b[1])

        # Pan/Tilt: `06 12` → position; `06 06` → menu mode
        if b[0] == 0x06 and len(b) >= 2:
            if b[1] == 0x12:
                pan = self.get_state("pan_position", 0)
                tilt = self.get_state("tilt_position", 0)
                return (
                    b"\x90\x50"
                    + _encode_4nibble(pan)
                    + _encode_4nibble(tilt)
                    + b"\xff"
                )
            if b[1] == 0x06:
                # Menu off (we don't model OSD)
                return b"\x90\x50\x03\xff"

        # CAM_LensBlock / CameraBlock / OtherBlock / Enlargement:
        # `7E 7E 00|01|02|03`
        if b[0] == 0x7E:
            return self._inquiry_block(b)

        # CAM_UACInq: `2A 02 A0 04` — wrapped as `09 2A ...` in our spec
        if b[0] == 0x2A:
            return b"\x90\x50\x03\xff"  # off

        return b"\x90\x60\x02\xff"

    def _inquiry_cam(self, op: int) -> bytes:
        if op == 0x47:  # zoom position
            return b"\x90\x50" + _encode_4nibble(
                self.get_state("zoom_position", 0)
            ) + b"\xff"
        if op == 0x48:  # focus position
            return b"\x90\x50" + _encode_4nibble(
                self.get_state("focus_position", 0)
            ) + b"\xff"
        if op == 0x38:  # focus AF mode
            mode = self.get_state("focus_mode", "auto")
            return b"\x90\x50" + (b"\x02" if mode == "auto" else b"\x03") + b"\xff"
        if op == 0x35:  # WB mode
            mode = self.get_state("wb_mode", "auto")
            return bytes([0x90, 0x50, _WB_MODE_TO_BYTE.get(mode, 0x00), 0xFF])
        if op == 0x39:  # AE mode
            mode = self.get_state("ae_mode", "full_auto")
            return bytes([0x90, 0x50, _AE_MODE_TO_BYTE.get(mode, 0x00), 0xFF])
        if op == 0x33:  # backlight
            on = bool(self.get_state("backlight", False))
            return b"\x90\x50" + (b"\x02" if on else b"\x03") + b"\xff"
        if op == 0xA4:  # flip
            mode = self.get_state("flip", "off")
            return bytes([0x90, 0x50, _FLIP_TO_BYTE.get(mode, 0x00), 0xFF])
        if op == 0x61:  # LR reverse
            on = bool(self.get_state("lr_reverse", False))
            return b"\x90\x50" + (b"\x02" if on else b"\x03") + b"\xff"
        if op == 0x66:  # picture flip
            on = bool(self.get_state("picture_flip", False))
            return b"\x90\x50" + (b"\x02" if on else b"\x03") + b"\xff"
        if op in (
            0x43, 0x44, 0x4A, 0x4B, 0x4D, 0x4E, 0xA1, 0xA2, 0x42, 0x49, 0x4F
        ):
            # Returns pq or pqrs zero — simulator doesn't model these in detail.
            return b"\x90\x50\x00\x00\x00\x00\xff"
        if op in (0x05, 0x50, 0x55, 0x58, 0x63, 0xA9, 0xAA, 0x2C):
            return b"\x90\x50\x00\xff"
        if op == 0x53 or op == 0x54:
            return b"\x90\x50\x00\xff"
        if op == 0x20:
            return b"\x90\x50\x00\x00\xff"
        return b"\x90\x60\x02\xff"

    def _inquiry_block(self, b: bytes) -> bytes:
        # `7E 7E XX` — return a zeroed block of the documented length.
        if len(b) < 3:
            return b"\x90\x60\x02\xff"
        block = b[2]
        if block == 0x00:
            # Lens block: 12 data bytes + zoom + focus + focus mode
            zoom = _encode_4nibble(self.get_state("zoom_position", 0))
            focus = _encode_4nibble(self.get_state("focus_position", 0))
            mode = 0x01 if self.get_state("focus_mode", "auto") == "auto" else 0x00
            return (
                b"\x90\x50"
                + zoom
                + b"\x00\x00"
                + focus
                + b"\x00"
                + bytes([mode])
                + b"\x00\xff"
            )
        if block == 0x01:
            # Camera block — 14 zeros
            return b"\x90\x50" + b"\x00" * 14 + b"\xff"
        if block == 0x02:
            # Other block
            power = 0x01 if self.get_state("power", True) else 0x00
            lr = 0x04 if self.get_state("lr_reverse", False) else 0x00
            return (
                b"\x90\x50"
                + bytes([power, lr])
                + b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff"
            )
        if block == 0x03:
            return b"\x90\x50" + b"\x00" * 14 + b"\xff"
        return b"\x90\x60\x02\xff"

    # ── State helpers ──

    def _step_zoom(self, delta: int) -> None:
        cur = self.get_state("zoom_position", 0)
        self.set_state("zoom_position", max(0, min(_ZOOM_MAX, cur + delta)))

    def _step_focus(self, delta: int) -> None:
        cur = self.get_state("focus_position", 0)
        self.set_state("focus_position", max(0, min(0xFFFF, cur + delta)))

    def _step_pt(self, pan_dir: int, tilt_dir: int, pan_speed: int, tilt_speed: int) -> None:
        # Pan dir: 1=left, 2=right, 3=stop. Tilt dir: 1=up, 2=down, 3=stop.
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
