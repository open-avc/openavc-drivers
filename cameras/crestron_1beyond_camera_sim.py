"""
Crestron 1 Beyond camera (IV-CAM series) simulator.

The server side of Crestron's VISCA command set on TCP 5500: raw
``8x ... FF`` packets addressed to the configured camera address, ACK +
Completion for a command, ``y0 50 ... FF`` for an inquiry, and the VISCA
error replies. Models follow the device's Model setting: the tracking
inquiry and the intelligent presets on the I-series, Group Tracking, preset
zones and tracking profiles on the I20, mount mode on the P-series, the IR
receiver on the I12 and I12D-B, and the Intelligent Switching commands on a
host (I12 or I12D-B).

Privacy Mode (power standby) answers the power inquiry and Power On and
refuses everything else as "not executable". The document does not say
which; the "Privacy Mode answers nothing" error mode plays the other reading,
where the camera answers nothing while it is in Privacy Mode except Power On,
which wakes it.

The model codes in the version reply are placeholders: the document gives no
table of real ones.

Driver: crestron_1beyond_camera
Transport: tcp (raw VISCA, 0xFF terminator); the driver's serial transport
simulates over TCP.
"""

from __future__ import annotations

import asyncio
import logging
import time


from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

_I_SERIES = {"i12", "i20", "i12d"}
_P_SERIES = {"p12", "p20"}
_SWITCHING_HOSTS = {"i12", "i12d"}

# Placeholder model codes for the version reply (not Crestron's).
_MODEL_CODES = {"i12": 0x0001, "i20": 0x0002, "p12": 0x0003, "p20": 0x0004, "i12d": 0x0005}

# Travel limits from the specifications (pan -130..130, tilt -30..90 degrees,
# 14.4 positions per degree).
_PAN_LIMIT = (-1872, 1872)
_TILT_LIMIT = (-432, 1296)
_ZOOM_LIMIT = (0, 0x4000)
_FOCUS_LIMIT = (0, 0xFFFF)

_AE_FROM = {0x00: "full_auto", 0x03: "manual", 0x0A: "shutter", 0x0B: "iris", 0x0D: "bright"}
_AE_TO = {v: k for k, v in _AE_FROM.items()}
_WB_FROM = {0x00: "auto", 0x01: "indoor", 0x02: "outdoor", 0x03: "one_push", 0x05: "manual"}
_WB_TO = {v: k for k, v in _WB_FROM.items()}
_VIDEO_TO = {
    "1080i60": 0x00, "1080p30": 0x01, "720p60": 0x02, "720p30": 0x03,
    "1080p60": 0x07, "1080i50": 0x08, "1080p25": 0x09, "720p50": 0x0A,
    "720p25": 0x0B, "1080p50": 0x0F,
}

# Level step byte -> state key, and direct byte -> state key.
_LEVEL_STEP = {
    0x03: "r_gain", 0x04: "b_gain", 0x0A: "shutter", 0x0B: "iris",
    0x0C: "gain", 0x0D: "bright", 0x0E: "exp_comp_code",
}
_LEVEL_DIRECT = {
    0x43: "r_gain", 0x44: "b_gain", 0x4A: "shutter", 0x4B: "iris",
    0x4C: "gain", 0x4D: "bright", 0x4E: "exp_comp_code",
}
_LEVEL_DEFAULT = {
    "r_gain": 0x80, "b_gain": 0x80, "shutter": 0x10, "iris": 0x08,
    "gain": 0x00, "bright": 0x07, "exp_comp_code": 0x07,
}
_LEVEL_MAX = {"exp_comp_code": 0x0E}

_RESERVED = {
    "i12": {80, 81, 84, 85, 86, 95, 99},
    "i20": {80, 81, 82, 83, 95, 99, *range(101, 109)},
    "i12d": {80, 81, 85, 86, 87, 88, 89, 95, 99},
    "p12": {95, 99},
    "p20": {95, 99},
}

BOOT_SECONDS = 3.0


def _encode_4nibble(value: int) -> bytes:
    v = value & 0xFFFF
    return bytes([(v >> 12) & 0x0F, (v >> 8) & 0x0F, (v >> 4) & 0x0F, v & 0x0F])


def _decode_4nibble(data: bytes, signed: bool = False) -> int:
    v = (
        ((data[0] & 0x0F) << 12) | ((data[1] & 0x0F) << 8)
        | ((data[2] & 0x0F) << 4) | (data[3] & 0x0F)
    )
    if signed and v >= 0x8000:
        v -= 0x10000
    return v


def _lightbar_text(segments: bytes) -> str:
    names = {0b00: "green", 0b01: "red", 0b11: "yellow", 0b10: "?"}
    levels = {0b00: "off", 0b01: "dim", 0b10: "medium", 0b11: "bright"}
    out = []
    for b in segments:
        level = levels[(b >> 2) & 0b11]
        out.append("off" if level == "off" else f"{names[b & 0b11]} {level}")
    return " | ".join(out)


class CrestronOneBeyondCameraSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "crestron_1beyond_camera",
        "name": "Crestron 1 Beyond Camera Simulator",
        "category": "camera",
        "transport": "tcp",
        "default_port": 5500,
        "initial_state": {
            "power": "on",
            "pan_position": 0,
            "tilt_position": 0,
            "zoom_position": 0,
            "focus_position": 0x1000,
            "focus_mode": "auto",
            "ae_mode": "full_auto",
            "wb_mode": "auto",
            "r_gain": 0x80,
            "b_gain": 0x80,
            "shutter": 0x10,
            "iris": 0x08,
            "gain": 0x00,
            "bright": 0x07,
            "exp_comp": False,
            "exp_comp_code": 0x07,
            "backlight": False,
            "last_preset": 0,
            "tracking": "paused",
            "group_tracking": False,
            "intelligent_mode": "group_framing",
            "tracking_profile": 1,
            "ir_receiver": True,
            "mount_mode": "stand",
            "video_format": "1080p60",
            "freeze": False,
            "preset_freeze": False,
            "osd_menu": False,
            "lightbar": "off | green bright | green bright | off",
            "switching_active": False,
            "switching_output": 1,
            "camera_2_ip": "",
            "camera_3_ip": "",
            "camera_4_ip": "",
            "camera_5_ip": "",
            "reboots": 0,
        },
        "delays": {"command_response": 0.005},
        "error_modes": {
            "privacy_silent": {
                "description": "Privacy Mode answers nothing (Power On still wakes the camera)",
                "behavior": "privacy_silent",
            },
            "busy": {
                "description": "Every command answers Command Buffer Full",
                "behavior": "busy",
            },
            "no_response": {
                "description": "The camera answers nothing at all",
                "behavior": "no_response",
            },
        },
        "controls": [
            {"type": "select", "key": "power", "label": "Power / Privacy Mode",
             "options": ["on", "standby"]},
            {"type": "select", "key": "tracking", "label": "Tracking",
             "options": ["active", "paused"]},
            {"type": "indicator", "key": "pan_position", "label": "Pan"},
            {"type": "indicator", "key": "tilt_position", "label": "Tilt"},
            {"type": "indicator", "key": "zoom_position", "label": "Zoom"},
            {"type": "indicator", "key": "focus_position", "label": "Focus"},
            {"type": "select", "key": "ae_mode", "label": "Exposure Mode",
             "options": ["full_auto", "manual", "shutter", "iris", "bright"]},
            {"type": "select", "key": "wb_mode", "label": "White Balance",
             "options": ["auto", "indoor", "outdoor", "one_push", "manual"]},
            {"type": "toggle", "key": "backlight", "label": "Backlight Compensation"},
            {"type": "toggle", "key": "ir_receiver", "label": "IR Receiver"},
            {"type": "select", "key": "mount_mode", "label": "Mount Mode (P-series)",
             "options": ["stand", "ceiling"]},
            {"type": "indicator", "key": "last_preset", "label": "Last Preset"},
            {"type": "indicator", "key": "lightbar", "label": "Lightbar"},
            {"type": "indicator", "key": "osd_menu", "label": "On-Screen Menu"},
            {"type": "toggle", "key": "switching_active", "label": "Intelligent Switching"},
            {"type": "indicator", "key": "switching_output", "label": "Switching Output"},
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._delimiter = b"\xff"
        self._line_mode = False
        model = str(self.config.get("model") or "i12").strip().lower()
        self._model = model if model in _I_SERIES | _P_SERIES else "i12"
        try:
            address = int(self.config.get("camera_address", 1))
        except (TypeError, ValueError):
            address = 1
        self._address = max(1, min(7, address))
        self._head = (self._address + 8) << 4
        self._presets: dict[int, tuple[int, int, int, int]] = {}
        self._velocity: dict[str, float] = {}
        self._moved_at = time.monotonic()
        self._booting_until = 0.0

    # ── Replies ──

    def _ack_done(self) -> bytes:
        return bytes([self._head, 0x41, 0xFF, self._head, 0x51, 0xFF])

    def _answer(self, *payload: int) -> bytes:
        return bytes([self._head, 0x50, *payload, 0xFF])

    def _error(self, code: int, socket: int = 0) -> bytes:
        return bytes([self._head, 0x60 | socket, code, 0xFF])

    def _syntax(self) -> bytes:
        return self._error(0x02)

    # ── Motion ──

    def _advance(self) -> None:
        now = time.monotonic()
        elapsed, self._moved_at = now - self._moved_at, now
        limits = {
            "pan_position": _PAN_LIMIT, "tilt_position": _TILT_LIMIT,
            "zoom_position": _ZOOM_LIMIT, "focus_position": _FOCUS_LIMIT,
        }
        for key, rate in list(self._velocity.items()):
            if not rate:
                continue
            lo, hi = limits[key]
            value = int(self.get_state(key, 0) + rate * elapsed)
            self.set_state(key, max(lo, min(hi, value)))

    def _drive(self, key: str, rate: float) -> None:
        self._advance()
        self._velocity[key] = rate

    def _stop_all(self) -> None:
        self._advance()
        self._velocity.clear()

    # ── Entry ──

    def handle_command(self, data: bytes) -> bytes | None:
        if self.has_error_behavior("no_response"):
            return None
        if time.monotonic() < self._booting_until:
            return None
        if len(data) < 3 or data[-1] != 0xFF:
            return None
        if data[0] == 0x88:
            return self._broadcast(data[1:-1])
        if data[0] != 0x80 | self._address:
            return None  # another camera's address
        body = data[1:-1]
        try:
            self._advance()
            return self._dispatch(body)
        except Exception:
            logger.exception("crestron_1beyond_camera_sim: error handling %s", data.hex())
            return self._error(0x41)

    def _broadcast(self, body: bytes) -> bytes | None:
        if body == b"\x01\x00\x01":  # IF_Clear: the camera passes it on
            return b"\x88\x01\x00\x01\xff"
        if body[:2] == b"\x30\x01":  # AddressSet
            return bytes([0x88, 0x30, self._address + 1, 0xFF])
        return None

    def _dispatch(self, body: bytes) -> bytes | None:
        inquiry = body[0] == 0x09 or body[:2] == b"\xc2\x09"
        if self.get_state("power") == "standby":
            is_power = body[:3] in (b"\x09\x04\x00", b"\x01\x04\x00")
            if self.has_error_behavior("privacy_silent"):
                # Nothing is answered but the wake command itself.
                if body == b"\x01\x04\x00\x02":
                    self.set_state("power", "on")
                    return self._ack_done()
                return None
            if not is_power:
                return self._error(0x41, 0 if inquiry else 1)
        if self.has_error_behavior("busy") and not inquiry:
            return self._error(0x03)
        if inquiry:
            return self._inquiry(body)
        if body[:2] == b"\x01\x04":
            return self._cam(body[2:])
        if body[:2] == b"\x01\x06":
            return self._pan_tilt(body[2:])
        if body[0] == 0xC1 and len(body) == 5:
            self.set_state("lightbar", _lightbar_text(body[1:5]))
            return self._ack_done()
        if body[:2] == b"\xc2\x01":
            return self._switching(body[2:])
        return self._syntax()

    # ── CAM_ commands ──

    def _cam(self, b: bytes) -> bytes:
        if not b:
            return self._syntax()
        op, rest = b[0], b[1:]
        if op == 0x00 and rest[:1] in (b"\x02", b"\x03"):
            if rest[0] == 0x03:
                self._stop_all()
            self.set_state("power", "on" if rest[0] == 0x02 else "standby")
            return self._ack_done()
        if op == 0x07 and len(rest) == 1:
            return self._lens_drive("zoom_position", rest[0], 900)
        if op == 0x08 and len(rest) == 1:
            if self.get_state("focus_mode") == "auto" and rest[0] != 0x00:
                return self._error(0x41, 1)
            return self._lens_drive("focus_position", rest[0], 4000)
        if op == 0x47 and len(rest) in (4, 8):
            self._velocity.pop("zoom_position", None)
            self.set_state("zoom_position", max(0, min(0x4000, _decode_4nibble(rest[0:4]))))
            if len(rest) == 8:
                self.set_state("focus_position", _decode_4nibble(rest[4:8]))
            return self._ack_done()
        if op == 0x48 and len(rest) == 4:
            if self.get_state("focus_mode") == "auto":
                return self._error(0x41, 1)
            self.set_state("focus_position", _decode_4nibble(rest))
            return self._ack_done()
        if op == 0x38 and len(rest) == 1 and rest[0] in (0x02, 0x03, 0x10):
            if rest[0] == 0x10:
                mode = "manual" if self.get_state("focus_mode") == "auto" else "auto"
            else:
                mode = "auto" if rest[0] == 0x02 else "manual"
            self.set_state("focus_mode", mode)
            return self._ack_done()
        if op == 0x18 and rest == b"\x01":
            return self._ack_done()
        if op == 0x35 and len(rest) == 1 and rest[0] in _WB_FROM:
            self.set_state("wb_mode", _WB_FROM[rest[0]])
            return self._ack_done()
        if op == 0x10 and rest == b"\x05":
            return self._ack_done()
        if op in _LEVEL_STEP and len(rest) == 1 and rest[0] in (0x00, 0x02, 0x03):
            key = _LEVEL_STEP[op]
            top = _LEVEL_MAX.get(key, 0xFF)
            value = int(self.get_state(key, _LEVEL_DEFAULT[key]))
            if rest[0] == 0x00:
                value = _LEVEL_DEFAULT[key]
            elif rest[0] == 0x02:
                value = min(top, value + 1)
            else:
                value = max(0, value - 1)
            self.set_state(key, value)
            return self._ack_done()
        if op in _LEVEL_DIRECT and len(rest) == 4 and rest[:2] == b"\x00\x00":
            key = _LEVEL_DIRECT[op]
            value = ((rest[2] & 0x0F) << 4) | (rest[3] & 0x0F)
            if value > _LEVEL_MAX.get(key, 0xFF):
                return self._syntax()
            self.set_state(key, value)
            return self._ack_done()
        if op == 0x39 and len(rest) == 1 and rest[0] in _AE_FROM:
            self.set_state("ae_mode", _AE_FROM[rest[0]])
            return self._ack_done()
        if op in (0x3E, 0x33) and rest[:1] in (b"\x02", b"\x03"):
            self.set_state("exp_comp" if op == 0x3E else "backlight", rest[0] == 0x02)
            return self._ack_done()
        if op == 0x3F and len(rest) == 2 and rest[0] in (0x00, 0x01, 0x02):
            return self._memory(rest[0], rest[1])
        if op == 0x62 and len(rest) == 1 and rest[0] in (0x02, 0x03, 0x22, 0x23):
            key = "freeze" if rest[0] in (0x02, 0x03) else "preset_freeze"
            self.set_state(key, rest[0] in (0x02, 0x22))
            return self._ack_done()
        if op == 0xA4 and rest[:1] in (b"\x02", b"\x03"):
            if self._model not in _P_SERIES:
                return self._syntax()
            self.set_state("mount_mode", "stand" if rest[0] == 0x02 else "ceiling")
            return self._ack_done()
        return self._syntax()

    def _lens_drive(self, key: str, code: int, standard: int) -> bytes:
        if code == 0x00:
            self._drive(key, 0)
        elif code == 0x02:
            self._drive(key, standard)
        elif code == 0x03:
            self._drive(key, -standard)
        elif code & 0xF0 == 0x20:
            self._drive(key, standard * ((code & 0x0F) + 1) / 4)
        elif code & 0xF0 == 0x30:
            self._drive(key, -standard * ((code & 0x0F) + 1) / 4)
        else:
            return self._syntax()
        return self._ack_done()

    def _memory(self, op: int, number: int) -> bytes:
        reserved = _RESERVED[self._model]
        if op in (0x00, 0x01):
            if number in reserved:
                return self._error(0x41, 1)
            if op == 0x01:
                self._presets[number] = (
                    self.get_state("pan_position", 0), self.get_state("tilt_position", 0),
                    self.get_state("zoom_position", 0), self.get_state("focus_position", 0),
                )
            else:
                self._presets.pop(number, None)
            return self._ack_done()
        # Recall.
        if number in reserved:
            return self._reserved(number)
        self.set_state("last_preset", number)
        stored = self._presets.get(number)
        if stored:
            self._stop_all()
            for key, value in zip(
                ("pan_position", "tilt_position", "zoom_position", "focus_position"), stored
            ):
                self.set_state(key, value)
        return self._ack_done()

    def _reserved(self, number: int) -> bytes:
        if number == 80:
            self.set_state("tracking", "active")
        elif number == 81:
            self.set_state("tracking", "paused")
        elif number in (82, 83):
            self.set_state("group_tracking", number == 82)
        elif number == 84:
            self.set_state("switching_active", True)
        elif number in (85, 86):
            self.set_state("switching_active", False)
            self.set_state("switching_output", 1 if number == 85 else 2)
        elif number in (87, 89):
            self.set_state("intelligent_mode", "group_framing" if number == 87 else "speaker_tracking")
        elif number == 88:
            return self._error(0x41, 1)
        elif number == 95:
            self.set_state("osd_menu", not self.get_state("osd_menu", False))
        elif number == 99:
            self._reboot()
        elif 101 <= number <= 104:
            self.set_state("last_preset", number)
        elif 105 <= number <= 108:
            self.set_state("tracking_profile", number - 104)
        return self._ack_done()

    def _reboot(self) -> None:
        self.set_state("reboots", self.get_state("reboots", 0) + 1)
        self.set_state("osd_menu", False)
        self._stop_all()
        self._booting_until = time.monotonic() + BOOT_SECONDS
        try:
            asyncio.get_running_loop().call_later(0.2, self._drop_clients)
        except RuntimeError:
            pass

    def _drop_clients(self) -> None:
        for writer in list(getattr(self, "_clients", {}).values()):
            try:
                writer.close()
            except Exception:
                pass

    # ── Pan / tilt ──

    def _pan_tilt(self, b: bytes) -> bytes:
        if not b:
            return self._syntax()
        op, rest = b[0], b[1:]
        if op == 0x01 and len(rest) == 4:
            pan_speed, tilt_speed, pan_dir, tilt_dir = rest
            if not (1 <= pan_speed <= 0x18 and 1 <= tilt_speed <= 0x14):
                return self._syntax()
            pan_rate = {0x01: -1, 0x02: 1, 0x03: 0}.get(pan_dir)
            tilt_rate = {0x01: 1, 0x02: -1, 0x03: 0}.get(tilt_dir)
            if pan_rate is None or tilt_rate is None:
                return self._syntax()
            self._drive("pan_position", pan_rate * pan_speed * 54)
            self._drive("tilt_position", tilt_rate * tilt_speed * 50)
            return self._ack_done()
        if op in (0x02, 0x03) and len(rest) == 10:
            pan = _decode_4nibble(rest[2:6], signed=True)
            tilt = _decode_4nibble(rest[6:10], signed=True)
            if op == 0x03:
                pan += self.get_state("pan_position", 0)
                tilt += self.get_state("tilt_position", 0)
            self._velocity.pop("pan_position", None)
            self._velocity.pop("tilt_position", None)
            self.set_state("pan_position", max(_PAN_LIMIT[0], min(_PAN_LIMIT[1], pan)))
            self.set_state("tilt_position", max(_TILT_LIMIT[0], min(_TILT_LIMIT[1], tilt)))
            return self._ack_done()
        if op in (0x04, 0x05) and not rest:
            self._velocity.pop("pan_position", None)
            self._velocity.pop("tilt_position", None)
            self.set_state("pan_position", 0)
            self.set_state("tilt_position", 0)
            return self._ack_done()
        if op == 0x07 and len(rest) == 10 and rest[0] == 0x00 and rest[1] in (0x00, 0x01):
            return self._ack_done()
        if op == 0x08 and rest[:1] in (b"\x02", b"\x03"):
            if self._model not in ("i12", "i12d"):
                return self._syntax()
            self.set_state("ir_receiver", rest[0] == 0x02)
            return self._ack_done()
        return self._syntax()

    # ── Intelligent Switching ──

    def _switching(self, b: bytes) -> bytes:
        if self._model not in _SWITCHING_HOSTS:
            return self._syntax()
        if len(b) == 10 and b[0] == 0x09 and 2 <= b[1] <= 5:
            digits = b[2:10]
            octets = [((digits[i] & 0x0F) << 4) | (digits[i + 1] & 0x0F) for i in range(0, 8, 2)]
            self.set_state(f"camera_{b[1]}_ip", ".".join(str(o) for o in octets))
            return self._ack_done()
        if b == b"\x0a\x00":
            for camera in range(2, 6):
                self.set_state(f"camera_{camera}_ip", "")
            return self._ack_done()
        if len(b) == 2 and b[0] == 0x08 and 0 <= b[1] <= 5:
            if b[1] == 0:
                self.set_state("switching_active", True)
            else:
                self.set_state("switching_output", b[1])
            return self._ack_done()
        if b == b"\x0b\x00":
            self.set_state("switching_active", False)
            return self._ack_done()
        return self._syntax()

    # ── Inquiries ──

    def _inquiry(self, body: bytes) -> bytes:
        if body[:2] == b"\xc2\x09":
            return self._switching_inquiry(body[2:])
        b = body[1:]
        if b == b"\x04\x00":
            return self._answer(0x02 if self.get_state("power") == "on" else 0x03)
        if b == b"\x04\x47":
            return self._answer(*_encode_4nibble(self.get_state("zoom_position", 0)))
        if b == b"\x04\x48":
            return self._answer(*_encode_4nibble(self.get_state("focus_position", 0)))
        if b == b"\x04\x38":
            return self._answer(0x02 if self.get_state("focus_mode") == "auto" else 0x03)
        if b == b"\x04\x35":
            return self._answer(_WB_TO.get(self.get_state("wb_mode"), 0x00))
        if len(b) == 2 and b[0] == 0x04 and b[1] in _LEVEL_DIRECT:
            value = int(self.get_state(_LEVEL_DIRECT[b[1]], 0))
            return self._answer(0x00, 0x00, (value >> 4) & 0x0F, value & 0x0F)
        if b == b"\x04\x39":
            return self._answer(_AE_TO.get(self.get_state("ae_mode"), 0x00))
        if b in (b"\x04\x3e", b"\x04\x33"):
            on = self.get_state("exp_comp" if b[1] == 0x3E else "backlight")
            return self._answer(0x02 if on else 0x03)
        if b == b"\x04\x3f":
            return self._answer(int(self.get_state("last_preset", 0)) & 0xFF)
        if b == b"\x00\x02":
            code = _MODEL_CODES[self._model]
            return self._answer(0x00, 0x01, code >> 8, code & 0xFF, 0x01, 0x00, 0x02)
        if b == b"\x06\x23":
            return self._answer(_VIDEO_TO.get(self.get_state("video_format"), 0x07))
        if b == b"\x06\x08":
            if self._model not in ("i12", "i12d"):
                return self._syntax()
            return self._answer(0x02 if self.get_state("ir_receiver") else 0x03)
        if b == b"\x06\x11":
            return self._answer(0x18, 0x14)
        if b == b"\x06\x12":
            return self._answer(
                *_encode_4nibble(self.get_state("pan_position", 0)),
                *_encode_4nibble(self.get_state("tilt_position", 0)),
            )
        if b == b"\x06\x10":
            return self._answer(0x00, 0x00)
        if b == b"\x08\x01":
            if self._model not in _I_SERIES:
                return self._syntax()
            return self._answer(0x02 if self.get_state("tracking") == "active" else 0x03)
        if b == b"\x04\xa4":
            if self._model not in _P_SERIES:
                return self._syntax()
            return self._answer(0x02 if self.get_state("mount_mode") == "stand" else 0x03)
        return self._syntax()

    def _switching_inquiry(self, b: bytes) -> bytes:
        if self._model not in _SWITCHING_HOSTS:
            return self._syntax()
        if b == b"\x08":
            return self._answer(
                0x01 if self.get_state("switching_active") else 0x00,
                int(self.get_state("switching_output", 1)),
            )
        if len(b) == 2 and b[0] == 0x09 and 2 <= b[1] <= 5:
            ip = str(self.get_state(f"camera_{b[1]}_ip", "") or "0.0.0.0")
            digits: list[int] = []
            for octet in ip.split("."):
                value = int(octet)
                digits += [(value >> 4) & 0x0F, value & 0x0F]
            return self._answer(b[1], *digits)
        if len(b) == 2 and b[0] == 0x0D and 2 <= b[1] <= 5:
            connected = bool(self.get_state(f"camera_{b[1]}_ip", ""))
            return self._answer(0x00, 0x01 if connected else 0x00)
        return self._syntax()
