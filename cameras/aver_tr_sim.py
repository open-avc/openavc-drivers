"""
AVer TR615 / TR211 / TR315 / TR335 — Simulator

One simulated camera on one port number: the HTTP CGI (``/cgi-bin?...``) over
TCP and VISCA over IP over UDP, as the camera serves them on 80 and 52381.
The driver reaches both through the simulator's port (its VISCA datagrams
follow the platform's ``udp_redirect``).

What it models from the TR615 and TR V3 user manuals:

* VISCA over IP: an acknowledgement (90 41 FF) then a completion (90 51 FF)
  for each command it knows, an inquiry answered 90 50 ... FF, and a syntax
  error (90 60 02 FF) for anything else. The reply carries the request's
  sequence number.
* The CGI: ``Set=``, ``Get=trk_mode``, ``ActPreset=`` and ``OnePush=``, each
  answered 200. A request whose suffix repeats the previous request's is
  ignored, as the 2022 CGI document says the camera does.
* The web login: ``login_required`` answers 401 to the rows the manuals mark
  "GET (Basic Authentication)" when no credentials come with them, and
  ``wrong_password`` answers 401 to any request that carries credentials.

Driver: aver_tr
Transport: http (plus VISCA over IP on UDP, same port number)
"""

from __future__ import annotations

import logging
import struct
from typing import Any
from urllib.parse import urlsplit

from openavc.simulator.datagram_server import DatagramServerMixin
from openavc.simulator.http_simulator import HTTPSimulator

logger = logging.getLogger(__name__)

_REPLY = 0x0111
_CONTROL_REPLY = 0x0201

_AE = {0x00, 0x03, 0x0A, 0x0B, 0x0D}
_TALLY = {0x02: "red", 0x03: "off", 0x04: "amber", 0x05: "green"}
# The CGI rows the manuals mark "GET (Basic Authentication)".
_LOGIN_ROWS = ("Get=trk_mode", "Get=trk_tracking_on", "OnePush=", "Get=usb_status",
               "Get=uvc_status", "Set=trk_update_detect", "Get=trk_detect",
               "GetTrackingDetectZone", "Set=trk_assign_zone", "SetString=")


def _nibbles(data: bytes) -> int:
    value = 0
    for b in data:
        value = (value << 4) | (b & 0x0F)
    return value


def _to_nibbles(value: int, count: int = 4) -> bytes:
    return bytes((value >> (4 * (count - 1 - i))) & 0x0F for i in range(count))


class AverTrSimulator(DatagramServerMixin, HTTPSimulator):

    # Neither capacity model describes an HTTP server with a UDP side.
    CAPACITY_KIND = None

    SIMULATOR_INFO = {
        "driver_id": "aver_tr",
        "name": "AVer TR Auto Tracking Camera Simulator",
        "category": "camera",
        "transport": "http",
        "default_port": 80,
        "initial_state": {
            "power": True,
            "tracking": True,
            "trk_mode": 1,
            "body": 2,
            "pan": 0,
            "tilt": 0,
            "zoom": 0,
            "focus": 4096,
            "focus_auto": True,
            "last_preset": 0,
            "tally": "off",
            "freeze": False,
            "preset_freeze": False,
            "ae": 0,
            "shutter": 10,
            "iris": 8,
            "gain": 0,
            "bright": 7,
            "expcomp": 7,
            "blc": False,
            "wdr": False,
            "wb": 0,
            "rgain": 128,
            "bgain": 128,
            "osd": False,
            "usb": True,
            "uvc": False,
            "fw": "0.0.0000.36",
            "rtmp": False,
            "saturation": 5,
            "contrast": 2,
            "expo": 5,
            "flip": False,
            "theme": 0,
            "usb_bulk": 0,
            "preset_speed": 3,
            "af_mode": 1,
            "auto_zoom": False,
            "auto_tilt": False,
            "tracking_area": False,
            "multi_presenter": False,
            "preset_ptz_only": False,
            "relative_zoom": False,
        },
        "delays": {"command_response": 0.01},
        "error_modes": {
            "communication_timeout": {
                "description": "The camera stops answering HTTP and VISCA",
                "behavior": "no_response",
            },
            "login_required": {
                "description": (
                    "The camera has a web login: the rows that need it "
                    "answer 401 without credentials"
                ),
                "behavior": "custom",
            },
            "wrong_password": {
                "description": "The web password was changed on the camera (401)",
                "behavior": "custom",
            },
            "visca_off": {
                "description": "VISCA over IP answers nothing (another port set on the camera)",
                "behavior": "custom",
            },
        },
        "controls": [
            {"type": "toggle", "key": "power", "label": "Power On"},
            {"type": "toggle", "key": "tracking", "label": "Auto Tracking"},
            {
                "type": "select", "key": "trk_mode", "label": "Tracking Mode",
                "options": [
                    {"value": 1, "label": "Presenter"},
                    {"value": 2, "label": "Zone"},
                    {"value": 3, "label": "Hybrid"},
                    {"value": 6, "label": "Framing"},
                ],
            },
            {
                "type": "select", "key": "tally", "label": "Tally",
                "options": [
                    {"value": "off", "label": "Off"},
                    {"value": "red", "label": "Red"},
                    {"value": "amber", "label": "Amber"},
                    {"value": "green", "label": "Green"},
                ],
            },
            {
                "type": "select", "key": "ae", "label": "Exposure Mode",
                "options": [
                    {"value": 0, "label": "Full Auto"},
                    {"value": 3, "label": "Manual"},
                    {"value": 10, "label": "Shutter Priority"},
                    {"value": 11, "label": "Iris Priority"},
                    {"value": 13, "label": "Bright"},
                ],
            },
            {
                "type": "select", "key": "wb", "label": "White Balance",
                "options": [
                    {"value": 0, "label": "Auto"},
                    {"value": 1, "label": "Indoor"},
                    {"value": 2, "label": "Outdoor"},
                    {"value": 3, "label": "One Push"},
                    {"value": 4, "label": "ATW"},
                    {"value": 5, "label": "Manual"},
                ],
            },
            {"type": "toggle", "key": "usb", "label": "USB Cable Connected"},
            {"type": "toggle", "key": "uvc", "label": "USB Video Streaming"},
            {"type": "indicator", "key": "fw", "label": "Firmware"},
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None) -> None:
        super().__init__(device_id, config)
        self._presets: dict[int, tuple[int, int, int]] = {}
        self._last_suffix = ""

    async def start(self, port: int) -> None:
        await super().start(port)
        try:
            await self.start_datagram_server(self.port)
        except OSError as exc:
            logger.warning("%s: VISCA not served on UDP %d: %s", self.name, self.port, exc)

    async def stop(self) -> None:
        await self.stop_datagram_server()
        await super().stop()

    def _i(self, key: str) -> int:
        try:
            return int(self.state.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    # ── HTTP CGI ──

    def handle_request(
        self, method: str, path: str, headers: dict[str, str], body: str
    ) -> tuple[int, Any]:
        parts = urlsplit(path)
        if parts.path != "/cgi-bin":
            return 404, "not found"
        query = parts.query
        has_auth = any(
            k.lower() == "authorization" and str(v).lower().startswith("basic ")
            for k, v in headers.items()
        )
        if "wrong_password" in self.active_errors and has_auth:
            return 401, "unauthorized"
        if (
            "login_required" in self.active_errors
            and not has_auth
            and query.startswith(_LOGIN_ROWS)
        ):
            return 401, "unauthorized"
        head, _, suffix = query.partition("&")
        if suffix and suffix == self._last_suffix:
            # "This ID cannot be the same, otherwise the camera will ignore
            # this command." (HTTP CGI Commands, 6/2/2022)
            return 200, ""
        if suffix:
            self._last_suffix = suffix
        return self._cgi(head)

    def _cgi(self, head: str) -> tuple[int, str]:
        key, _, value = head.partition("=")
        if key == "Get":
            name = value.split(",", 1)[0]
            if name == "trk_mode":
                return 200, f"trk_mode,3={self._i('trk_mode')}\r\n"
            if name == "trk_tracking_on":
                return 200, f"trk_tracking_on,3={1 if self.state.get('tracking') else 0}\r\n"
            return 404, "not found"
        if key == "Set":
            fields = value.split(",")
            if len(fields) != 3 or fields[1] != "3":
                return 400, "bad request"
            name, raw = fields[0], fields[2]
            try:
                n = int(raw)
            except ValueError:
                return 400, "bad request"
            setters = {
                "trk_mode": ("trk_mode", (1, 2, 3, 6)),
                "trk_tracking_on": ("tracking", (0, 1)),
                "img_saturation": ("saturation", range(0, 11)),
                "img_contrast": ("contrast", range(0, 5)),
                "img_expo_expo": ("expo", range(1, 10)),
                "img_flip": ("flip", (0, 1)),
                "sys_theme_mode": ("theme", (0, 1, 2, 3, 4, 7)),
                "usb_bulk_mode_en": ("usb_bulk", (0, 1)),
                "preset_speed": ("preset_speed", range(1, 7)),
                "vdo_rtmp_enable": ("rtmp", (0, 1)),
            }
            if name not in setters:
                return 404, "not found"
            state_key, allowed = setters[name]
            if n not in allowed:
                return 400, "bad request"
            if state_key in ("tracking", "flip", "rtmp"):
                self.set_state(state_key, bool(n))
            else:
                self.set_state(state_key, n)
            return 200, "ok"
        if key == "ActPreset":
            fields = value.split(",")
            if len(fields) != 2 or fields[0] not in ("0", "1") or not fields[1].isdigit():
                return 400, "bad request"
            number = int(fields[1])
            if not 0 <= number <= 255:
                return 400, "bad request"
            if fields[0] == "1":
                self._save_preset(number)
            else:
                self._recall_preset(number)
            return 200, "ok"
        if key == "OnePush":
            if value == "d":
                self._factory_defaults()
            return 200, "ok"
        return 404, "not found"

    def _save_preset(self, number: int) -> None:
        self._presets[number] = (self._i("pan"), self._i("tilt"), self._i("zoom"))
        self.set_state("last_preset", number)

    def _recall_preset(self, number: int) -> None:
        if number in self._presets:
            pan, tilt, zoom = self._presets[number]
            self.set_state("pan", pan)
            self.set_state("tilt", tilt)
            self.set_state("zoom", zoom)
        self.set_state("last_preset", number)

    def _factory_defaults(self) -> None:
        for key, value in self.SIMULATOR_INFO["initial_state"].items():
            self.set_state(key, value)
        self._presets.clear()

    # ── VISCA over IP ──

    def handle_command(self, data: bytes) -> bytes | None:
        return None  # every datagram goes through dispatch_datagram below

    async def dispatch_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        if "visca_off" in self.active_errors or len(data) < 9:
            return
        payload_type, length, sequence = struct.unpack(">HHI", data[:8])
        payload = data[8:]
        if len(payload) != length:
            return

        def reply(kind: int, body: bytes) -> None:
            self.send_datagram(struct.pack(">HHI", kind, len(body), sequence) + body, addr)

        if payload_type == 0x0200:
            reply(_CONTROL_REPLY, b"\x01")
            return
        if payload_type == 0x0110:
            answer = self._inquiry(payload)
            reply(_REPLY, b"\x90\x50" + answer + b"\xff" if answer is not None else b"\x90\x60\x02\xff")
            return
        if payload_type == 0x0100:
            if self._command(payload):
                reply(_REPLY, b"\x90\x41\xff")
                reply(_REPLY, b"\x90\x51\xff")
            else:
                reply(_REPLY, b"\x90\x60\x02\xff")

    def _flag(self, key: str) -> bytes:
        return b"\x02" if self.state.get(key) else b"\x03"

    def _inquiry(self, p: bytes) -> bytes | None:
        s = self.state
        table: dict[bytes, Any] = {
            b"\x81\x09\x04\x00\xff": lambda: self._flag("power"),
            b"\x81\x09\x36\x69\x02\xff": lambda: b"\x01" if s.get("tracking") else b"\x00",
            b"\x81\x09\x36\x69\x01\xff": lambda: bytes([min(self._i("trk_mode"), 3)]),
            b"\x81\x09\x36\x69\x03\xff": lambda: bytes([self._i("body") or 1]),
            b"\x81\x09\x06\x12\xff": lambda: _to_nibbles(self._i("pan")) + _to_nibbles(self._i("tilt")),
            b"\x81\x09\x04\x47\xff": lambda: _to_nibbles(self._i("zoom")),
            b"\x81\x09\x04\x48\xff": lambda: _to_nibbles(self._i("focus")),
            b"\x81\x09\x04\x38\xff": lambda: self._flag("focus_auto"),
            b"\x81\x09\x04\x3f\xff": lambda: bytes([self._i("last_preset") & 0xFF]),
            b"\x81\x09\x7e\x01\x0a\xff": lambda: b"\x03" if s.get("tally") in (None, "off") else b"\x02",
            b"\x81\x09\x04\x62\x01\xff": lambda: self._flag("freeze"),
            b"\x81\x09\x04\x62\x02\xff": lambda: self._flag("preset_freeze"),
            b"\x81\x09\x04\x39\xff": lambda: bytes([self._i("ae")]),
            b"\x81\x09\x04\x4a\xff": lambda: b"\x00\x00" + _to_nibbles(self._i("shutter"), 2),
            b"\x81\x09\x04\x4b\xff": lambda: b"\x00\x00" + _to_nibbles(self._i("iris"), 2),
            b"\x81\x09\x04\x4c\xff": lambda: b"\x00\x00" + _to_nibbles(self._i("gain"), 2),
            b"\x81\x09\x04\x4d\xff": lambda: b"\x00\x00" + _to_nibbles(self._i("bright"), 2),
            b"\x81\x09\x04\x4e\xff": lambda: b"\x00\x00" + _to_nibbles(self._i("expcomp"), 2),
            b"\x81\x09\x04\x33\xff": lambda: self._flag("blc"),
            b"\x81\x09\x04\x3d\xff": lambda: self._flag("wdr"),
            b"\x81\x09\x04\x35\xff": lambda: bytes([self._i("wb")]),
            b"\x81\x09\x04\x43\xff": lambda: b"\x00\x00" + _to_nibbles(self._i("rgain"), 2),
            b"\x81\x09\x04\x44\xff": lambda: b"\x00\x00" + _to_nibbles(self._i("bgain"), 2),
            b"\x81\x09\x7e\x04\x76\x01\xff": lambda: self._flag("osd"),
            b"\x81\x09\x36\x69\x05\xff": lambda: b"\x01" if s.get("usb") else b"\x00",
            b"\x81\x09\x36\x69\x06\xff": lambda: b"\x01" if s.get("uvc") else b"\x00",
            b"\x81\x09\x36\x69\x04\xff": self._firmware,
        }
        fn = table.get(bytes(p))
        return fn() if fn else None

    def _firmware(self) -> bytes:
        digits = str(self.state.get("fw") or "0.0.0000.00").replace(".", "")
        digits = (digits + "00000000")[:8]
        return bytes(int(c, 16) & 0x0F for c in digits)

    def _step(self, key: str, direction: int) -> None:
        self.set_state(key, max(0, min(255, self._i(key) + (1 if direction == 0x02 else -1))))

    def _command(self, p: bytes) -> bool:
        if len(p) < 4 or p[0] != 0x81 or p[1] != 0x01 or p[-1] != 0xFF:
            return False
        body = bytes(p[2:-1])
        flags = {0x33: "blc", 0x3D: "wdr", 0x7D: "tracking", 0xA0: "auto_zoom",
                 0xA1: "tracking_area", 0xA2: "rtmp", 0xA5: "preset_ptz_only",
                 0xA6: "relative_zoom", 0xA7: "auto_tilt", 0xA9: "multi_presenter"}
        steps = {0x03: "rgain", 0x04: "bgain", 0x0A: "shutter", 0x0B: "iris",
                 0x0C: "gain", 0x0D: "bright", 0x0E: "expcomp"}
        if body[0] == 0x04 and len(body) == 3:
            code, arg = body[1], body[2]
            if code == 0x00 and arg in (0x02, 0x03):
                self.set_state("power", arg == 0x02)
            elif code in flags and arg in (0x02, 0x03):
                self.set_state(flags[code], arg == 0x02)
            elif code in steps and arg in (0x02, 0x03):
                self._step(steps[code], arg)
            elif code == 0x07 and (arg == 0x00 or 0x20 <= arg <= 0x37):
                pass  # zoom drive / stop: the position holds in simulation
            elif code == 0x08 and arg in (0x00, 0x02, 0x03):
                pass  # focus drive / stop
            elif code == 0x38 and arg in (0x02, 0x03):
                self.set_state("focus_auto", arg == 0x02)
            elif code == 0x18 and arg == 0x01:
                pass  # one push AF
            elif code == 0x57 and arg in (0x00, 0x01, 0x02):
                self.set_state("af_mode", arg)
            elif code == 0x35 and arg in range(0, 6):
                self.set_state("wb", arg)
            elif code == 0x10 and arg == 0x05:
                pass  # one push WB trigger
            elif code == 0x39 and arg in _AE:
                self.set_state("ae", arg)
            elif code == 0x62 and arg in (0x02, 0x03):
                self.set_state("freeze", arg == 0x02)
            elif code == 0x62 and arg in (0x22, 0x23):
                self.set_state("preset_freeze", arg == 0x22)
            elif code == 0xA3 and arg in range(0, 5):
                self.set_state("theme", arg)
            else:
                return False
            return True
        if body == b"\x04\xa4":
            return True  # reboot
        if body[0] == 0x04 and len(body) == 4 and body[1] == 0x3F and body[2] in (0, 1, 2):
            number = body[3]
            if body[2] == 0x00:
                self._presets.pop(number, None)
            elif body[2] == 0x01:
                self._save_preset(number)
            else:
                self._recall_preset(number)
            return True
        if body[0] == 0x04 and len(body) == 6 and body[1] == 0x47:
            self.set_state("zoom", _nibbles(body[2:6]))
            return True
        if body[0] == 0x06 and len(body) == 6 and body[1] == 0x01:
            return 1 <= body[2] <= 0x18 and 1 <= body[3] <= 0x18
        if body[0] == 0x06 and len(body) == 12 and body[1] == 0x02:
            self.set_state("pan", _nibbles(body[4:8]))
            self.set_state("tilt", _nibbles(body[8:12]))
            return True
        if body in (b"\x06\x04", b"\x06\x05"):
            self.set_state("pan", 0)
            self.set_state("tilt", 0)
            return True
        if body == b"\x06\x06\x10":
            self.set_state("osd", not self.state.get("osd"))
            return True
        if body == b"\x7e\x01\x02\x00\x01":
            return True  # menu enter
        if body[:4] == b"\x7e\x01\x0a\x00" and len(body) == 5 and body[4] in _TALLY:
            self.set_state("tally", _TALLY[body[4]])
            return True
        return False
