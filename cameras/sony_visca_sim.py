"""
Sony VISCA-over-IP PTZ Camera — Simulator.

Implements the Sony-spec wire format on UDP 52381 and the Sony-specific
command surface that the driver adds on top of the universal VISCA set:
picture profile, R/B gain, color matrix, defog, visibility enhancer,
low-light basis, chroma suppress, PRESET MODE, PTZ TRACE, tally, IR
correction, ICR, AF mode, AE speed, min/max shutter, high sensitivity,
OSD on/off.

The wire format (8-byte VISCA-over-IP header + VISCA payload, Control
RESET handshake on connect) is the same as the generic ``visca_ip``
simulator — duplicated here so the simulator stays a single, standalone
file.

Driver side: ``cameras/sony_visca.py``.
"""

from __future__ import annotations

import logging
import struct
from typing import Any

from simulator.udp_simulator import UDPSimulator

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


def _encode_2nibble(value: int) -> bytes:
    v = value & 0xFF
    return bytes([(v >> 4) & 0x0F, v & 0x0F])


def _decode_2nibble(data: bytes) -> int:
    return ((data[0] & 0x0F) << 4) | (data[1] & 0x0F)


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

_PICTURE_PROFILE_TO_BYTE = {
    "std":       0x02,
    "off":       0x03,
    "high_sat":  0x04,
    "fl_light":  0x05,
    "movie":     0x06,
    "still":     0x07,
    "cinema":    0x08,
    "pro":       0x09,
    "itu709":    0x0A,
    "bw":        0x0B,
}
_BYTE_TO_PICTURE_PROFILE = {v: k for k, v in _PICTURE_PROFILE_TO_BYTE.items()}

_PRESET_MODE_TO_BYTE = {
    "mode1": 0x00,
    "mode2": 0x01,
    "trace": 0x10,
}
_BYTE_TO_PRESET_MODE = {v: k for k, v in _PRESET_MODE_TO_BYTE.items()}

_AF_MODE_TO_BYTE = {
    "normal":        0x00,
    "interval":      0x01,
    "zoom_trigger":  0x02,
}
_BYTE_TO_AF_MODE = {v: k for k, v in _AF_MODE_TO_BYTE.items()}

_TALLY_LEVEL_TO_BYTE = {
    "off":      0x00,
    "on_low":   0x04,
    "on_high":  0x05,
}
_BYTE_TO_TALLY_LEVEL = {v: k for k, v in _TALLY_LEVEL_TO_BYTE.items()}

_PTZ_TRACE_STATUS_NAME_TO_BYTE = {
    "none": 0x00,
    "recording": 0x01,
    "preparing": 0x02,
    "ready": 0x03,
    "playing": 0x04,
    "deleting": 0x05,
}


_PAN_MIN, _PAN_MAX = -2448, 2448
_TILT_MIN, _TILT_MAX = -432, 1296
_ZOOM_MAX = 0x4000


def _ack_completion() -> bytes:
    return b"\x90\x41\xff\x90\x51\xff"


def _err_syntax() -> bytes:
    return b"\x90\x60\x02\xff"


class SonyVISCASimulator(UDPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "sony_visca",
        "name": "Sony VISCA-IP PTZ Camera Simulator",
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
            "spotlight": False,
            "picture_profile": "std",
            "rgain": 0x80,
            "bgain": 0x80,
            "chroma_suppress": 0,
            "visibility_enhancer": False,
            "defog": False,
            "defog_level": 2,
            "low_light": False,
            "low_light_level": 7,
            "ae_speed": 1,
            "min_shutter": 0x10,
            "max_shutter": 0x40,
            "high_sensitivity": False,
            "exp_comp": False,
            "exp_comp_level": 7,
            "flicker_cancel": False,
            "ir_correction": "standard",
            "ir_cut_filter": "day",
            "auto_icr": False,
            "af_mode": "normal",
            "preset_mode": "mode1",
            "ptz_trace_status": "none",
            "tally_level": "off",
            "tally_onoff": False,
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
                "options": list(_AE_MODE_TO_BYTE.keys()),
            },
            {
                "type": "select",
                "key": "wb_mode",
                "label": "White Balance",
                "options": list(_WB_MODE_TO_BYTE.keys()),
            },
            {
                "type": "select",
                "key": "picture_profile",
                "label": "Picture Profile",
                "options": list(_PICTURE_PROFILE_TO_BYTE.keys()),
            },
            {
                "type": "select",
                "key": "preset_mode",
                "label": "Preset Mode",
                "options": list(_PRESET_MODE_TO_BYTE.keys()),
            },
            {
                "type": "select",
                "key": "tally_level",
                "label": "Tally",
                "options": list(_TALLY_LEVEL_TO_BYTE.keys()),
            },
            {"type": "power", "key": "power", "label": "Power"},
            {"type": "toggle", "key": "backlight", "label": "Backlight"},
            {"type": "toggle", "key": "defog", "label": "Defog"},
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._presets: dict[int, dict[str, Any]] = {}
        self._color_matrix: dict[str, int] = {
            k: 0x63 for k in ("rg", "rb", "gr", "gb", "br", "bg")
        }
        self._last_sequence = 0

    # ── Top-level wrap/unwrap ──

    def handle_command(self, data: bytes) -> bytes | None:
        unwrapped = _unwrap(data)
        if unwrapped is None:
            return None
        payload_type, sequence, payload = unwrapped
        self._last_sequence = sequence

        if payload_type == PAYLOAD_CONTROL_CMD:
            if payload == b"\x01":
                return _wrap(b"\x01", PAYLOAD_CONTROL_REPLY, sequence)
            return _wrap(b"\x0F\x02", PAYLOAD_CONTROL_REPLY, sequence)

        if payload_type in (PAYLOAD_VISCA_COMMAND, PAYLOAD_VISCA_INQUIRY):
            visca_reply = self._dispatch_visca(payload)
            if visca_reply is None:
                return None
            return _wrap(visca_reply, PAYLOAD_VISCA_REPLY, sequence)

        return None

    # ── VISCA byte dispatch ──

    def _dispatch_visca(self, packet: bytes) -> bytes | None:
        if len(packet) < 3 or packet[0] != 0x81 or packet[-1] != 0xFF:
            return _err_syntax()
        body = packet[1:-1]
        if not body:
            return _err_syntax()

        try:
            if body[0] == 0x09:
                return self._handle_inquiry(body[1:])
            if body[0] == 0x01:
                return self._handle_action(body[1:])
        except Exception:
            logger.exception("sony_visca_sim: error handling %s", packet.hex())
            return b"\x90\x60\x41\xff"

        return _err_syntax()

    # ── Action handlers ──

    def _handle_action(self, b: bytes) -> bytes | None:
        if not b:
            return _err_syntax()
        if b[0] == 0x04 and len(b) >= 2:
            return self._action_cam(b[1:])
        if b[0] == 0x05 and len(b) >= 2:
            return self._action_cam5(b[1:])
        if b[0] == 0x06 and len(b) >= 2:
            return self._action_pt(b[1:])
        if b[0] == 0x7E and len(b) >= 2:
            return self._action_ext(b[1:])
        return _err_syntax()

    def _action_cam(self, b: bytes) -> bytes | None:
        op = b[0]
        rest = b[1:]

        if op == 0x00 and rest:
            self.set_state("power", "on" if rest[0] == 0x02 else "standby")
            return _ack_completion()

        if op == 0x01 and rest:
            self.set_state("ir_cut_filter", "night" if rest[0] == 0x02 else "day")
            return _ack_completion()

        if op == 0x03 and rest:
            sub = rest[0]
            cur = self.get_state("rgain", 0x80)
            if sub == 0x00:
                self.set_state("rgain", 0x80)
            elif sub == 0x02:
                self.set_state("rgain", min(0xFF, cur + 1))
            elif sub == 0x03:
                self.set_state("rgain", max(0, cur - 1))
            return _ack_completion()

        if op == 0x04 and rest:
            sub = rest[0]
            cur = self.get_state("bgain", 0x80)
            if sub == 0x00:
                self.set_state("bgain", 0x80)
            elif sub == 0x02:
                self.set_state("bgain", min(0xFF, cur + 1))
            elif sub == 0x03:
                self.set_state("bgain", max(0, cur - 1))
            return _ack_completion()

        if op == 0x07 and rest:
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

        if op == 0x08 and rest:
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

        if op == 0x10 and rest and rest[0] == 0x05:
            return _ack_completion()

        if op == 0x11 and rest:
            self.set_state(
                "ir_correction", "ir_light" if rest[0] == 0x01 else "standard"
            )
            return _ack_completion()

        if op == 0x18 and rest and rest[0] == 0x01:
            return _ack_completion()

        if op == 0x32 and rest:
            self.set_state("flicker_cancel", rest[0] == 0x02)
            return _ack_completion()

        if op == 0x33 and rest:
            self.set_state("backlight", rest[0] == 0x02)
            return _ack_completion()

        if op == 0x35 and rest:
            mode = _BYTE_TO_WB_MODE.get(rest[0])
            if mode:
                self.set_state("wb_mode", mode)
            return _ack_completion()

        if op == 0x37 and len(rest) >= 2:
            self.set_state("defog", rest[0] == 0x02)
            if rest[0] == 0x02 and rest[1]:
                self.set_state("defog_level", rest[1])
            return _ack_completion()

        if op == 0x38 and rest:
            if rest[0] == 0x02:
                self.set_state("focus_mode", "auto")
            elif rest[0] == 0x03:
                self.set_state("focus_mode", "manual")
            elif rest[0] == 0x10:
                cur = self.get_state("focus_mode", "auto")
                self.set_state("focus_mode", "manual" if cur == "auto" else "auto")
            return _ack_completion()

        if op == 0x39 and rest:
            mode = _BYTE_TO_AE_MODE.get(rest[0])
            if mode:
                self.set_state("ae_mode", mode)
            return _ack_completion()

        if op == 0x3A and rest:
            self.set_state("spotlight", rest[0] == 0x02)
            return _ack_completion()

        if op == 0x3D and rest:
            self.set_state("visibility_enhancer", rest[0] == 0x06)
            return _ack_completion()

        if op == 0x3E and rest:
            self.set_state("exp_comp", rest[0] == 0x02)
            return _ack_completion()

        if op == 0x3F and len(rest) >= 2:
            sub, num = rest[0], rest[1]
            if 0 <= num <= 99:
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

        if op == 0x43 and len(rest) >= 4:
            self.set_state("rgain", _decode_2nibble(rest[2:4]))
            return _ack_completion()

        if op == 0x44 and len(rest) >= 4:
            self.set_state("bgain", _decode_2nibble(rest[2:4]))
            return _ack_completion()

        if op == 0x47 and len(rest) >= 4:
            self.set_state("zoom_position", _decode_4nibble(rest[:4]))
            return _ack_completion()

        if op == 0x48 and len(rest) >= 4:
            self.set_state("focus_position", _decode_4nibble(rest[:4]))
            return _ack_completion()

        if op == 0x4E and len(rest) >= 4:
            self.set_state("exp_comp_level", _decode_2nibble(rest[2:4]))
            return _ack_completion()

        if op == 0x51 and rest:
            self.set_state("auto_icr", rest[0] == 0x02)
            return _ack_completion()

        if op == 0x57 and rest:
            mode = _BYTE_TO_AF_MODE.get(rest[0])
            if mode:
                self.set_state("af_mode", mode)
            return _ack_completion()

        if op == 0x5D and rest:
            self.set_state("ae_speed", rest[0])
            return _ack_completion()

        if op == 0x5E and rest:
            self.set_state("high_sensitivity", rest[0] == 0x02)
            return _ack_completion()

        if op == 0x5F and rest:
            self.set_state("chroma_suppress", rest[0])
            return _ack_completion()

        return _err_syntax()

    def _action_cam5(self, b: bytes) -> bytes | None:
        op = b[0]
        rest = b[1:]

        # MAX/MIN SHUTTER: 05 2A 00|01 0p 0p
        if op == 0x2A and len(rest) >= 3:
            kind, hi, lo = rest[0], rest[1], rest[2]
            value = ((hi & 0x0F) << 4) | (lo & 0x0F)
            if kind == 0x00:
                self.set_state("max_shutter", value)
            elif kind == 0x01:
                self.set_state("min_shutter", value)
            return _ack_completion()

        # LOW LIGHT BASIS On/Off: 05 39 0p
        if op == 0x39 and rest:
            self.set_state("low_light", rest[0] == 0x02)
            return _ack_completion()

        # LOW LIGHT BASIS LEVEL: 05 49 0p
        if op == 0x49 and rest:
            self.set_state("low_light_level", rest[0])
            return _ack_completion()

        return _err_syntax()

    def _action_pt(self, b: bytes) -> bytes | None:
        op = b[0]
        rest = b[1:]

        if op == 0x01 and len(rest) >= 4:
            pan_speed, tilt_speed, pan_dir, tilt_dir = rest[:4]
            self._step_pt(pan_dir, tilt_dir, pan_speed, tilt_speed)
            return _ack_completion()

        if op == 0x02 and len(rest) >= 10:
            pan = _decode_4nibble(rest[2:6], signed=True)
            tilt = _decode_4nibble(rest[6:10], signed=True)
            self.set_state("pan_position", max(_PAN_MIN, min(_PAN_MAX, pan)))
            self.set_state("tilt_position", max(_TILT_MIN, min(_TILT_MAX, tilt)))
            return _ack_completion()

        if op == 0x04:
            self.set_state("pan_position", 0)
            self.set_state("tilt_position", 0)
            return _ack_completion()

        if op == 0x05:
            self.set_state("pan_position", 0)
            self.set_state("tilt_position", 0)
            return _ack_completion()

        return _err_syntax()

    def _action_ext(self, b: bytes) -> bytes | None:
        # Sony 7E extended commands. b is the bytes after the leading 0x7E.
        if len(b) < 2:
            return _err_syntax()
        group = b[0]
        op = b[1]
        rest = b[2:]

        # Group 01 — picture profile, color matrix, tally, menu enter
        if group == 0x01:
            if op == 0x02 and len(rest) >= 2 and rest[0] == 0x00 and rest[1] == 0x01:
                # Menu enter — no-op ack.
                return _ack_completion()
            if op == 0x0A and len(rest) >= 2:
                # Tally: 7E 01 0A 00 0p (on/off) or 7E 01 0A 01 0p (level)
                kind, val = rest[0], rest[1]
                if kind == 0x00:
                    self.set_state("tally_onoff", val == 0x02)
                    if val == 0x03:
                        self.set_state("tally_level", "off")
                    return _ack_completion()
                if kind == 0x01:
                    level = _BYTE_TO_TALLY_LEVEL.get(val)
                    if level:
                        self.set_state("tally_level", level)
                    return _ack_completion()
            if op == 0x3D and rest:
                # Picture profile (CAM_PictureEffect ext)
                profile = _BYTE_TO_PICTURE_PROFILE.get(rest[0])
                if profile:
                    self.set_state("picture_profile", profile)
                return _ack_completion()
            # Color matrix: 7E 01 7A..7F 0p 0p
            for axis, code in {
                "rg": 0x7A, "rb": 0x7B, "gr": 0x7C,
                "gb": 0x7D, "br": 0x7E, "bg": 0x7F,
            }.items():
                if op == code and len(rest) >= 2:
                    self._color_matrix[axis] = _decode_2nibble(rest[:2])
                    return _ack_completion()

        # Group 04 — preset mode, PTZ TRACE, OSD
        if group == 0x04:
            if op == 0x20 and len(rest) >= 3:
                # PTZ TRACE: 7E 04 20 [00 0p 02]=record_start, [00 00 03]=record_stop,
                #            [01 0p 01]=play_prepare, [01 00 02]=play_start,
                #            [02 0p 00]=delete
                sub, _slot, action = rest[0], rest[1], rest[2]
                if sub == 0x00 and action == 0x02:
                    self.set_state("ptz_trace_status", "recording")
                elif sub == 0x00 and action == 0x03:
                    self.set_state("ptz_trace_status", "none")
                elif sub == 0x01 and action == 0x01:
                    self.set_state("ptz_trace_status", "preparing")
                elif sub == 0x01 and action == 0x02:
                    self.set_state("ptz_trace_status", "playing")
                elif sub == 0x02 and action == 0x00:
                    self.set_state("ptz_trace_status", "deleting")
                return _ack_completion()
            if op == 0x3D and rest:
                mode = _BYTE_TO_PRESET_MODE.get(rest[0])
                if mode:
                    self.set_state("preset_mode", mode)
                return _ack_completion()
            if op == 0x76 and len(rest) >= 2:
                # OSD: ack only — simulator doesn't track OSD state, but the
                # camera does respond.
                return _ack_completion()

        return _err_syntax()

    # ── Inquiry handlers ──

    def _handle_inquiry(self, b: bytes) -> bytes:
        if len(b) < 2:
            return _err_syntax()

        if b[0] == 0x04:
            return self._inq_cam(b[1:])
        if b[0] == 0x05:
            return self._inq_cam5(b[1:])
        if b[0] == 0x06:
            return self._inq_pt(b[1:])
        if b[0] == 0x7E:
            return self._inq_ext(b[1:])
        return _err_syntax()

    def _inq_cam(self, b: bytes) -> bytes:
        op = b[0]

        if op == 0x00:
            on = self.get_state("power", "on") == "on"
            return b"\x90\x50" + (b"\x02" if on else b"\x03") + b"\xff"
        if op == 0x01:
            return b"\x90\x50" + (
                b"\x02" if self.get_state("ir_cut_filter", "day") == "night" else b"\x03"
            ) + b"\xff"
        if op == 0x11:
            return b"\x90\x50" + (
                b"\x01" if self.get_state("ir_correction", "standard") == "ir_light" else b"\x00"
            ) + b"\xff"
        if op == 0x32:
            return b"\x90\x50" + (
                b"\x02" if self.get_state("flicker_cancel", False) else b"\x03"
            ) + b"\xff"
        if op == 0x33:
            return b"\x90\x50" + (
                b"\x02" if self.get_state("backlight", False) else b"\x03"
            ) + b"\xff"
        if op == 0x35:
            mode = self.get_state("wb_mode", "auto1")
            return bytes([0x90, 0x50, _WB_MODE_TO_BYTE.get(mode, 0x00), 0xFF])
        if op == 0x37:
            on = bool(self.get_state("defog", False))
            lvl = int(self.get_state("defog_level", 2))
            return bytes([0x90, 0x50, 0x02 if on else 0x03, lvl, 0xFF])
        if op == 0x38:
            mode = self.get_state("focus_mode", "auto")
            return b"\x90\x50" + (b"\x02" if mode == "auto" else b"\x03") + b"\xff"
        if op == 0x39:
            mode = self.get_state("ae_mode", "full_auto")
            return bytes([0x90, 0x50, _AE_MODE_TO_BYTE.get(mode, 0x00), 0xFF])
        if op == 0x3A:
            return b"\x90\x50" + (
                b"\x02" if self.get_state("spotlight", False) else b"\x03"
            ) + b"\xff"
        if op == 0x3D:
            return b"\x90\x50" + (
                b"\x06" if self.get_state("visibility_enhancer", False) else b"\x03"
            ) + b"\xff"
        if op == 0x3E:
            return b"\x90\x50" + (
                b"\x02" if self.get_state("exp_comp", False) else b"\x03"
            ) + b"\xff"
        if op == 0x43:
            return (
                b"\x90\x50\x00\x00"
                + _encode_2nibble(self.get_state("rgain", 0x80))
                + b"\xff"
            )
        if op == 0x44:
            return (
                b"\x90\x50\x00\x00"
                + _encode_2nibble(self.get_state("bgain", 0x80))
                + b"\xff"
            )
        if op == 0x47:
            return (
                b"\x90\x50"
                + _encode_4nibble(self.get_state("zoom_position", 0))
                + b"\xff"
            )
        if op == 0x48:
            return (
                b"\x90\x50"
                + _encode_4nibble(self.get_state("focus_position", 0))
                + b"\xff"
            )
        if op == 0x4E:
            return (
                b"\x90\x50\x00\x00"
                + _encode_2nibble(self.get_state("exp_comp_level", 7))
                + b"\xff"
            )
        if op == 0x51:
            return b"\x90\x50" + (
                b"\x02" if self.get_state("auto_icr", False) else b"\x03"
            ) + b"\xff"
        if op == 0x57:
            mode = self.get_state("af_mode", "normal")
            return bytes([0x90, 0x50, _AF_MODE_TO_BYTE.get(mode, 0x00), 0xFF])
        if op == 0x5D:
            return bytes(
                [0x90, 0x50, int(self.get_state("ae_speed", 1)) & 0xFF, 0xFF]
            )
        if op == 0x5E:
            return b"\x90\x50" + (
                b"\x02" if self.get_state("high_sensitivity", False) else b"\x03"
            ) + b"\xff"
        if op == 0x5F:
            return bytes(
                [0x90, 0x50, int(self.get_state("chroma_suppress", 0)) & 0x0F, 0xFF]
            )

        return _err_syntax()

    def _inq_cam5(self, b: bytes) -> bytes:
        op = b[0]
        rest = b[1:]

        if op == 0x2A and rest:
            kind = rest[0]
            if kind == 0x00:
                v = int(self.get_state("max_shutter", 0x40))
            elif kind == 0x01:
                v = int(self.get_state("min_shutter", 0x10))
            else:
                return _err_syntax()
            return b"\x90\x50" + _encode_2nibble(v) + b"\xff"

        if op == 0x39:
            return b"\x90\x50" + (
                b"\x02" if self.get_state("low_light", False) else b"\x03"
            ) + b"\xff"
        if op == 0x49:
            return bytes(
                [0x90, 0x50, int(self.get_state("low_light_level", 7)) & 0x0F, 0xFF]
            )

        return _err_syntax()

    def _inq_pt(self, b: bytes) -> bytes:
        if b and b[0] == 0x12:
            pan = self.get_state("pan_position", 0)
            tilt = self.get_state("tilt_position", 0)
            return (
                b"\x90\x50"
                + _encode_4nibble(pan)
                + _encode_4nibble(tilt)
                + b"\xff"
            )
        return _err_syntax()

    def _inq_ext(self, b: bytes) -> bytes:
        # b is bytes after 7E. Layout is `<group> <op> [<args>]`.
        if len(b) < 2:
            return _err_syntax()
        group = b[0]
        op = b[1]
        rest = b[2:]

        if group == 0x01:
            if op == 0x0A:
                level = self.get_state("tally_level", "off")
                return bytes(
                    [0x90, 0x50, _TALLY_LEVEL_TO_BYTE.get(level, 0x00), 0xFF]
                )
            if op == 0x3D:
                profile = self.get_state("picture_profile", "std")
                return bytes(
                    [0x90, 0x50, _PICTURE_PROFILE_TO_BYTE.get(profile, 0x02), 0xFF]
                )
            for axis, code in {
                "rg": 0x7A, "rb": 0x7B, "gr": 0x7C,
                "gb": 0x7D, "br": 0x7E, "bg": 0x7F,
            }.items():
                if op == code:
                    return (
                        b"\x90\x50\x00\x00"
                        + _encode_2nibble(self._color_matrix.get(axis, 0x63))
                        + b"\xff"
                    )

        if group == 0x04:
            if op == 0x20 and rest and rest[0] == 0x03:
                status = self.get_state("ptz_trace_status", "none")
                return bytes(
                    [0x90, 0x50, _PTZ_TRACE_STATUS_NAME_TO_BYTE.get(status, 0x00), 0xFF]
                )
            if op == 0x3D:
                mode = self.get_state("preset_mode", "mode1")
                return bytes(
                    [0x90, 0x50, _PRESET_MODE_TO_BYTE.get(mode, 0x00), 0xFF]
                )

        return _err_syntax()

    # ── Movement helpers ──

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
