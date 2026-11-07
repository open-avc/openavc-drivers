"""
Sony SRG-A40 / SRG-A12 — Simulator

One simulated camera on one port number: the HTTP CGI over TCP and VISCA
over IP over UDP, as the camera serves them on 80 and 52381. The driver
reaches both through the simulator's port (its VISCA datagrams follow the
platform's ``udp_redirect``).

What it models from Sony's VISCA Command List / CGI Command List for the
SRG-A40 / A12 (Software Version 4.0):

* VISCA over IP: an acknowledgement (90 41 FF) then a completion (90 51 FF)
  for each command it knows, an inquiry answered 90 50 ... FF, a syntax
  error (90 60 02 FF) for anything else or a value out of range, and "not
  executable" (90 61 41 FF) for a command sent while the on-screen menu is
  open (power and the menu itself excepted). A message whose sequence number
  is the one the camera last saw is answered with the control ERROR 0F 01;
  RESET starts the count again.
* The tally lamp goes out 15 s after the last Tally On ("The tally lamp is
  turned off if not receiving an ON command from any controller for 15
  seconds").
* The CGI: ``/command/inquiry.cgi?inq=<group>[&inq=...]`` answered
  ``name=value&...``; ``/command/<cgi>`` and ``/analytics/<cgi>`` settings,
  each checked against the document's values. HTTP Digest: a request with no
  ``Authorization: Digest`` is answered 401 with a challenge. A request with
  no Referer header is answered 403, as the factory Referer Check does.
* ``model`` in the simulator's config (SRG-A40 or SRG-A12): the A12 has
  optical zoom only, to 4000h.
* Error modes: the camera stops answering; the password was changed (every
  signed request 401); a Light / viewer account (every setting 403); VISCA
  switched off (CAMERA SETUP switch 3); the web interface silent.

Driver: sony_srg_a
Transport: http (plus VISCA over IP on UDP, same port number)
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from openavc.simulator.datagram_server import DatagramServerMixin
from openavc.simulator.http_simulator import HTTPSimulator

logger = logging.getLogger(__name__)

_REPLY = 0x0111
_CONTROL_CMD = 0x0200
_CONTROL_REPLY = 0x0201

# One-code VISCA settings: group (the bytes after 8x 01 / 8x 09) -> (state
# key, the codes the camera takes). Set 8x 01 <group> pp FF; inquiry
# 8x 09 <group> FF -> y0 50 pp FF.
_CODES: dict[bytes, tuple[str, frozenset[int]]] = {
    b"\x04\x39": ("ae", frozenset({0x00, 0x03, 0x0A, 0x0B})),
    b"\x04\x35": ("wb", frozenset(range(6))),
    b"\x04\x63": ("effect", frozenset({0x00, 0x04})),
    b"\x04\x06": ("zoom_mode", frozenset({0x02, 0x03, 0x04})),
    b"\x04\x57": ("af_mode", frozenset({0, 1, 2})),
    b"\x04\x58": ("af_sens", frozenset({2, 3})),
    b"\x04\x11": ("ir_corr", frozenset({0, 1})),
    b"\x7e\x04\x50": ("standby_mode", frozenset({2, 3})),
    b"\x7e\x04\x3b": ("call_mode", frozenset({2, 3})),
    b"\x04\x56": ("wb_speed", frozenset(range(1, 6))),
    b"\x04\x5f": ("chroma", frozenset(range(4))),
    b"\x04\x53": ("nr", frozenset(range(6))),
    b"\x04\x33": ("backlight", frozenset({2, 3})),
    b"\x04\x3a": ("spotlight", frozenset({2, 3})),
    b"\x04\x5e": ("high_sens", frozenset({2, 3})),
    b"\x04\x3e": ("expcomp_on", frozenset({2, 3})),
    b"\x04\x5a": ("slow_shutter", frozenset({2, 3})),
    b"\x04\x3d": ("visibility", frozenset({6, 3})),
    b"\x05\x39": ("low_light", frozenset({2, 3})),
    b"\x04\x32": ("flicker", frozenset({2, 3})),
    b"\x04\x34": ("stabilizer", frozenset({2, 3})),
    b"\x04\x52": ("high_res", frozenset({2, 3})),
    b"\x7e\x04\x36": ("tele_convert", frozenset({2, 3})),
    b"\x04\x66": ("flip", frozenset({2, 3})),
    b"\x06\x08": ("ir_receive", frozenset({2, 3})),
    b"\x06\x44": ("pt_slow", frozenset({2, 3})),
}

# Values sent as 00 00 0p 0q: group -> (state key, lowest, highest), and the
# Reset / Up / Down group that steps it.
_LEVELS: dict[bytes, tuple[str, int, int]] = {
    b"\x04\x4b": ("iris", 0x00, 0x19),
    b"\x04\x4c": ("gain", 0x01, 0x11),
    b"\x04\x4a": ("shutter", 0x06, 0x21),
    b"\x04\x4e": ("expcomp", 0x00, 0x0E),
    b"\x04\x43": ("rgain", 0x00, 0xFF),
    b"\x04\x44": ("bgain", 0x00, 0xFF),
    b"\x04\x42": ("detail", 0x00, 0x0F),
}
_STEPS: dict[bytes, tuple[bytes, int]] = {
    b"\x04\x0b": (b"\x04\x4b", 0x19), b"\x04\x0c": (b"\x04\x4c", 0x01),
    b"\x04\x0a": (b"\x04\x4a", 0x12), b"\x04\x0e": (b"\x04\x4e", 0x07),
    b"\x04\x03": (b"\x04\x43", 0x80), b"\x04\x04": (b"\x04\x44", 0x80),
    b"\x04\x02": (b"\x04\x42", 0x07),
}

_ON_OFF_WORD = ("on", "off")

# CGI settings: parameter -> (state key, allowed values or (low, high)).
_CGI: dict[str, tuple[str, Any]] = {
    "PtzAutoFramingFramingMode": ("framing_mode", ("person", "ball_sports")),
    "PtzAutoFramingTriggerType": ("trigger", ("auto", "manual")),
    "PtzAutoFramingAutoStartEnable": ("auto_start", _ON_OFF_WORD),
    "PtzAutoFramingMultiTrackingEnable": ("multi", _ON_OFF_WORD),
    "PtzAutoFramingMultiTrackingTargetNum": ("multi_num", (2, 8)),
    "PtzAutoFramingMultiTrackingWaitTime": ("multi_wait", (0, 60)),
    "PtzAutoFramingLeadRoomLevel": ("lead_room", ("off", "low", "middle", "high")),
    "PtzAutoFramingFaceIndicatorEnable3": ("face_ind", _ON_OFF_WORD),
    "PtzAutoFramingIndividualIdentification": ("individual", _ON_OFF_WORD),
    "PtzAutoFramingRegisteredFaceTracking": ("reg_face", _ON_OFF_WORD),
    "PtzAutoFramingLostWaitTime": ("lost_wait", (1, 60)),
    "PtzAutoFramingFaceLostWaitTime": ("face_lost_wait", (0, 60)),
    "PtzAutoFramingDetectionRestartTime": ("restart_time", (0, 600)),
    "TallyLevel": ("tally_level", ("off", "low", "high")),
    "TallyIndicationPtzAutoFraming": ("tally_af", _ON_OFF_WORD),
    "StreamMode": ("stream_mode", ("rtsp", "rtmp", "srt-caller", "srt-listener", "ndi_hx", "off")),
    "NdiSourceName": ("ndi_name", None),
}
_GROUP_OF = {
    "tally": ("TallyLevel", "TallyIndicationPtzAutoFraming"),
    "stream": ("StreamMode",),
    "ndi": ("NdiSourceName",),
}

_EXE = {
    "PtzAutoFramingRestart": "on", "PtzAutoFramingDecideStartPosition": "on",
    "PtzAutoFramingMoveStartPosition": "on", "PtzAutoFramingFixedAnglePositionSet": "1",
    "PtzAutoFramingFixedAnglePositionCall": "1",
}


def _nibbles(data: bytes) -> int:
    value = 0
    for b in data:
        value = (value << 4) | (b & 0x0F)
    return value


def _to_nibbles(value: int, count: int = 4) -> bytes:
    return bytes((value >> (4 * (count - 1 - i))) & 0x0F for i in range(count))


class SonySrgASimulator(DatagramServerMixin, HTTPSimulator):

    # Neither capacity model describes an HTTP server with a UDP side.
    CAPACITY_KIND = None

    SIMULATOR_INFO = {
        "driver_id": "sony_srg_a",
        "name": "Sony SRG-A40 / SRG-A12 Simulator",
        "category": "camera",
        "transport": "http",
        "default_port": 80,
        "initial_state": {
            "power": True,
            "af": False,
            "af_status": "idle",
            "pan": 0,
            "tilt": 0,
            "zoom": 0,
            "focus": 0x1000,
            "focus_auto": True,
            "last_preset": 0x7F,
            "tally": False,
            "osd": False,
            "ae": 0,
            "iris": 0x19,
            "gain": 0x01,
            "shutter": 0x12,
            "expcomp": 0x07,
            "wb": 0,
            "rgain": 0x80,
            "bgain": 0x80,
            "wb_speed": 3,
            "chroma": 0,
            "detail": 0x07,
            "nr": 2,
            "defog": 3,
            "defog_level": 0,
            "effect": 0,
            "zoom_mode": 3,
            "af_mode": 0,
            "af_sens": 2,
            "ir_corr": 0,
            "standby_mode": 3,
            "call_mode": 3,
            "hdmi_color": 0,
            "video_format": 0x04,
            "backlight": 3,
            "spotlight": 3,
            "high_sens": 3,
            "expcomp_on": 3,
            "slow_shutter": 3,
            "visibility": 3,
            "low_light": 3,
            "flicker": 3,
            "stabilizer": 3,
            "high_res": 3,
            "tele_convert": 3,
            "flip": 3,
            "ir_receive": 2,
            "pt_slow": 3,
            "framing_mode": "person",
            "trigger": "auto",
            "auto_start": "off",
            "multi": "off",
            "multi_num": 2,
            "multi_wait": 0,
            "lead_room": "off",
            "face_ind": "off",
            "individual": "on",
            "reg_face": "off",
            "lost_wait": 6,
            "face_lost_wait": 0,
            "restart_time": 0,
            "comp_v": 0,
            "comp_h": 0,
            "comp_size": 1200,
            "tally_level": "high",
            "tally_af": "off",
            "stream_mode": "rtsp",
            "streaming": False,
            "ndi_name": "SRG-A40",
            "rtmp_url": "",
            "rtmp_key": "",
            "srt_url": "",
            "temp_warning": False,
        },
        "delays": {"command_response": 0.01},
        "error_modes": {
            "communication_timeout": {
                "description": "The camera stops answering HTTP and VISCA",
                "behavior": "no_response",
            },
            "wrong_password": {
                "description": "The web password was changed on the camera (401)",
                "behavior": "custom",
            },
            "viewer_account": {
                "description": "The web account may only watch: every setting answers 403",
                "behavior": "custom",
            },
            "visca_off": {
                "description": "VISCA over IP answers nothing (CAMERA SETUP switch 3 off)",
                "behavior": "custom",
            },
            "web_off": {
                "description": "The web interface answers nothing while VISCA does",
                "behavior": "custom",
            },
        },
        "controls": [
            {"type": "toggle", "key": "power", "label": "Power On"},
            {"type": "toggle", "key": "af", "label": "PTZ Auto Framing"},
            {
                "type": "select", "key": "af_status", "label": "Auto Framing Status",
                "options": [
                    {"value": v, "label": v.replace("_", " ").title()}
                    for v in (
                        "idle", "missing", "preparing", "searching", "tracking",
                        "waiting", "fixed_angle",
                    )
                ],
            },
            {"type": "toggle", "key": "tally", "label": "Tally Lamp"},
            {"type": "toggle", "key": "osd", "label": "On-Screen Menu Open"},
            {"type": "toggle", "key": "temp_warning", "label": "Temperature Warning"},
            {"type": "toggle", "key": "streaming", "label": "Streaming"},
            {"type": "indicator", "key": "pan", "label": "Pan"},
            {"type": "indicator", "key": "zoom", "label": "Zoom"},
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None) -> None:
        super().__init__(device_id, config)
        self._presets: dict[int, tuple[int, int, int, str]] = {}
        self._comp_names = {1: "Full Body", 2: "Waist", 3: "Closeup"}
        self._last_seq = 0
        self._tally_lit = 0.0
        # The camera's 15 s; a proof shortens it.
        self.tally_expiry_s = 15.0
        model = str((config or {}).get("model") or "SRG-A40")
        self.model = model if model in ("SRG-A40", "SRG-A12") else "SRG-A40"

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

    def _zoom_max(self) -> int:
        return 0x4000 if self.model == "SRG-A12" else 0x7AC0

    def _tally_on(self) -> bool:
        if self.state.get("tally") and self._tally_lit and (
            time.monotonic() - self._tally_lit > self.tally_expiry_s
        ):
            self.set_state("tally", False)
        return bool(self.state.get("tally"))

    # ── HTTP CGI ──

    async def respond_http(self, request, method, path, headers, body):
        if "web_off" in self.active_errors:
            # The web server is not running: the request is never answered.
            await asyncio.sleep(30)
        return await super().respond_http(request, method, path, headers, body)

    def handle_request(self, method: str, path: str, headers: dict[str, str], body: str):
        lower = {k.lower(): str(v) for k, v in headers.items()}
        auth = lower.get("authorization", "")
        if not auth.lower().startswith("digest ") or "wrong_password" in self.active_errors:
            challenge = (
                f'Digest realm="Sony Network Camera {self.model}", '
                f'nonce="{int(time.time() * 1000):x}", qop="auth", algorithm=MD5'
            )
            return 401, "Unauthorized", {"WWW-Authenticate": challenge}
        if "referer" not in lower:
            # Referer Check, on at the factory.
            return 403, "Forbidden"
        parts = urlsplit(path)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        if parts.path == "/command/inquiry.cgi":
            return self._inquiry_cgi(pairs)
        if "viewer_account" in self.active_errors:
            return 403, "Forbidden"
        cgi = parts.path.rsplit("/", 1)[-1]
        folder = parts.path.rsplit("/", 2)[-2] if parts.path.count("/") >= 2 else ""
        analytics = cgi in ("ptzautoframing.cgi", "ptzautoframingexe.cgi")
        if folder != ("analytics" if analytics else "command"):
            return 404, "Not Found"
        for name, value in pairs:
            status = self._set_cgi(cgi, name, value)
            if status != 200:
                return status, "Bad Request" if status == 400 else "Not Found"
        return 200, ""

    def _inquiry_cgi(self, pairs: list[tuple[str, str]]):
        s = self.state
        out: list[str] = []
        for key, group in pairs:
            if key != "inq":
                return 400, "Bad Request"
            if group == "ptzautoframing":
                multi = s.get("multi") == "on"
                out += [
                    f"PtzAutoFraming={'on' if s.get('af') else 'off'}",
                    f"PtzAutoFramingTrackingStatus={s.get('af_status') or 'idle'}",
                    f"PtzAutoFramingAdjustSetting={self._i('comp_v')},{self._i('comp_h')},{self._i('comp_size')}",
                    f"PtzAutoFramingMultiTrackingCurrentTargetNum={(self._i('multi_num') if multi else 1) if s.get('af') else 0},8",
                    "PtzAutoFramingPresetName=" + ",".join(
                        f"{n},{name}" for n, name in sorted(self._comp_names.items())
                    ),
                ]
                out += [
                    f"{name}={s.get(key)}" for name, (key, _a) in _CGI.items()
                    if name.startswith("PtzAutoFraming")
                ]
            elif group == "presetposition":
                out += [
                    "PresetName=" + ",".join(
                        f"{n},{p[3]}" for n, p in sorted(self._presets.items())
                    ),
                    "PresetNum=256",
                    f"CallMode={'freeze' if self._i('call_mode') == 2 else 'normal'}",
                ]
            elif group in _GROUP_OF:
                out += [f"{name}={s.get(_CGI[name][0])}" for name in _GROUP_OF[group]]
                if group == "tally":
                    out.append(f"TallyControl={'on' if self._tally_on() else 'off'}")
                if group == "stream":
                    out.append(f"StreamStatus={self._stream_status()}")
            elif group == "status":
                out.append(f"TemperatureWarning={1 if s.get('temp_warning') else 0}")
            elif group == "rtmp":
                out += [
                    f"RtmpServerUrl1={s.get('rtmp_url') or ''}",
                    f"RtmpStreamKey1Used={1 if s.get('rtmp_key') else 0}",
                    "RtmpCertInstalled=0",
                ]
            elif group == "srt":
                out += [f"SrtServerUrl1={s.get('srt_url') or ''}", "SrtListenPort=4201"]
            elif group == "system":
                out += [f"ModelName={self.model}", "Serial=01234567", "BuildNumber=4000"]
            elif group == "sysinfo":
                out += [
                    "SoftVersion=4.00",
                    f"Power={'on' if s.get('power') else 'standby'}",
                    "PasswordChanged=1",
                ]
            else:
                return 400, "Bad Request"
        return 200, "&".join(out)

    def _stream_status(self) -> str:
        if not self.state.get("streaming"):
            return "off" if self.state.get("stream_mode") == "srt-caller" else "invalid"
        return "streaming"

    def _set_cgi(self, cgi: str, name: str, value: str) -> int:
        if cgi in ("ptzautoframing.cgi", "tally.cgi", "stream.cgi", "ndi.cgi") and name in _CGI:
            key, allowed = _CGI[name]
            if allowed is None:
                if len(value.encode("utf-8")) > 64:
                    return 400
                self.set_state(key, value)
                return 200
            if isinstance(allowed, tuple) and allowed and isinstance(allowed[0], int):
                try:
                    number = int(value)
                except ValueError:
                    return 400
                if not allowed[0] <= number <= allowed[1]:
                    return 400
                self.set_state(key, number)
                return 200
            if value not in allowed:
                return 400
            if name == "StreamMode" and value != self.state.get("stream_mode"):
                self.set_state("streaming", False)
            self.set_state(key, value)
            return 200
        handler = getattr(self, f"_cgi_{cgi.replace('.cgi', '').replace('-', '_')}", None)
        if handler is None:
            return 404
        return handler(name, value)

    def _cgi_ptzautoframing(self, name: str, value: str) -> int:
        fields = value.split(",")
        if name == "PtzAutoFraming" and value in _ON_OFF_WORD:
            self._set_af(value == "on")
        elif name == "PtzAutoFramingAdjustSetting" and len(fields) == 3:
            try:
                v, h, size = (int(f) for f in fields)
            except ValueError:
                return 400
            if not (-50 <= v <= 70 and -50 <= h <= 50 and 160 <= size <= 3500):
                return 400
            self.set_state("comp_v", v)
            self.set_state("comp_h", h)
            self.set_state("comp_size", size)
        elif name == "PtzAutoFramingPresetCall" and value in ("1", "2", "3"):
            # A recalled composition: the simulator keeps one per preset number.
            self.set_state("comp_size", {"1": 600, "2": 1200, "3": 2400}[value])
        elif name == "PtzAutoFramingPresetSet" and len(fields) == 2 and fields[0] in ("1", "2", "3"):
            if len(fields[1].encode("utf-8")) > 32 or set(fields[1]) & set('"%&,:~'):
                return 400
            self._comp_names[int(fields[0])] = fields[1]
        elif name == "PtzAutoFramingPresetReset" and value in ("1", "2", "3"):
            self._comp_names[int(value)] = {1: "Full Body", 2: "Waist", 3: "Closeup"}[int(value)]
        elif name == "PtzAutoFramingObjectPosition" and len(fields) == 2:
            try:
                x, y = (int(f) for f in fields)
            except ValueError:
                return 400
            if not (0 <= x <= 10000 and 0 <= y <= 10000):
                return 400
            if self.state.get("af"):
                self.set_state("af_status", "tracking")
        else:
            return 400
        return 200

    def _cgi_ptzautoframingexe(self, name: str, value: str) -> int:
        if _EXE.get(name) != value:
            return 400
        if name == "PtzAutoFramingRestart" and self.state.get("af"):
            self.set_state("af_status", "searching")
        return 200

    def _cgi_presetposition(self, name: str, value: str) -> int:
        fields = value.split(",")
        try:
            number = int(fields[0])
        except ValueError:
            return 400
        if not 1 <= number <= 256:
            return 400
        if name == "PresetSet" and len(fields) == 3 and fields[2] in _ON_OFF_WORD:
            if len(fields[1].encode("utf-8")) > 32:
                return 400
            self._save_preset(number, fields[1])
        elif name == "PresetCall" and len(fields) == 1:
            self._recall_preset(number)
        elif name == "PresetClear" and len(fields) == 1:
            self._presets.pop(number, None)
        elif name == "PresetName" and len(fields) == 2:
            if number not in self._presets:
                return 400
            pan, tilt, zoom, _old = self._presets[number]
            self._presets[number] = (pan, tilt, zoom, fields[1])
        else:
            return 400
        return 200

    def _cgi_rtmpexe(self, name: str, value: str) -> int:
        if name != "RtmpStreaming" or value not in ("on", "off", "force"):
            return 400
        if value != "off" and self.state.get("stream_mode") != "rtmp":
            return 400
        self.set_state("streaming", value != "off")
        return 200

    def _cgi_srtexe(self, name: str, value: str) -> int:
        if name != "SrtStreaming" or value not in _ON_OFF_WORD:
            return 400
        if value == "on" and self.state.get("stream_mode") != "srt-caller":
            return 400
        self.set_state("streaming", value == "on")
        return 200

    def _cgi_rtmp(self, name: str, value: str) -> int:
        if name == "RtmpServerUrl1":
            if value and not value.lower().startswith(("rtmp://", "rtmps://")):
                return 400
            self.set_state("rtmp_url", unquote(value))
        elif name == "RtmpStreamKey1":
            self.set_state("rtmp_key", unquote(value))
        else:
            return 400
        return 200

    def _cgi_srt(self, name: str, value: str) -> int:
        if name != "SrtServerUrl1" or (value and not value.lower().startswith("srt://")):
            return 400
        self.set_state("srt_url", unquote(value))
        return 200

    def _cgi_main(self, name: str, value: str) -> int:
        if name == "System" and value in ("on", "reboot", "standby"):
            if value == "standby":
                self.set_state("power", False)
            elif value == "on":
                self.set_state("power", True)
        elif name == "FactoryDefault" and value in ("hard", "soft"):
            for key, initial in self.SIMULATOR_INFO["initial_state"].items():
                self.set_state(key, initial)
            self._presets.clear()
        else:
            return 400
        return 200

    def _save_preset(self, number: int, name: str = "") -> None:
        old = self._presets.get(number)
        label = name or (old[3] if old else "")
        self._presets[number] = (self._i("pan"), self._i("tilt"), self._i("zoom"), label)

    def _recall_preset(self, number: int) -> None:
        if number in self._presets:
            pan, tilt, zoom, _name = self._presets[number]
            self.set_state("pan", pan)
            self.set_state("tilt", tilt)
            self.set_state("zoom", zoom)
        if number <= 64:
            self.set_state("last_preset", number - 1)

    def _set_af(self, on: bool) -> None:
        self.set_state("af", on)
        self.set_state("af_status", "searching" if on else "idle")

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

        if payload_type == _CONTROL_CMD:
            if payload == b"\x01":
                self._last_seq = 0
                reply(_CONTROL_REPLY, b"\x01")
            return
        if sequence and sequence == self._last_seq:
            # A sequence number the camera has already seen.
            reply(_CONTROL_CMD, b"\x0f\x01")
            return
        self._last_seq = sequence
        # A packet ends at its first FF, whatever follows.
        end = payload.find(b"\xff")
        p = payload[: end + 1] if end >= 0 else payload
        if payload_type == 0x0110:
            answer = self._inquiry(p)
            reply(_REPLY, b"\x90\x50" + answer + b"\xff" if answer is not None else b"\x90\x60\x02\xff")
            return
        if payload_type == 0x0100:
            if self.state.get("osd") and not (
                p.startswith(b"\x81\x01\x04\x00") or p.startswith(b"\x81\x01\x06\x06")
                or p == b"\x81\x01\x7e\x01\x02\x00\x01\xff"
            ):
                reply(_REPLY, b"\x90\x61\x41\xff")
                return
            if self._command(p):
                reply(_REPLY, b"\x90\x41\xff")
                reply(_REPLY, b"\x90\x51\xff")
            else:
                reply(_REPLY, b"\x90\x60\x02\xff")

    def _inquiry(self, p: bytes) -> bytes | None:
        if len(p) < 4 or p[0] != 0x81 or p[1] != 0x09 or p[-1] != 0xFF:
            return None
        group = p[2:-1]
        s = self.state
        if group in _CODES:
            return bytes([self._i(_CODES[group][0])])
        if group in _LEVELS:
            return b"\x00\x00" + _to_nibbles(self._i(_LEVELS[group][0]), 2)
        simple: dict[bytes, Any] = {
            b"\x04\x00": lambda: b"\x02" if s.get("power") else b"\x03",
            b"\x7e\x04\x3a": lambda: b"\x01" if s.get("af") else b"\x00",
            b"\x06\x12": lambda: _to_nibbles(self._i("pan") & 0xFFFF)
            + _to_nibbles(self._i("tilt") & 0xFFFF),
            b"\x04\x47": lambda: _to_nibbles(self._i("zoom")),
            b"\x04\x48": lambda: _to_nibbles(self._i("focus")),
            b"\x04\x38": lambda: b"\x02" if s.get("focus_auto") else b"\x03",
            b"\x04\x3f": lambda: bytes([self._i("last_preset")]),
            b"\x7e\x01\x0a": lambda: b"\x02" if self._tally_on() else b"\x03",
            b"\x06\x06": lambda: b"\x02" if s.get("osd") else b"\x03",
            b"\x04\x37": lambda: bytes([self._i("defog"), self._i("defog_level")]),
            b"\x7e\x01\x03": lambda: bytes([self._i("hdmi_color")]),
            b"\x7e\x04\x33": lambda: bytes([self._i("video_format") >> 4, self._i("video_format") & 0x0F]),
        }
        answer = simple.get(group)
        return answer() if answer else None

    def _command(self, p: bytes) -> bool:
        if len(p) < 4 or p[0] != 0x81 or p[1] != 0x01 or p[-1] != 0xFF:
            return False
        body = p[2:-1]
        for group, (key, allowed) in _CODES.items():
            if len(body) == len(group) + 1 and body.startswith(group):
                code = body[-1]
                if code not in allowed:
                    return False
                if key == "zoom_mode" and code != 0x03 and self.model == "SRG-A12":
                    return False  # optical only on the A12
                self.set_state(key, code)
                return True
        for group, (key, low, high) in _LEVELS.items():
            if len(body) == len(group) + 4 and body.startswith(group + b"\x00\x00"):
                value = _nibbles(body[-2:])
                if not low <= value <= high or (key == "iris" and 0 < value < 5):
                    return False
                if key == "gain" and value > 0x0D and self._i("high_sens") != 2:
                    return False
                self.set_state(key, value)
                return True
        if len(body) == 3 and body[:2] in _STEPS and body[2] in (0x00, 0x02, 0x03):
            level, reset = _STEPS[body[:2]]
            key, low, high = _LEVELS[level]
            step = {0x00: None, 0x02: 1, 0x03: -1}[body[2]]
            self.set_state(key, reset if step is None else max(low, min(high, self._i(key) + step)))
            return True
        return self._other(body)

    def _other(self, body: bytes) -> bool:
        if body in (b"\x04\x00\x02", b"\x04\x00\x03"):
            self.set_state("power", body[-1] == 0x02)
            if body[-1] == 0x02 and 1 in self._presets:
                # "After the camera is turned on from standby mode, the camera
                # position moves to the position recorded on PRESET1."
                self._recall_preset(1)
            return True
        if body in (b"\x7e\x04\x3a\x01", b"\x7e\x04\x3a\x00"):
            self._set_af(body[-1] == 0x01)
            return True
        if body[:2] == b"\x06\x01" and len(body) == 6:
            return 1 <= body[2] <= 0x18 and 1 <= body[3] <= 0x17
        if body[:2] == b"\x06\x02" and len(body) == 12:
            pan = _nibbles(body[4:8])
            tilt = _nibbles(body[8:12])
            pan_s = pan - 0x10000 if pan & 0x8000 else pan
            tilt_s = tilt - 0x10000 if tilt & 0x8000 else tilt
            if not 1 <= body[2] <= 0x18 or not -0x2200 <= pan_s <= 0x2200:
                return False
            low, high = (-0x1200, 0x0400) if self._i("flip") == 2 else (-0x0400, 0x1200)
            if not low <= tilt_s <= high:
                return False
            self.set_state("pan", pan_s)
            self.set_state("tilt", tilt_s)
            return True
        if body in (b"\x06\x04", b"\x06\x05"):
            self.set_state("pan", 0)
            self.set_state("tilt", 0)
            return True
        if body[:2] == b"\x04\x07" and len(body) == 3:
            return body[2] in (0x00, 0x02, 0x03) or body[2] >> 4 in (2, 3)
        if body[:2] == b"\x04\x47" and len(body) == 6:
            position = _nibbles(body[2:])
            if position > self._zoom_max():
                return False
            self.set_state("zoom", position)
            return True
        if body[:2] == b"\x04\x08" and len(body) == 3:
            return body[2] in (0x00, 0x02, 0x03) or body[2] >> 4 in (2, 3)
        if body[:2] == b"\x04\x48" and len(body) == 6:
            position = _nibbles(body[2:])
            if not 0x1000 <= position <= 0xF000:
                return False
            self.set_state("focus", position)
            return True
        if body in (b"\x04\x38\x02", b"\x04\x38\x03"):
            self.set_state("focus_auto", body[-1] == 0x02)
            return True
        if body in (b"\x04\x18\x01", b"\x04\x18\x02"):
            if body[-1] == 0x02:
                self.set_state("focus", 0x1000)
            return True
        if body[:2] == b"\x04\x3f" and len(body) == 4 and body[2] in (0, 1, 2) and body[3] <= 0x3F:
            number = body[3] + 1
            if body[2] == 0x00:
                self._presets.pop(number, None)
            elif body[2] == 0x01:
                self._save_preset(number)
            else:
                self._recall_preset(number)
            return True
        if body[:2] == b"\x04\x37" and len(body) == 4 and body[2] in (2, 3) and body[3] <= 3:
            self.set_state("defog", body[2])
            self.set_state("defog_level", body[3])
            return True
        if body[:4] == b"\x7e\x01\x0a\x00" and len(body) == 5 and body[4] in (2, 3):
            on = body[4] == 0x02
            self.set_state("tally", on)
            self._tally_lit = time.monotonic() if on else 0.0
            return True
        if body[:4] == b"\x7e\x01\x0a\x01" and len(body) == 5 and body[4] in (0, 4, 5):
            self.set_state("tally_level", {0: "off", 4: "low", 5: "high"}[body[4]])
            return True
        if body[:4] == b"\x7e\x01\x03\x00" and len(body) == 5 and body[4] in (0, 1):
            self.set_state("hdmi_color", body[4])
            return True
        if body in (b"\x06\x06\x02", b"\x06\x06\x03", b"\x06\x06\x10"):
            self.set_state("osd", {0x02: True, 0x03: False, 0x10: not self.state.get("osd")}[body[-1]])
            return True
        if body in (b"\x7e\x01\x02\x00\x01", b"\x04\x10\x05"):
            return True
        return False
