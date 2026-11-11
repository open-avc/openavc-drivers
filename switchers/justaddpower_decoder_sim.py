"""
Just Add Power Decoder (justOS HTTP API) — Simulator.

Simulates one justOS decoder's justAPI as Just Add Power's OpenAPI document
(v1.1.2) describes it: ``GET`` of a details or settings URI under
``/cgi-bin/api`` answers ``{"data": <value>}``, a parent URI its children as
one object; ``POST`` of a command takes a JSON body and answers ``{"data":
"OK"}``, or ``{"error": [{"details", "requires", "uri"}]}`` with HTTP 400. A
value this model lacks answers ``null``; an unknown URI answers 404.

The ``model`` config value picks the hardware (default ``3G+AVP RX``). Stereo
volume is answered only by the models the document lists for it; another model
answers ``null`` and refuses the command with the document's "Invalid command
for hardware" error. ``channel_mode`` (``unified`` by default, or
``flexible``) follows KB 346: in Flexible Mode ``/command/channel`` is refused
and ``/command/cli`` takes ``channel -v N``, ``channel -a N`` and ``channel -u
N``.

What the documents do not say and this simulator chooses: in Flexible Mode
``/details/channel`` answers the video channel; the refusal texts for Flexible
Mode and for RS-232 outside api serial mode; that ``/command/cli`` prints
nothing for a command that worked; the HTTP serial acknowledgement (``OK``), which is not endpoint feedback; and that a reboot answers 503
for ``reboot_downtime`` seconds (default 5).

Error modes: ``communication_timeout`` (answers nothing) and
``video_details_refused`` (``/details/video`` answers 500).

The HTTP simulator exposes serial commissioning settings only. It does not
open a second listener on TCP 6752 or emulate equipment connected by RS-232.
Direct bridge byte transport must be checked with a separate serial peer.

Driver: justaddpower_decoder
Transport: http
"""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import urlsplit

from openavc.simulator.http_simulator import HTTPSimulator

API = "/cgi-bin/api"
VOLUME_MODELS = ("3G+4+ TX", "3G+AVP RX", "3G+AVP TX", "3G+WP4 TX")
VIDEO_WORDS = ("on", "off", "start", "stop", "mute", "unmute", "pause", "play")
HEX = re.compile(r"^[0-9A-Fa-f]{2}( +[0-9A-Fa-f]{2})*$")
EDID = (
    "00ffffffffffff0028308a710000000001190103807a44780a0dc9a05747982712484c2108008180a9c0714f"
    "b3000101010101010101023a801871382d40582c4500c2ad4200001e"
)


def _ok(data: Any = "OK"):
    return 200, {"data": data}


def _refuse(uri: str, details: str, requires: list[str] | None = None):
    return 400, {"error": [{"details": details, "requires": requires or [], "uri": uri}]}


class JustAddPowerDecoderSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "justaddpower_decoder",
        "name": "Just Add Power Decoder Simulator",
        "category": "switcher",
        "transport": "http",
        "default_port": 80,
        "initial_state": {
            "channel": 1, "video_channel": 1, "audio_channel": 1, "usb_channel": 1,
            "status": "Decoding Video", "firmware_version": "B3.2.0",
            "ip_address": "192.168.254.130", "mac_address": "c2:00:00:01:4c:dd", "link_speed": "1G",
            "resolution": "1920x1080p@60Hz", "scaler": "1920x1080p60Hz", "hdcp": "1.x", "hdr": "passthru",
            "hdmi_audio": True, "hdmi_audio_format": "LPCM", "hdmi_audio_channels": "2",
            "hdmi_audio_sample_rate": "48KHz", "hdmi_audio_sample_size": "24bits",
            "stereo_delay": 0, "stereo_volume": 10,
            "wall_rows": 1, "wall_columns": 1, "wall_row": 1, "wall_column": 1,
            "wall_rotation": 0, "wall_mirror": False, "zoom_horizontal": 0, "zoom_vertical": 0,
            "bezel": "", "image_pull": False,
            "device_name": "Decoder 1", "device_id": "RX1", "leds": "default", "serial_mode": "api",
            "serial_baud": 9600, "serial_databits": 8, "serial_parity": "none", "serial_stopbits": 1,
            # simulator bookkeeping
            "video_output": "on", "tv_power": "on", "ppp_power": "on", "osd": "", "identify": "off",
            "usb": "disconnected", "last_cec": "", "last_serial": "", "last_cli": "", "last_switch": "",
        },
        "delays": {"command_response": 0.02},
        "controls": [
            {"type": "select", "key": "status", "label": "Status",
             "options": ["Decoding Video", "Searching for Transmitter", "Services Stopped", "Starting Services"]},
            {"type": "select", "key": "resolution", "label": "Output resolution",
             "options": ["1920x1080p@60Hz", "1280x720p@60Hz", "3840x2160p@30Hz"]},
            {"type": "select", "key": "hdcp", "label": "HDCP", "options": ["1.x", "2.2"]},
            {"type": "toggle", "key": "hdmi_audio", "label": "HDMI audio present"},
            {"type": "select", "key": "serial_mode", "label": "Serial mode", "options": ["api", "passive", "direct"]},
            {"type": "indicator", "key": "channel", "label": "Channel"},
            {"type": "indicator", "key": "video_channel", "label": "Video channel (Flexible)"},
            {"type": "indicator", "key": "audio_channel", "label": "Audio channel (Flexible)"},
            {"type": "indicator", "key": "usb_channel", "label": "USB channel (Flexible)"},
            {"type": "indicator", "key": "video_output", "label": "HDMI output"},
            {"type": "indicator", "key": "tv_power", "label": "TV (CEC)"},
            {"type": "indicator", "key": "ppp_power", "label": "Display (PlugPlayPresent)"},
            {"type": "indicator", "key": "osd", "label": "On-screen text"},
            {"type": "indicator", "key": "identify", "label": "Identify"},
            {"type": "indicator", "key": "usb", "label": "USB"},
            {"type": "indicator", "key": "last_cli", "label": "Last CLI command"},
            {"type": "indicator", "key": "last_switch", "label": "Last JPSW / JADConfig switch"},
        ],
        "error_modes": {
            "communication_timeout": {"description": "Decoder stops answering", "behavior": "no_response"},
            "video_details_refused": {"description": "The decoder refuses the video details read (HTTP 500)", "behavior": "custom"},
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        cfg = self.config or {}
        self._model = str(cfg.get("model", "") or "3G+AVP RX")
        self._flexible = str(cfg.get("channel_mode", "unified") or "unified").strip().lower() == "flexible"
        self._reboot_downtime = float(cfg.get("reboot_downtime", 5.0) or 0)
        self._started = time.monotonic()
        self._down_until = 0.0
        self.requests: list[str] = []

    # ── The API tree, built from state ──

    def _uptime(self) -> str:
        seconds = int(time.monotonic() - self._started)
        days, rest = divmod(seconds, 86400)
        hours, rest = divmod(rest, 3600)
        minutes, secs = divmod(rest, 60)
        return f"{days}d {hours:02d}h {minutes:02d}m {secs:02d}s"

    def _has_volume(self) -> bool:
        return self._model in VOLUME_MODELS

    def _tree(self) -> dict[str, Any]:
        s = self.state
        reported = s["video_channel"] if self._flexible else s["channel"]
        return {
            "details": {
                "audio": {
                    "hdmi": {
                        "channels": s["hdmi_audio_channels"], "format": s["hdmi_audio_format"],
                        "sampleFrequency": s["hdmi_audio_sample_rate"], "sampleSize": s["hdmi_audio_sample_size"],
                        "status": bool(s["hdmi_audio"]),
                    },
                    "stereo": {"delay": s["stereo_delay"]},
                },
                "channel": reported,
                "device": {
                    "firmware": {"date": "Tue, 13 Jan 2026 10:00:00 -0500", "update": False, "version": s["firmware_version"]},
                    "model": self._model,
                    "network": {
                        "ipaddress": s["ip_address"], "mac": s["mac_address"], "mtu": 8000,
                        "netmask": "255.255.255.0", "speed": s["link_speed"],
                    },
                    "status": s["status"],
                    "time": "Thu, 01 Jan 1970 00:40:30 +0000",
                    "uptime": self._uptime(),
                },
                "edid": EDID,
                "imagepull": bool(s["image_pull"]),
                "system": {"rx": [], "tx": []},
                "video": {"hdcp": s["hdcp"], "hdr": s["hdr"] if s["hdr"] != "off" else False,
                          "resolution": s["resolution"], "scaler": s["scaler"]},
                "videowall": {"layout": {
                    "columnPosition": s["wall_column"], "rowPosition": s["wall_row"],
                    "totalColumns": s["wall_columns"], "totalRows": s["wall_rows"],
                }},
            },
            "settings": {
                "device": {
                    "id": s["device_id"], "leds": False if s["leds"] == "off" else s["leds"], "name": s["device_name"],
                    "network": {"dns": "0.0.0.0", "gateway": "0.0.0.0", "ipaddress": s["ip_address"], "mode": "dhcp",
                                "multicast": f"239.92.{reported // 100:02d}.{reported % 100:02d}",
                                "netmask": "255.255.255.0", "timeserver": ""},
                    "powersave": {"enabled": False, "timeout": 0},
                },
                "serial": {"mode": s["serial_mode"], "speed": {"baud": s["serial_baud"], "databits": s["serial_databits"], "parity": s["serial_parity"], "stopbits": s["serial_stopbits"]}},
                "videowall": {
                    "layout": {"columnPosition": s["wall_column"], "rowPosition": s["wall_row"],
                               "totalColumns": s["wall_columns"], "totalRows": s["wall_rows"]},
                    "rotate": {"clockwise": s["wall_rotation"], "mirror": bool(s["wall_mirror"])},
                    "zoom": {"horizontal": s["zoom_horizontal"], "vertical": s["zoom_vertical"]},
                },
            },
        }

    def _lookup(self, uri: str):
        if uri == "/details/audio/stereo/volume":
            return True, (self.state["stereo_volume"] if self._has_volume() else None)
        node: Any = self._tree()
        for part in [p for p in uri.split("/") if p]:
            if not isinstance(node, dict) or part not in node:
                return False, None
            node = node[part]
        return True, node

    # ── Requests ──

    def handle_request(self, method: str, path: str, headers: dict[str, str], body: str):
        if time.monotonic() < self._down_until:
            return 503, "Service Unavailable"
        self.requests.append(f"{method} {path}" + (f" {body}" if body else ""))
        del self.requests[:-200]
        uri = urlsplit(path).path
        if not uri.startswith(API + "/"):
            return 404, "Not Found"
        uri = uri[len(API):]
        if method == "GET":
            if uri == "/details/video" and "video_details_refused" in self.active_errors:
                return 500, "Internal Server Error"
            found, value = self._lookup(uri)
            return _ok(value) if found else (404, "Not Found")
        if method != "POST":
            return 405, "Method Not Allowed"
        try:
            payload = json.loads(body) if body.strip() else None
        except ValueError:
            # A plain-text body is taken as it is, the way text/plain is accepted.
            payload = body.strip()
        if uri.startswith("/command/"):
            return self._command(uri, payload)
        if uri.startswith("/settings/"):
            return self._setting(uri, payload)
        return 404, "Not Found"

    def _command(self, uri: str, value: Any):
        if uri == "/command/channel":
            if self._flexible:
                return _refuse(uri, "Not supported in Flexible Mode")
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 9999:
                return _refuse(uri, "Channel must be an integer 0-9999")
            for key in ("channel", "video_channel", "audio_channel", "usb_channel"):
                self.set_state(key, value)
            return _ok()
        if uri == "/command/cli":
            return self._cli(str(value or ""))
        if uri == "/command/video":
            if value not in VIDEO_WORDS:
                return _refuse(uri, f"Invalid value {value!r}")
            if value == "stop":
                self.set_state("status", "Services Stopped")
            elif value == "start":
                self.set_state("status", "Decoding Video")
            self.set_state("video_output", {"play": "on", "unmute": "on", "pause": "frozen", "mute": "muted"}.get(value, value))
            return _ok()
        if uri == "/command/cec/tv":
            if value not in ("on", "off"):
                return _refuse(uri, f"Invalid value {value!r}")
            self.set_state("tv_power", value)
            return _ok()
        if uri == "/command/cec/bytes":
            if not isinstance(value, str) or not HEX.match(value.strip()):
                return _refuse(uri, "Invalid CEC bytes")
            self.set_state("last_cec", value.strip())
            return _ok()
        if uri == "/command/ppp":
            power = value.get("power") if isinstance(value, dict) else None
            if power not in ("on", "off"):
                return _refuse(uri, "Invalid power value")
            self.set_state("ppp_power", power)
            return _ok()
        if uri == "/command/osd":
            if not isinstance(value, dict):
                return _refuse(uri, "Invalid OSD body")
            if "identify" in value:
                self.set_state("identify", "on" if value["identify"] == "on" else "off")
            else:
                self.set_state("osd", str(value.get("string", "")))
            return _ok()
        if uri == "/command/videowall/layout":
            if not (isinstance(value, list) and len(value) == 4 and all(isinstance(v, int) and v >= 1 for v in value)):
                return _refuse(uri, "Layout must be [totalRows, totalColumns, rowPosition, columnPosition]")
            rows, columns, row, column = value
            if row > rows or column > columns:
                return _refuse(uri, "Position outside the wall")
            self.set_state("wall_rows", rows)
            self.set_state("wall_columns", columns)
            self.set_state("wall_row", row)
            self.set_state("wall_column", column)
            return _ok()
        if uri == "/command/videowall/rotate/clockwise":
            if value not in (0, 90, 180, 270) or isinstance(value, bool):
                return _refuse(uri, "Rotation must be 0, 90, 180 or 270")
            self.set_state("wall_rotation", value)
            return _ok()
        if uri == "/command/videowall/bezel":
            keys = ("ow", "oh", "vw", "vh")
            if not (isinstance(value, dict) and all(isinstance(value.get(k), int) and 1 <= value[k] <= 65535 for k in keys)):
                return _refuse(uri, "Bezel needs ow, oh, vw and vh, 1-65535")
            self.set_state("bezel", ",".join(str(value[k]) for k in keys))
            return _ok()
        if uri in ("/command/videowall/zoom/horizontal", "/command/videowall/zoom/vertical"):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                return _refuse(uri, "Zoom must be 0 or more")
            self.set_state("zoom_" + uri.rsplit("/", 1)[1], value)
            return _ok()
        if uri == "/command/audio/stereo/volume":
            if not self._has_volume():
                return _refuse(uri, f"Invalid command for hardware '{self._model}'", list(VOLUME_MODELS))
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 11:
                return _refuse(uri, "Volume must be 0-11")
            self.set_state("stereo_volume", value)
            return _ok()
        if uri == "/command/audio/stereo/delay":
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 175:
                return _refuse(uri, "Delay must be 0-175")
            self.set_state("stereo_delay", value)
            return _ok()
        if uri == "/command/edid":
            return _ok() if value == "capture" else _refuse(uri, "Invalid value")
        if uri in ("/command/serial/ascii", "/command/serial/hex"):
            if self.state["serial_mode"] != "api":
                return _refuse(uri, "Serial mode is not api")
            if uri.endswith("hex") and (not isinstance(value, str) or not HEX.match(value.strip())):
                return _refuse(uri, "Invalid hex bytes")
            self.set_state("last_serial", str(value))
            return _ok()  # KB 473: API mode does not return endpoint feedback.
        if uri == "/command/flux":
            return _ok("completeir,1:1")
        if uri == "/command/imagepull":
            if not isinstance(value, dict):
                return _refuse(uri, "Invalid image pull body")
            self.set_state("image_pull", True)
            return _ok()
        if uri == "/command/switch":
            if not (isinstance(value, dict) and isinstance(value.get("rx"), list) and isinstance(value.get("tx"), int)):
                return _refuse(uri, "Switch needs rx (a list) and tx")
            self.set_state("last_switch", f"JADConfig rx {value['rx']} tx {value['tx']}")
            return _ok()
        if uri == "/command/device":
            if value == "reboot":
                self._down_until = time.monotonic() + self._reboot_downtime
                self._started = self._down_until
                return _ok()
            return _ok() if value == "save" else _refuse(uri, "Invalid value")
        return 404, "Not Found"

    def _cli(self, line: str):
        self.set_state("last_cli", line)
        match = re.fullmatch(r"channel -([vau]) (\d{1,4})", line.strip())
        if match:
            if not self._flexible:
                return _ok("Unified Mode: use channel N")
            key = {"v": "video_channel", "a": "audio_channel", "u": "usb_channel"}[match.group(1)]
            self.set_state(key, int(match.group(2)))
            return _ok("")
        if line.strip() == "usb connect":
            self.set_state("usb", "connected")
            return _ok("")
        if line.strip() == "usb stop":
            self.set_state("usb", "disconnected")
            return _ok("")
        jpsw = re.fullmatch(r"jpsw -r (\S+) -t (\d+)", line.strip())
        if jpsw:
            self.set_state("last_switch", f"JPSW decoders {jpsw.group(1)} encoder {jpsw.group(2)}")
            return _ok("")
        return _ok(f"sh: {line.split(' ')[0] if line else ''}: not found")

    def _setting(self, uri: str, value: Any):
        if uri == "/settings/device/name":
            if not isinstance(value, str):
                return _refuse(uri, "Name must be a string")
            self.set_state("device_name", value)
            return _ok(value)
        if uri == "/settings/device/leds":
            if value not in (False, "video", "default"):
                return _refuse(uri, "LEDs must be false, video or default")
            self.set_state("leds", "off" if value is False else value)
            return _ok(value)
        found, _ = self._lookup(uri)
        return _refuse(uri, "This simulator does not write that setting") if found else (404, "Not Found")
