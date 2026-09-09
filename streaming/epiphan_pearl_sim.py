"""
Epiphan Pearl — Simulator

Simulates the Pearl device REST API v2.0 as the driver uses it: firmware and
identity, system status, the connectivity report and speed test, channels
with their publishers (status, settings, add, rename, delete, start and
stop), layouts, bookmarks, recorders (status, start and stop, the archive),
inputs with per-type settings blocks, the HDMI output source, storages and
their transfer status, automatic file upload, the one-touch control,
configuration presets, CMS events with the alias identifiers and the ad-hoc
session, reboot and shutdown.

By default it is a Pearl Mini: two channels (HDMI-A, HDMI-B) each with a
built-in recorder, a multitrack recorder, three streams on channel 1 (RTMP,
SRT listener, NDI) and one on channel 2 (RTSP announce), HDMI, SDI, XLR/TRS,
RCA, USB, RTSP, SRT, NDI and web-graphics inputs, one HDMI output, internal
and external storage. ``model: "nano"`` shrinks it to the Pearl Nano's one
channel, one recorder and one layout.

Start and stop are asynchronous as the API says: a publisher or recorder
answers ``starting`` for a moment after the command and settles to
``started`` (an SRT listener to ``listening``) on the next read.

The HDMI input answers its settings on ``hdmi-a`` and refuses them (405) on
``hdmi-b``, because the REST API document does both: its GET 405 examples
list "Pearl 2/Mini/Nexus HDMI" as unsupported while its PUT examples include
a "Pearl Mini HDMI" settings body. Hardware settles which; until then the
driver is exercised both ways.

Authentication is off by default (``require_auth``), which is what the
connect-lifecycle smoke and a first look in the Simulator UI need. With it
on, every request must carry HTTP Basic with the configured ``password``
(default ``secret``); the username is not checked, as the API document does
not say which accounts may use it.

Driver: epiphan_pearl
Transport: http
"""

from __future__ import annotations

import base64
import copy
import json
import re
import time
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from openavc.simulator.http_simulator import HTTPSimulator

API_PREFIX = "/api/v2.0"

# How long a start takes to settle from ``starting`` to ``started``.
SETTLE_S = 0.3

SECTIONS = ["system", "network", "sources", "edid", "channels", "afu", "cms",
            "avstudio", "frontscreen", "displays"]


def _ok(result: Any = None) -> tuple[int, str]:
    body: dict[str, Any] = {"status": "ok"}
    if result is not None:
        body["result"] = result
    return 200, json.dumps(body)


def _created(result: Any) -> tuple[int, str]:
    return 201, json.dumps({"status": "ok", "result": result})


def _error(http: int, status: str, message: str) -> tuple[int, str]:
    return http, json.dumps({"status": status, "message": message})


def _not_found(message: str) -> tuple[int, str]:
    return _error(404, "notfound", message)


def _deep_merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


class EpiphanPearlSimulator(HTTPSimulator):
    """A Pearl Mini (or Nano) over the REST API v2.0."""

    SIMULATOR_INFO = {
        "driver_id": "epiphan_pearl",
        "name": "Epiphan Pearl Simulator",
        "category": "streaming",
        "transport": "http",
        "default_port": 80,
        "initial_state": {
            "product_name": "Pearl Mini",
            "firmware_version": "4.24.1",
            "recorders_started": 0,
            "publishers_started": 0,
            "cpu_load": 25,
            "cpu_temp": 57,
            "external_storage": True,
            "single_touch_pressed": False,
            "event_ongoing": False,
            "afu_state": "idle",
            "rebooted": False,
            "shutdown": False,
        },
        "controls": [
            {"type": "slider", "key": "cpu_load", "min": 0, "max": 100, "label": "CPU Load (%)"},
            {"type": "slider", "key": "cpu_temp", "min": 20, "max": 90, "label": "CPU Temperature (C)"},
            {"type": "toggle", "key": "external_storage", "label": "External Drive Present"},
            {"type": "toggle", "key": "single_touch_pressed", "label": "One-Touch Pressed"},
            {"type": "toggle", "key": "event_ongoing", "label": "CMS Event Running"},
            {"type": "select", "key": "afu_state", "label": "File Upload State",
             "options": ["idle", "paused", "uploading", "error", "disabled"]},
        ],
        "error_modes": {
            "publisher_error": {
                "description": "The RTMP stream on channel 1 reports an error",
                "behavior": "publisher_error",
            },
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        cfg = self.config
        self._require_auth = bool(cfg.get("require_auth", False))
        self._password = str(cfg.get("password", "secret"))
        self._nano = str(cfg.get("model", "mini")).lower() == "nano"
        # The API documents a 409 when "the maximum number of allowed network
        # inputs is reached" without saying what the maximum is.
        self._max_network_inputs = int(cfg.get("max_network_inputs", 10))
        self._started = time.time()
        self.calls: list[str] = []
        self._build_model()

    # ── Model ──

    def _build_model(self) -> None:
        now = time.time()
        if self._nano:
            self.set_state("product_name", "Pearl Nano")
            self._firmware = {"version": "4.24.1", "revision": "250107_1e4514f",
                              "product_id": 55, "product_name": "Pearl Nano"}
        else:
            self._firmware = {"version": "4.24.1", "revision": "250107_1e4514f",
                              "product_id": 44, "product_name": "Pearl Mini"}
        self._ident = {"name": "", "location": "Room 101", "description": "Lecture capture"}
        self._rebooted = False

        def publisher(ptype: str, name: str, block: dict[str, Any], enabled: bool = True,
                      single_touch: bool = True) -> dict[str, Any]:
            return {
                "name": name,
                "settings": {"type": ptype, ptype.replace("-", "_"): block,
                             "common": {"enabled": enabled, "single_touch": single_touch}},
                "started": False, "state": "stopped", "since": 0.0, "pending": 0.0,
                "reconnections": 0,
            }

        self._channels: dict[str, dict[str, Any]] = {
            "1": {
                "name": "HDMI-A",
                "layouts": {"1": "Default", "2": "Picture in Picture"},
                "active_layout": "1",
                "encoders": [
                    {"id": "0", "type": "video", "name": "H.264", "resolution": "1920x1080",
                     "framerate": 30, "bitrate": 3000},
                    {"id": "1", "type": "audio", "name": "AAC", "channels": 2, "bitrate": 128},
                ],
                "publishers": {
                    "0": publisher("rtmp", "Stream 1", {
                        "disable_audio": False, "url": "rtmp://192.168.86.51/live",
                        "stream": "lecture-key", "username": "", "password": ""}),
                    "1": publisher("srt", "Stream 2", {
                        "port": 1029, "disable_audio": False, "mode": "listener",
                        "latency": 125, "bw_recovery_overhead": 25,
                        "encryption": {"keylength": 128, "passphrase": "AnyPassphrase"}}),
                    "2": publisher("ndi", "Stream 3", {
                        "disable_audio": False, "ndi_name": "Pearl HDMI-A", "ndi_group": ""},
                        enabled=False, single_touch=False),
                },
            },
        }
        if not self._nano:
            self._channels["2"] = {
                "name": "HDMI-B",
                "layouts": {"1": "Default"},
                "active_layout": "1",
                "encoders": [
                    {"id": "0", "type": "video", "name": "H.264", "resolution": "1280x720",
                     "framerate": 30, "bitrate": 2000},
                    {"id": "1", "type": "audio", "name": "AAC", "channels": 2, "bitrate": 128},
                ],
                "publishers": {
                    "0": publisher("rtsp", "Stream 1", {
                        "disable_audio": False, "url": "rtsp://192.168.86.51/announce",
                        "transport": "udp", "username": "", "password": ""}),
                },
            }
        self._next_publisher: dict[str, int] = {cid: len(ch["publishers"]) for cid, ch in self._channels.items()}

        def recorder(name: str, multisource: bool = False) -> dict[str, Any]:
            return {"name": name, "multisource": multisource, "state": "stopped",
                    "since": 0.0, "pending": 0.0, "files": []}

        self._recorders: dict[str, dict[str, Any]] = {"1": recorder("HDMI-A")}
        self._recorders["1"]["files"] = [
            {"id": "VGA.1733871268.HDMI-A.mp4", "name": "HDMI-A_Dec10_17-54-28", "extension": "mp4",
             "recording": False, "downloaded": False, "created": "2024-12-10T17:54:28-0500",
             "duration": 309, "size": 120487978, "recorder": "1", "uploading": False, "event_id": ""},
            {"id": "VGA.1733956350.HDMI-A.mp4", "name": "HDMI-A_Dec11_17-32-30", "extension": "mp4",
             "recording": False, "downloaded": False, "created": "2024-12-11T17:32:30-0500",
             "duration": 1040, "size": 407160404, "recorder": "1", "uploading": False, "event_id": ""},
        ]
        if not self._nano:
            self._recorders["2"] = recorder("HDMI-B")
            self._recorders["3"] = recorder("Multitrack", multisource=True)
        self._file_seq = 0

        def video_common(timeout: int = 5) -> dict[str, Any]:
            return {"nosignal": {"image": "", "timeout": timeout},
                    "force_full_color_range": True, "hwaccel_decoding": True}

        # Inputs: (name, real name, audio, video, type, settings or None for
        # "Input settings are not supported").
        self._inputs: dict[str, dict[str, Any]] = {}
        if self._nano:
            self._inputs["hdmi"] = {"name": "HDMI", "real": "HDMI", "audio": True, "video": True,
                                    "type": "embedded", "settings": None}
            self._inputs["sdi"] = {"name": "SDI", "real": "SDI", "audio": True, "video": True,
                                   "type": "embedded", "settings": {
                                       "video": {"nosignal": {"image": "", "timeout": 5}},
                                       "sdi": {"scaling": "", "audio": {"mute": False, "delay": 0}}}}
            self._inputs["analog"] = {"name": "Analog Audio", "real": "XLR/RCA", "audio": True, "video": False,
                                      "type": "embedded", "settings": {
                                          "audio": {"delay": 0},
                                          "local_audio": {"gain": 0, "mute": False, "input_type": "XLR+RCA",
                                                          "stereo_pair": True,
                                                          "channels": {"channelA": {"gain": 0, "mute": False},
                                                                       "channelB": {"gain": 0, "mute": False}}}}}
        else:
            self._inputs["hdmi-a"] = {"name": "HDMI-A", "real": "HDMI-A", "audio": True, "video": True,
                                      "type": "embedded", "settings": {
                                          "video": {"nosignal": {"image": "", "timeout": 5}},
                                          "hdmi": {"deinterlacing": False, "audio": {"mute": False, "delay": 0}}}}
            self._inputs["hdmi-b"] = {"name": "HDMI-B", "real": "HDMI-B", "audio": True, "video": True,
                                      "type": "embedded", "settings": None}
            self._inputs["sdi"] = {"name": "SDI", "real": "SDI", "audio": True, "video": True,
                                   "type": "embedded", "settings": {
                                       "video": {"nosignal": {"image": "", "timeout": 5}},
                                       "sdi": {"scaling": "", "audio": {"mute": False, "delay": 0}}}}
            self._inputs["analog-a"] = {"name": "XLR/TRS", "real": "XLR/TRS", "audio": True, "video": False,
                                        "type": "embedded", "settings": {
                                            "audio": {"delay": 0},
                                            "local_audio": {"gain": 27, "mute": False, "phantom_power": False,
                                                            "stereo_pair": True,
                                                            "channels": {"channelA": {"gain": 27, "mute": False},
                                                                         "channelB": {"gain": 27, "mute": False}}}}}
            self._inputs["analog-b"] = {"name": "RCA/3.5mm", "real": "RCA/3.5mm", "audio": True, "video": False,
                                        "type": "embedded", "settings": {
                                            "audio": {"delay": 0},
                                            "local_audio": {"gain": 6, "mute": False, "input_type": "RCA+3.5mm"}}}
            self._inputs["USBA"] = {"name": "USB-A", "real": "USB-A", "audio": True, "video": True,
                                    "type": "usb", "settings": None}
            self._inputs["RTSP1"] = {"name": "RTSP 1", "real": "RTSP 1", "audio": True, "video": True,
                                     "type": "rtsp", "settings": {
                                         "audio": {"delay": 0}, "video": video_common(),
                                         "rtsp": {"url": "rtsp://10.10.10.10:8554/rtspstream", "username": "",
                                                  "password": "", "transport": "udp"}}}
            self._inputs["SRT1"] = {"name": "SRT 1", "real": "SRT 1", "audio": True, "video": True,
                                    "type": "srt", "settings": {
                                        "audio": {"delay": 0}, "video": video_common(),
                                        "srt": {"mode": "listener", "latency": 80,
                                                "encryption": {"passphrase": "111111111111111", "keylength": 128},
                                                "port": 1024}}}
            self._inputs["NDI1"] = {"name": "NDI 1", "real": "NDI 1", "audio": True, "video": True,
                                    "type": "ndi", "settings": None}
            self._inputs["WEBG1"] = {"name": "Web Graphics 1", "real": "Web Graphics 1", "audio": False,
                                     "video": True, "type": "web-graphics", "settings": None}
        self._next_input: dict[str, int] = {"rtsp": 2, "srt": 2, "ndi": 2, "web-graphics": 2}

        self._outputs: dict[str, dict[str, Any]] = {"D1": {"name": "HDMI", "source": "1"}}
        self._storages: dict[str, dict[str, Any]] = {
            "main": {"state": "ready", "total": 15809413120, "free": 11821019136, "transfer": None},
            "external": {"state": "ready", "total": 64023257088, "free": 40000000000,
                         "transfer": {"state": "completed",
                                      "session": {"started": "2025-01-10T18:03:04-05:00",
                                                  "completed": "2025-01-10T18:03:07-05:00",
                                                  "total": {"count": 3, "size": 20113170},
                                                  "processed": {"count": 3, "size": 20113170}},
                                      "current_time": "2025-01-10T18:03:13-05:00"}},
            "maintenance": {"state": "nodev", "transfer": None},
        }
        self._afu: dict[str, dict[str, Any]] = {"0": {"protocol": "webdav"}}
        self._single_touch: dict[str, dict[str, Any]] = {"0": {"pressed": False}}
        self._presets: list[dict[str, Any]] = [
            {"name": "Default", "description": "Default profile", "sections": ["all"], "readonly": True},
            {"name": "Lecture", "description": "", "sections": list(SECTIONS), "readonly": False},
            {"name": "Meeting", "description": "", "sections": list(SECTIONS), "readonly": False},
        ]
        self._applied_preset = ""
        self._events: dict[str, dict[str, Any]] = {
            "782ec0f4bbcc48e2a42a44eea5e69dc5": {
                "id": "782ec0f4bbcc48e2a42a44eea5e69dc5", "status": "scheduled",
                "title": "Physics 101", "start": int(now) + 3600, "finish": int(now) + 7200,
                "recorders": [{"id": "1"}], "streams": [], "tags": "",
            },
            "0c4d1f5c7e5b4bd2a9e9d1f2c3b4a596": {
                "id": "0c4d1f5c7e5b4bd2a9e9d1f2c3b4a596", "status": "finished",
                "title": "Chemistry 201", "start": int(now) - 7200, "finish": int(now) - 3600,
                "recorders": [{"id": "1"}], "streams": [], "tags": "",
            },
        }
        self._adhoc_session: dict[str, Any] | None = None
        self._speedtests = 0
        self._sync_state()

    # ── Simulator UI bridge ──

    def set_state(self, key: str, value: Any) -> None:
        super().set_state(key, value)
        if not hasattr(self, "_channels"):
            return
        if key == "event_ongoing":
            running = [e for e in self._events.values() if e["status"] in ("running", "paused")]
            if value and not running:
                now = int(time.time())
                self._events["adhoc-ui"] = {
                    "id": "adhoc-ui", "status": "running", "title": "Ad-hoc from the simulator",
                    "start": now - 60, "finish": now + 3540, "recorders": [{"id": "1"}],
                    "streams": [], "tags": "",
                }
            elif not value and running:
                for event in running:
                    event["status"] = "finished"
        elif key == "external_storage":
            self._storages["external"]["state"] = "ready" if value else "nodev"
        elif key == "single_touch_pressed":
            stc = self._single_touch["0"]
            if bool(value) != stc["pressed"]:
                self._toggle_single_touch("0")

    def _sync_state(self) -> None:
        self._settle()
        super().set_state("recorders_started", sum(1 for r in self._recorders.values() if r["state"] == "started"))
        super().set_state("publishers_started", sum(
            1 for ch in self._channels.values() for p in ch["publishers"].values() if p["started"]))
        super().set_state("single_touch_pressed", self._single_touch["0"]["pressed"])
        super().set_state("event_ongoing", any(
            e["status"] in ("running", "paused") for e in self._events.values()))
        super().set_state("external_storage", self._storages["external"]["state"] == "ready")

    # ── Time and transitions ──

    def _settle(self) -> None:
        now = time.time()
        for ch in self._channels.values():
            for pub in ch["publishers"].values():
                if pub["state"] == "starting" and now >= pub["pending"]:
                    mode = pub["settings"].get("srt", {}).get("mode") if pub["settings"]["type"] == "srt" else ""
                    pub["state"] = "listening" if mode == "listener" else "started"
        for rec in self._recorders.values():
            if rec["state"] == "starting" and now >= rec["pending"]:
                rec["state"] = "started"
        for event in self._events.values():
            if event["status"] == "running" and now >= event["finish"]:
                event["status"] = "finished"

    # ── Authentication ──

    def _authorized(self, headers: dict[str, str]) -> bool:
        if not self._require_auth:
            return True
        auth = ""
        for key, value in headers.items():
            if key.lower() == "authorization":
                auth = value
        if not auth.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        return decoded.split(":", 1)[-1] == self._password

    # ── Dispatch ──

    def handle_request(self, method: str, path: str, headers: dict[str, str], body: str):
        parts = urlsplit(path)
        route = unquote(parts.path)
        query = {k: v for k, v in parse_qsl(parts.query, keep_blank_values=True)}
        if not route.startswith(API_PREFIX):
            return 404, "Not Found"
        route = route[len(API_PREFIX):]
        if not self._authorized(headers):
            return 401, "Unauthorized", {"WWW-Authenticate": 'Basic realm="Pearl"'}
        self.calls.append(f"{method} {route}")
        try:
            data = json.loads(body) if body else None
        except ValueError:
            data = None
        self._settle()
        result = self._route(method, route, query, data)
        self._sync_state()
        return result

    def _route(self, method: str, route: str, query: dict[str, str], data: Any):
        m = re.fullmatch
        # System
        if route == "/system/firmware" and method == "GET":
            return _ok(dict(self._firmware))
        if route == "/system/firmware/version" and method == "GET":
            return _ok(self._firmware["version"])
        if route == "/system/firmware/revision" and method == "GET":
            return _ok(self._firmware["revision"])
        if route == "/system/firmware/product_id" and method == "GET":
            return _ok(self._firmware["product_id"])
        if route == "/system/firmware/product_name" and method == "GET":
            return _ok(self._firmware["product_name"])
        if route == "/system/ident" and method == "GET":
            return _ok(dict(self._ident))
        if route == "/system/status" and method == "GET":
            temp = float(self.get_state("cpu_temp", 57))
            return _ok({
                "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "uptime": int(time.time() - self._started) + 5490,
                "cpuload": int(self.get_state("cpu_load", 25)),
                "cpuload_high": int(self.get_state("cpu_load", 25)) >= 90,
                "cputemp": temp,
                "cputemp_threshold": 70,
            })
        if route == "/system/connectivity/details" and method == "GET":
            return _ok({
                "external_ip": "174.115.41.91", "mdns": "GSAA495529", "dns": "ok", "http": "ok",
                "https": "ok", "captive_portal": "ok", "icmp": "error", "epiphan_edge": "disabled",
                "vtun": "disabled",
            })
        if route == "/system/connectivity/tools/speedtest" and method == "GET":
            mode = query.get("mode", "uplink")
            protocol = query.get("protocol", "tcp")
            if mode not in ("uplink", "downlink"):
                return _error(400, "badrequest", "Invalid mode parameter")
            if protocol not in ("tcp", "udp"):
                return _error(400, "badrequest", "Invalid protocol parameter")
            self._speedtests += 1
            result: dict[str, Any] = {
                "protocol": protocol, "mode": mode, "bandwidth": 91318568,
                "bitrate_limit": 1000000000, "duration": min(int(query.get("timeout", "30") or 30), 10),
            }
            if protocol == "udp":
                result["udp"] = {"loss": 2}
            return _ok(result)
        if route == "/system/control/reboot" and method == "POST":
            self._rebooted = True
            super().set_state("rebooted", True)
            return _ok()
        if route == "/system/control/shutdown" and method == "POST":
            super().set_state("shutdown", True)
            return _ok()
        if route == "/system/control/factoryreset" and method == "POST":
            return _ok()
        # Presets
        if route == "/system/presets" and method == "GET":
            out = []
            for preset in self._presets:
                entry: dict[str, Any] = {"name": preset["name"]}
                if query.get("description") == "true" or query.get("details") == "true":
                    entry["description"] = preset["description"]
                if query.get("sections") == "true" or query.get("details") == "true":
                    entry["sections"] = preset["sections"]
                if query.get("readonly") == "true" or query.get("details") == "true":
                    entry["readonly"] = preset["readonly"]
                out.append(entry)
            return _ok(out)
        mt = m(r"/system/presets/([^/]+)/control/apply", route)
        if mt and method == "POST":
            name = mt.group(1)
            if not name:
                return _error(400, "badrequest", "Missing preset name")
            if not any(p["name"] == name for p in self._presets):
                return _not_found(f"Configuration preset {name} is not found")
            sections = data.get("sections") if isinstance(data, dict) else None
            self._applied_preset = name
            reboot = not sections or "system" in sections or "network" in sections or "all" in sections
            return _ok({"reboot": reboot})
        # Storages
        if route == "/system/storages" and method == "GET":
            return _ok([{"id": sid} for sid in self._storages])
        mt = m(r"/system/storages/([^/]+)/status", route)
        if mt and method == "GET":
            storage = self._storages.get(mt.group(1))
            if storage is None:
                return _not_found(f"Storage '{mt.group(1)}' not found")
            result = {"state": storage["state"]}
            if storage["state"] == "ready":
                result["total"] = storage["total"]
                result["free"] = storage["free"]
            return _ok(result)
        mt = m(r"/system/storages/([^/]+)/control/eject", route)
        if mt and method == "POST":
            storage = self._storages.get(mt.group(1))
            if storage is None:
                return _not_found(f"Storage '{mt.group(1)}' not found")
            if mt.group(1) != "external":
                return _error(405, "notallowed", f"Storage '{mt.group(1)}' does not support eject")
            storage["state"] = "nodev"
            return _ok()
        mt = m(r"/system/storages/([^/]+)/transfer/status", route)
        if mt and method == "GET":
            storage = self._storages.get(mt.group(1))
            if storage is None:
                return _not_found(f"Storage '{mt.group(1)}' not found")
            if storage["transfer"] is None:
                return _error(405, "notallowed", f"Storage '{mt.group(1)}' does not support transfer")
            if storage["state"] != "ready":
                return _ok({"state": "nomedia"})
            return _ok(copy.deepcopy(storage["transfer"]))
        # One-touch
        if route == "/system/singletouchcontrol" and method == "GET":
            return _ok([{"id": stcid} for stcid in self._single_touch])
        mt = m(r"/system/singletouchcontrol/([^/]+)/state", route)
        if mt and method == "GET":
            if mt.group(1) not in self._single_touch:
                return _not_found(f"single touch control object '{mt.group(1)}' not found")
            return _ok(self._single_touch_state(mt.group(1)))
        mt = m(r"/system/singletouchcontrol/([^/]+)/control/toggle", route)
        if mt and method == "POST":
            if mt.group(1) not in self._single_touch:
                return _not_found(f"single touch control object '{mt.group(1)}' not found")
            self._toggle_single_touch(mt.group(1))
            return _ok()
        # AFU
        if route == "/afu" and method == "GET":
            return _ok([{"id": aid} for aid in self._afu])
        if route == "/afu/status" and method == "GET":
            return _ok([{"id": aid, "status": self._afu_status(aid)} for aid in self._afu])
        mt = m(r"/afu/([^/]+)/status", route)
        if mt and method == "GET":
            if mt.group(1) not in self._afu:
                return _not_found(f"AFU destination '{mt.group(1)}' not found")
            return _ok(self._afu_status(mt.group(1)))
        # Channels
        if route == "/channels" and method == "GET":
            return _ok(self._channel_list(query))
        mt = m(r"/channels/([^/]+)/name", route)
        if mt:
            ch = self._channels.get(mt.group(1))
            if ch is None:
                return _not_found("Channel not found")
            if method == "GET":
                return _ok(ch["name"])
            if method == "PUT":
                name = query.get("name", "")
                if not 1 <= len(name) <= 128:
                    return _error(400, "badrequest", "Invalid name")
                ch["name"] = name
                self._recorders.get(mt.group(1), {})["name"] = name
                return _ok(name)
        mt = m(r"/channels/([^/]+)/preview", route)
        if mt and method == "GET":
            if mt.group(1) not in self._channels:
                return _not_found("Channel not found")
            return 200, "JPEG"
        mt = m(r"/channels/([^/]+)/bookmarks", route)
        if mt and method == "POST":
            if mt.group(1) not in self._channels:
                return _not_found("Channel not found")
            if not query.get("text"):
                return _error(400, "badrequest", "Missing bookmark text")
            rec = self._recorders.get(mt.group(1))
            if rec is None or rec["state"] != "started":
                return _error(409, "conflict", "The channel is not being recorded.")
            rec.setdefault("bookmarks", []).append(query["text"])
            return _ok()
        mt = m(r"/channels/([^/]+)/layouts/active", route)
        if mt and method == "PUT":
            ch = self._channels.get(mt.group(1))
            if ch is None:
                return _not_found("Channel not found")
            layout = query.get("id", "")
            if layout not in ch["layouts"]:
                return _not_found("Layout not found")
            ch["active_layout"] = layout
            return _ok()
        mt = m(r"/channels/([^/]+)/publishers/control/(start|stop)", route)
        if mt and method == "POST":
            ch = self._channels.get(mt.group(1))
            if ch is None:
                return _not_found("Channel not found")
            for pub in ch["publishers"].values():
                self._publisher_control(pub, mt.group(2))
            return _ok()
        mt = m(r"/channels/([^/]+)/publishers/type", route)
        if mt and method == "GET":
            ch = self._channels.get(mt.group(1))
            if ch is None:
                return _not_found("Channel not found")
            return _ok([{"id": pid, "type": p["settings"]["type"], "name": p["name"]}
                        for pid, p in ch["publishers"].items()])
        mt = m(r"/channels/([^/]+)/publishers/status", route)
        if mt and method == "GET":
            ch = self._channels.get(mt.group(1))
            if ch is None:
                return _not_found("Channel not found")
            return _ok([{"id": pid, "type": p["settings"]["type"], "status": self._publisher_status(mt.group(1), pid)}
                        for pid, p in ch["publishers"].items()])
        mt = m(r"/channels/([^/]+)/publishers", route)
        if mt:
            ch = self._channels.get(mt.group(1))
            if ch is None:
                return _not_found("Channel not found")
            if method == "GET":
                return _ok([{"id": pid, "type": p["settings"]["type"], "name": p["name"]}
                            for pid, p in ch["publishers"].items()])
            if method == "POST":
                return self._add_publisher(mt.group(1), data)
        mt = m(r"/channels/([^/]+)/publishers/([^/]+)(/.*)?", route)
        if mt:
            cid, pid, tail = mt.group(1), mt.group(2), mt.group(3) or ""
            ch = self._channels.get(cid)
            if ch is None:
                return _not_found(f"channel '{cid}' not found")
            pub = ch["publishers"].get(pid)
            if pub is None:
                return _not_found(f"streamer '{pid}' for channel '{cid}' not found")
            if tail == "" and method == "DELETE":
                del ch["publishers"][pid]
                return _ok()
            if tail == "/control/start" and method == "POST":
                self._publisher_control(pub, "start")
                return _ok()
            if tail == "/control/stop" and method == "POST":
                self._publisher_control(pub, "stop")
                return _ok()
            if tail == "/type" and method == "GET":
                return _ok(pub["settings"]["type"])
            if tail == "/name":
                if method == "GET":
                    return _ok(pub["name"])
                if method == "PUT":
                    name = query.get("name", "")
                    if not 1 <= len(name) <= 128:
                        return _error(400, "badrequest", "Invalid name")
                    pub["name"] = name
                    return _ok(name)
            if tail == "/status" and method == "GET":
                return _ok(self._publisher_status(cid, pid))
            if tail == "/settings":
                if method == "GET":
                    return _ok(copy.deepcopy(pub["settings"]))
                if method == "PUT":
                    if not isinstance(data, dict) or "type" not in data:
                        return _error(400, "badrequest", "Invalid settings")
                    pub["settings"] = copy.deepcopy(data)
                    return _ok(copy.deepcopy(pub["settings"]))
                if method == "PATCH":
                    if not isinstance(data, dict):
                        return _error(400, "badrequest", "Invalid settings")
                    _deep_merge(pub["settings"], copy.deepcopy(data))
                    # The document shows this reply in the legacy flat shape;
                    # so does the simulator, so a driver cannot lean on it.
                    ptype = pub["settings"]["type"]
                    block = pub["settings"].get(ptype.replace("-", "_"), {})
                    flat = {"started": pub["started"], "single-touch": pub["settings"]["common"].get("single_touch"),
                            "disable-audio": block.get("disable_audio", False)}
                    flat.update({k: v for k, v in block.items() if k != "disable_audio"})
                    return _ok(flat)
        # Inputs
        if route == "/inputs":
            if method == "GET":
                types = [t for t in query.get("types", "").split(",") if t]
                ids = [i for i in query.get("ids", "").split(",") if i]
                out = []
                for sid, inp in self._inputs.items():
                    if types and inp["type"] not in types:
                        continue
                    if ids and sid not in ids:
                        continue
                    out.append({"id": sid, "name": inp["name"], "real_device_name": inp["real"],
                                "audio": inp["audio"], "video": inp["video"], "type": inp["type"]})
                return _ok(out)
            if method == "POST":
                return self._add_input(data)
        mt = m(r"/inputs/([^/]+)/preview", route)
        if mt and method == "GET":
            if mt.group(1) not in self._inputs:
                return _not_found("Input not found")
            return 200, "JPEG"
        mt = m(r"/inputs/([^/]+)/settings", route)
        if mt:
            inp = self._inputs.get(mt.group(1))
            if inp is None:
                return _not_found("Input not found")
            if inp["settings"] is None:
                return _error(405, "notallowed", "Input settings are not supported")
            if method == "GET":
                return _ok(copy.deepcopy(inp["settings"]))
            if method == "PUT":
                if not isinstance(data, dict):
                    return _error(400, "badrequest", "Invalid settings")
                inp["settings"] = copy.deepcopy(data)
                return _ok(copy.deepcopy(inp["settings"]))
            if method == "PATCH":
                if not isinstance(data, dict):
                    return _error(400, "badrequest", "Invalid settings")
                _deep_merge(inp["settings"], copy.deepcopy(data))
                return _ok(copy.deepcopy(inp["settings"]))
        # Outputs
        if route == "/outputs" and method == "GET":
            ids = [i for i in query.get("ids", "").split(",") if i]
            return _ok([{"id": did, "name": o["name"]} for did, o in self._outputs.items()
                        if not ids or did in ids])
        mt = m(r"/outputs/([^/]+)/preview", route)
        if mt and method == "GET":
            if mt.group(1) not in self._outputs:
                return _not_found(f"output '{mt.group(1)}' not found")
            return 200, "JPEG"
        mt = m(r"/outputs/([^/]+)/settings", route)
        if mt and method == "PUT":
            out = self._outputs.get(mt.group(1))
            if out is None:
                return _not_found(f"output '{mt.group(1)}' not found")
            source = query.get("source", "")
            if not source:
                return _error(400, "badrequest", "Missing source")
            if source not in self._channels and source not in self._inputs and \
                    source not in ("multiview", "deviceinfo", "console"):
                return _error(400, "badrequest", f"Unknown source '{source}'")
            out["source"] = source
            return _ok()
        # Recorders
        if route == "/recorders" and method == "GET":
            ids = [i for i in query.get("ids", "").split(",") if i]
            return _ok([{"id": rid, "name": r["name"], "multisource": r["multisource"]}
                        for rid, r in self._recorders.items() if not ids or rid in ids])
        if route == "/recorders/status" and method == "GET":
            ids = [i for i in query.get("ids", "").split(",") if i]
            return _ok([{"id": rid, "name": r["name"], "status": self._recorder_status(rid)}
                        for rid, r in self._recorders.items() if not ids or rid in ids])
        mt = m(r"/recorders/control/(start|stop)", route)
        if mt and method == "POST":
            ids = [i for i in query.get("ids", "").split(",") if i]
            for rid, rec in self._recorders.items():
                if not ids or rid in ids:
                    self._recorder_control(rid, rec, mt.group(1))
            return _ok()
        mt = m(r"/recorders/([^/]+)/control/(start|stop)", route)
        if mt and method == "POST":
            rec = self._recorders.get(mt.group(1))
            if rec is None:
                return _not_found("Recorder not found")
            if rec["state"] == "disabled":
                return _error(400, "badrequest", "Recorder is disabled")
            self._recorder_control(mt.group(1), rec, mt.group(2))
            return _ok()
        mt = m(r"/recorders/([^/]+)/status", route)
        if mt and method == "GET":
            if mt.group(1) not in self._recorders:
                return _not_found("Recorder not found")
            return _ok(self._recorder_status(mt.group(1)))
        mt = m(r"/recorders/([^/]+)/archive/files", route)
        if mt and method == "GET":
            rec = self._recorders.get(mt.group(1))
            if rec is None:
                return _not_found("Recorder not found")
            files = list(rec["files"])
            start = int(query.get("from", "0") or 0)
            limit = query.get("limit")
            files = files[start:]
            if limit:
                files = files[: int(limit)]
            return _ok(copy.deepcopy(files))
        # Events
        if route == "/schedule/events/adhoc/session":
            if method == "GET":
                if self._adhoc_session is None:
                    return _not_found("No ad-hoc session")
                return 200, json.dumps(self._adhoc_session)
            if method == "POST":
                if not isinstance(data, dict) or not data.get("id"):
                    return _error(400, "badrequest", "Missing user id")
                now = int(time.time())
                self._adhoc_session = {"id": str(data["id"]), "name": f"User {data['id']}",
                                       "created": now, "expired": now + 3600}
                if "password" in data:
                    self._adhoc_session["default_folder"] = "folder-1"
                return 200, json.dumps(self._adhoc_session)
            if method == "DELETE":
                self._adhoc_session = None
                return _ok()
        if route == "/schedule/events":
            if method == "GET":
                events = list(self._events.values())
                status = query.get("status")
                if status:
                    events = [e for e in events if e["status"] == status]
                limit = query.get("limit")
                if limit:
                    events = events[: int(limit)]
                return _ok(copy.deepcopy(events))
            if method == "POST":
                return self._create_adhoc_event(data)
        mt = m(r"/schedule/events/([^/]+)", route)
        if mt and method == "GET":
            event = self._resolve_event(mt.group(1))
            if event is None:
                return _not_found("Event not found")
            return _ok(copy.deepcopy(event))
        mt = m(r"/schedule/events/([^/]+)/control/(start|stop|pause|resume|extend)", route)
        if mt and method == "POST":
            event = self._resolve_event(mt.group(1))
            if event is None:
                return _not_found("Event not found")
            return self._event_control(event, mt.group(2), data)
        return _not_found("Not found")

    # ── Channel and publisher helpers ──

    def _channel_list(self, query: dict[str, str]) -> list[dict[str, Any]]:
        ids = [i for i in query.get("ids", "").split(",") if i]
        out = []
        for cid, ch in self._channels.items():
            if ids and cid not in ids:
                continue
            entry: dict[str, Any] = {"id": cid, "name": ch["name"]}
            if query.get("publishers") == "true":
                pubs = []
                for pid, pub in ch["publishers"].items():
                    p: dict[str, Any] = {"id": pid, "type": pub["settings"]["type"], "name": pub["name"]}
                    if query.get("publishers-status") == "true":
                        p["status"] = self._publisher_status(cid, pid)
                    if query.get("publishers-settings") == "true":
                        p["settings"] = copy.deepcopy(pub["settings"])
                    pubs.append(p)
                entry["publishers"] = pubs
            if query.get("encoders") == "true":
                entry["encoders"] = copy.deepcopy(ch["encoders"])
            if query.get("active_layout") == "true":
                lid = ch["active_layout"]
                entry["active_layout"] = {
                    "id": lid, "name": ch["layouts"][lid],
                    "sources": {"video": [{"id": "D2P0.hdmi-a", "name": "HDMI-A"}],
                                "audio": [{"id": "D2P0.hdmi-a-audio", "name": "HDMI-A Audio"}]},
                }
            out.append(entry)
        return out

    def _publisher_status(self, cid: str, pid: str) -> dict[str, Any]:
        pub = self._channels[cid]["publishers"][pid]
        settings = pub["settings"]
        ptype = settings["type"]
        block = settings.get(ptype.replace("-", "_"), {})
        configured = True
        if ptype in ("rtmp", "rtsp", "hls"):
            configured = bool(block.get("url"))
        elif ptype == "srt" and block.get("mode") != "listener":
            configured = bool(block.get("url"))
        status: dict[str, Any] = {
            "is_configured": configured,
            "started": pub["started"],
            "state": pub["state"],
        }
        if cid == "1" and pid == "0" and self.has_error_behavior("publisher_error"):
            status["state"] = "error"
            status["description"] = "Connection refused by the RTMP server"
        if pub["started"] and pub["state"] in ("started", "listening", "starting"):
            duration = int(time.time() - pub["since"])
            status["duration"] = duration
            status["since"] = int(pub["since"])
            status["reconnections"] = pub["reconnections"]
            if ptype == "srt" and pub["state"] == "started":
                status["statistics"] = {
                    "total": {"duration": duration, "pkt_sent": duration * 340, "pkt_loss": 0,
                              "pkt_retrans": 0, "pkt_drop": 0, "latency": block.get("latency", 125),
                              "byte_sent": duration * 440000, "byte_retrans": 0, "byte_drop": 0},
                    "current": {"duration": min(duration, 20), "send_rate": 3.61, "loss_ratio": 0,
                                "retrans_ratio": 0, "drop_ratio": 0, "latency": block.get("latency", 125),
                                "rtt": 342, "estimated_bandwidth": 318.624, "send_buffer": 1000,
                                "stream_id": "", "ip": "192.168.86.102"},
                }
        return status

    def _publisher_control(self, pub: dict[str, Any], action: str) -> None:
        if action == "start":
            if not pub["settings"]["common"].get("enabled", False):
                return
            if not pub["started"]:
                pub["started"] = True
                pub["state"] = "starting"
                pub["since"] = time.time()
                pub["pending"] = time.time() + SETTLE_S
        else:
            pub["started"] = False
            pub["state"] = "stopped"
            pub["since"] = 0.0

    def _add_publisher(self, cid: str, data: Any):
        if not isinstance(data, dict) or not isinstance(data.get("settings"), dict):
            return _error(400, "badrequest", "Missing settings")
        settings = copy.deepcopy(data["settings"])
        ptype = settings.get("type")
        if ptype not in ("rtsp", "rtmp", "rtp-udp", "mpegts-udp", "mpegts-rtp", "ndi", "hls", "srt"):
            return _error(400, "badrequest", "Invalid publisher type")
        if ptype.replace("-", "_") not in settings:
            return _error(400, "badrequest", f"Missing {ptype} settings")
        settings.setdefault("common", {"enabled": False, "single_touch": True})
        pid = str(self._next_publisher[cid])
        self._next_publisher[cid] += 1
        name = str(data.get("name") or f"Stream {int(pid) + 1}")
        self._channels[cid]["publishers"][pid] = {
            "name": name, "settings": settings, "started": False, "state": "stopped",
            "since": 0.0, "pending": 0.0, "reconnections": 0,
        }
        return _created({"id": pid, "name": name, "settings": copy.deepcopy(settings)})

    # ── Recorders ──

    def _recorder_status(self, rid: str) -> dict[str, Any]:
        rec = self._recorders[rid]
        status: dict[str, Any] = {"state": rec["state"]}
        if rec["state"] in ("started", "starting"):
            status["duration"] = int(time.time() - rec["since"])
            status["active"] = "1"
            status["total"] = "1"
        return status

    def _recorder_control(self, rid: str, rec: dict[str, Any], action: str) -> None:
        if action == "start":
            if rec["state"] in ("stopped", "error"):
                rec["state"] = "starting"
                rec["since"] = time.time()
                rec["pending"] = time.time() + SETTLE_S
                self._file_seq += 1
                stamp = time.strftime("%Y-%m-%dT%H:%M:%S-0500")
                rec["files"].append({
                    "id": f"VGA.{int(rec['since'])}.{rec['name']}.mp4",
                    "name": f"{rec['name']}_{time.strftime('%b%d_%H-%M-%S')}",
                    "extension": "mp4", "recording": True, "downloaded": False, "created": stamp,
                    "duration": 0, "size": 0, "recorder": rid, "uploading": False, "event_id": "",
                })
        elif rec["state"] in ("started", "starting"):
            duration = int(time.time() - rec["since"])
            rec["state"] = "stopped"
            for f in rec["files"]:
                if f["recording"]:
                    f["recording"] = False
                    f["duration"] = duration
                    f["size"] = duration * 390000
            rec["since"] = 0.0

    # ── One-touch ──

    def _single_touch_state(self, stcid: str) -> dict[str, Any]:
        pressed = self._single_touch[stcid]["pressed"]
        pubs = [p for ch in self._channels.values() for p in ch["publishers"].values()
                if p["settings"]["common"].get("single_touch")]
        recs = list(self._recorders.values())
        rec_active = sum(1 for r in recs if r["state"] in ("started", "starting"))
        pub_active = sum(1 for p in pubs if p["started"])
        return {
            "pressed": pressed,
            "status": (rec_active == len(recs) and pub_active == len(pubs)) if pressed else True,
            "recorders": {"total": len(recs), "active": rec_active, "success": len(recs)},
            "publishers": {"total": len(pubs), "active": pub_active, "success": len(pubs)},
        }

    def _toggle_single_touch(self, stcid: str) -> None:
        stc = self._single_touch[stcid]
        stc["pressed"] = not stc["pressed"]
        action = "start" if stc["pressed"] else "stop"
        for rid, rec in self._recorders.items():
            self._recorder_control(rid, rec, action)
        for ch in self._channels.values():
            for pub in ch["publishers"].values():
                if pub["settings"]["common"].get("single_touch"):
                    self._publisher_control(pub, action)

    # ── AFU ──

    def _afu_status(self, aid: str) -> dict[str, Any]:
        state = str(self.get_state("afu_state", "idle"))
        status: dict[str, Any] = {"state": state, "protocol": self._afu[aid]["protocol"],
                                  "queue": {"files": 0, "size": 0}}
        if state == "uploading":
            status["queue"] = {"files": 1, "size": 184323243}
            status["file"] = {"recorder": "1", "id": "VGA.1736858583.HDMI-A.mp4",
                              "uploaded": 42000000, "size": 184323243}
        if state == "error":
            status["error"] = {"message": "Server refused the connection"}
        return status

    # ── Inputs ──

    def _add_input(self, data: Any):
        if not isinstance(data, dict) or not data.get("type"):
            return _error(400, "badrequest", "Missing input type")
        itype = str(data["type"])
        if itype not in ("rtsp", "srt", "ndi", "web-graphics"):
            return _error(405, "notallowed", "Input type is not allowed")
        if sum(1 for i in self._inputs.values() if i["type"] in ("rtsp", "srt", "ndi", "web-graphics")) >= self._max_network_inputs:
            return _error(409, "conflict", "The maximum number of allowed network inputs is reached")
        settings = copy.deepcopy(data.get("settings") or {})
        prefix = {"rtsp": "RTSP", "srt": "SRT", "ndi": "NDI", "web-graphics": "WEBG"}[itype]
        sid = f"{prefix}{self._next_input[itype]}"
        self._next_input[itype] += 1
        name = str(data.get("name") or sid)
        if itype in ("ndi", "web-graphics"):
            stored = None
        else:
            stored = settings
            stored.setdefault("audio", {"delay": 0})
            stored.setdefault("video", {"nosignal": {"image": "", "timeout": 5},
                                        "force_full_color_range": True, "hwaccel_decoding": True})
        self._inputs[sid] = {"name": name, "real": name, "audio": itype != "web-graphics", "video": True,
                             "type": itype, "settings": stored, "created": settings}
        return _created(sid)

    # ── Events ──

    def _resolve_event(self, key: str) -> dict[str, Any] | None:
        if key in self._events:
            return self._events[key]
        events = list(self._events.values())
        if key == "upcoming":
            scheduled = sorted((e for e in events if e["status"] == "scheduled"), key=lambda e: e["start"])
            return scheduled[0] if scheduled else None
        if key == "ongoing":
            for e in events:
                if e["status"] in ("running", "paused"):
                    return e
            return None
        if key in ("running", "paused"):
            for e in events:
                if e["status"] == key:
                    return e
            return None
        if key == "completed":
            finished = sorted((e for e in events if e["status"] == "finished"), key=lambda e: e["finish"])
            return finished[-1] if finished else None
        return None

    def _event_control(self, event: dict[str, Any], action: str, data: Any):
        now = int(time.time())
        if action == "start":
            if event["status"] != "scheduled":
                return _error(409, "conflict", "Only a scheduled event can be started")
            duration = event["finish"] - event["start"]
            event["start"] = now
            event["finish"] = now + duration
            event["status"] = "running"
            return _ok()
        if action == "stop":
            if event["status"] not in ("running", "paused"):
                return _error(409, "conflict", "Only a running or paused event can be stopped")
            event["status"] = "finished"
            event["finish"] = now
            return _ok()
        if action == "pause":
            if event["status"] != "running":
                return _error(409, "conflict", "Only a running event can be paused")
            event["status"] = "paused"
            return _ok()
        if action == "resume":
            if event["status"] != "paused":
                return _error(409, "conflict", "Only a paused event can be resumed")
            event["status"] = "running"
            return _ok()
        if action == "extend":
            if event["status"] not in ("running", "paused"):
                return _error(409, "conflict", "Only a running or paused event can be extended")
            seconds = data.get("finish") if isinstance(data, dict) else None
            if not isinstance(seconds, int) or seconds <= 0:
                return _error(400, "badrequest", "Missing finish")
            new_finish = event["finish"] + seconds
            for other in self._events.values():
                if other is not event and other["status"] == "scheduled" and other["start"] < new_finish:
                    return _error(409, "conflict", "The extended event overlaps the next scheduled event")
            event["finish"] = new_finish
            return _ok()
        return _not_found("Not found")

    def _create_adhoc_event(self, data: Any):
        if not isinstance(data, dict):
            return _error(400, "badrequest", "Missing event")
        title = str(data.get("title", ""))
        duration = data.get("duration")
        if not title or not isinstance(duration, int) or duration < 60:
            return _error(400, "badrequest", "Missing title or duration")
        if "type" in data and self._adhoc_session is None and not data.get("organizer"):
            return _error(409, "conflict", "No ad-hoc session")
        start = data.get("start", 0)
        now = int(time.time())
        start_at = now + start if isinstance(start, int) and start <= 31536000 else int(start)
        event_id = f"adhoc{len(self._events) + 1:04d}"
        event = {
            "id": event_id, "status": "scheduled" if start_at > now else "running",
            "title": title, "start": start_at, "finish": start_at + duration,
            "recorders": [{"id": "1"}], "streams": [], "tags": str(data.get("tags", "")),
        }
        self._events[event_id] = event
        return _created(copy.deepcopy(event))
