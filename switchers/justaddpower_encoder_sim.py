"""
Just Add Power Encoder (justOS HTTP API) — Simulator.

Simulates one justOS encoder's justAPI as Just Add Power's OpenAPI document
(v1.1.2) describes it: ``GET`` of a details or settings URI under
``/cgi-bin/api`` answers ``{"data": <value>}``, a parent URI its children as
one object; ``POST`` of a command takes a JSON body and answers ``{"data":
"OK"}``, or ``{"error": [{"details", "requires", "uri"}]}`` with HTTP 400. A
value this model lacks answers ``null``; an unknown URI answers 404; a
decoder's command answers the document's "Invalid command for hardware"
error.

The ``model`` config value picks the hardware (default ``3G+AVP TX``). Stereo
volume is answered only by the models the document lists for it, and the DSP
mutes only by the 3G+AVP TX and 3G+WP4 TX; another model answers ``null`` and
refuses the command with the models that support it. ``channel_mode``
(``unified`` by default, or ``flexible``) follows KB 346: in Flexible Mode
``/command/channel`` is refused and ``/command/cli`` takes ``channel -v N``,
``channel -a N`` and ``channel -u N``.

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

Driver: justaddpower_encoder
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
VIDEO_WORDS = ("on", "off", "start", "stop")
DSP_MODELS = ("3G+AVP TX", "3G+WP4 TX")
RX_ONLY = ("/command/cec/tv", "/command/ppp", "/command/osd", "/command/edid")
HEX = re.compile(r"^[0-9A-Fa-f]{2}( +[0-9A-Fa-f]{2})*$")
EDID = (
    "00ffffffffffff0028308a710000000001190103807a44780a0dc9a05747982712484c2108008180a9c0714f"
    "b3000101010101010101023a801871382d40582c4500c2ad4200001e"
)


def _ok(data: Any = "OK"):
    return 200, {"data": data}


def _refuse(uri: str, details: str, requires: list[str] | None = None):
    return 400, {"error": [{"details": details, "requires": requires or [], "uri": uri}]}


class JustAddPowerEncoderSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "justaddpower_encoder",
        "name": "Just Add Power Encoder Simulator",
        "category": "switcher",
        "transport": "http",
        "default_port": 80,
        "initial_state": {
            "channel": 1, "video_channel": 1, "audio_channel": 1, "usb_channel": 1,
            "status": "Streaming Video", "firmware_version": "B3.2.0",
            "ip_address": "192.168.254.6", "mac_address": "c2:00:00:01:4c:dc", "link_speed": "1G",
            "resolution": "1920x1080p@60Hz", "hdcp": "1.x", "hdr": "passthru",
            "hdmi_audio": True, "hdmi_audio_format": "LPCM", "hdmi_audio_channels": "2",
            "hdmi_audio_sample_rate": "48KHz", "hdmi_audio_sample_size": "24bits",
            "stereo_delay": 0, "stereo_volume": 10,
            "dsp_hdmi": "on", "dsp_line": "on", "dsp_mic": "on", "max_bandwidth": "auto", "hdr_setting": "passthru",
            "image_pull": False,
            "device_name": "Encoder 1", "device_id": "TX1", "leds": "default", "serial_mode": "api",
            "serial_baud": 9600, "serial_databits": 8, "serial_parity": "none", "serial_stopbits": 1,
            # simulator bookkeeping
            "video_input": "on", "edid_choice": "hdmi", "last_cec": "", "last_serial": "", "last_cli": "",
            "last_switch": "", "dsp_restarts": 0,
        },
        "delays": {"command_response": 0.02},
        "controls": [
            {"type": "select", "key": "status", "label": "Status",
             "options": ["Streaming Video", "Waiting for Receiver", "No Video from Source", "Services Stopped"]},
            {"type": "select", "key": "resolution", "label": "Input resolution",
             "options": ["1920x1080p@60Hz", "1280x720p@60Hz", "3840x2160p@30Hz", ""]},
            {"type": "select", "key": "hdcp", "label": "HDCP", "options": ["1.x", "2.2"]},
            {"type": "toggle", "key": "hdmi_audio", "label": "HDMI audio present"},
            {"type": "select", "key": "serial_mode", "label": "Serial mode", "options": ["api", "passive", "direct"]},
            {"type": "indicator", "key": "channel", "label": "Channel"},
            {"type": "indicator", "key": "video_channel", "label": "Video channel (Flexible)"},
            {"type": "indicator", "key": "audio_channel", "label": "Audio channel (Flexible)"},
            {"type": "indicator", "key": "usb_channel", "label": "USB channel (Flexible)"},
            {"type": "indicator", "key": "video_input", "label": "HDMI input"},
            {"type": "indicator", "key": "edid_choice", "label": "EDID offered"},
            {"type": "indicator", "key": "dsp_hdmi", "label": "DSP HDMI"},
            {"type": "indicator", "key": "dsp_line", "label": "DSP line"},
            {"type": "indicator", "key": "dsp_mic", "label": "DSP mic"},
            {"type": "indicator", "key": "last_cli", "label": "Last CLI command"},
            {"type": "indicator", "key": "last_switch", "label": "Last JPSW / JADConfig switch"},
        ],
        "error_modes": {
            "communication_timeout": {"description": "Encoder stops answering", "behavior": "no_response"},
            "video_details_refused": {"description": "The encoder refuses the video details read (HTTP 500)", "behavior": "custom"},
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        cfg = self.config or {}
        self._model = str(cfg.get("model", "") or "3G+AVP TX")
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

    def _has_dsp(self) -> bool:
        return self._model in DSP_MODELS

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
                          "resolution": s["resolution"] or False, "scaler": None},
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
                "audio": {"dsp": ({"hdmi": True, "line": True, "mic": True} if self._has_dsp() else None)},
                "video": {
                    "stream": {"enabled": True, "maxbandwidth": s["max_bandwidth"]},
                    "hdr": "passthru" if s["hdr_setting"] == "passthru" else False,
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
        if uri in RX_ONLY or uri.startswith("/command/videowall/") or uri.startswith("/command/tiler/"):
            return _refuse(uri, f"Invalid command for hardware '{self._model}'", ["3G RX", "3G+4+ RX", "3G+AVP RX"])
        if uri == "/command/video":
            if value not in VIDEO_WORDS:
                return _refuse(uri, f"Invalid value {value!r}")
            if value == "stop":
                self.set_state("status", "Services Stopped")
            elif value == "start":
                self.set_state("status", "Streaming Video")
            else:
                self.set_state("video_input", value)
            return _ok()
        if uri == "/command/cec/bytes":
            if not isinstance(value, str) or not HEX.match(value.strip()):
                return _refuse(uri, "Invalid CEC bytes")
            self.set_state("last_cec", value.strip())
            return _ok()
        if uri.startswith("/command/audio/dsp"):
            if not self._has_dsp():
                return _refuse(uri, f"Invalid command for hardware '{self._model}'", list(DSP_MODELS))
            if uri == "/command/audio/dsp":
                if value != "reset":
                    return _refuse(uri, "Invalid value")
                self.set_state("dsp_restarts", int(self.state["dsp_restarts"]) + 1)
                return _ok()
            port = uri.rsplit("/", 1)[1]
            if port not in ("hdmi", "line", "mic") or value not in ("on", "mute"):
                return _refuse(uri, "Invalid value")
            self.set_state(f"dsp_{port}", value)
            return _ok()
        if uri == "/command/edid/default":
            if value not in ("hdmi", "multichannel"):
                return _refuse(uri, "Invalid value")
            self.set_state("edid_choice", value)
            return _ok()
        if uri == "/command/edid/custom":
            if not isinstance(value, str) or not re.fullmatch(r"[0-9A-Fa-f]+", value):
                return _refuse(uri, "Invalid EDID")
            self.set_state("edid_choice", "custom")
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
        if uri == "/settings/video/stream/maxbandwidth":
            if value not in ("10M", "50M", "100M", "150M", "200M", "auto"):
                return _refuse(uri, "Invalid bandwidth")
            self.set_state("max_bandwidth", value)
            return _ok(value)
        if uri == "/settings/video/hdr":
            if value not in ("passthru", False):
                return _refuse(uri, "HDR must be passthru or false")
            self.set_state("hdr_setting", "passthru" if value == "passthru" else "off")
            return _ok(value)
        found, _ = self._lookup(uri)
        return _refuse(uri, "This simulator does not write that setting") if found else (404, "Not Found")
