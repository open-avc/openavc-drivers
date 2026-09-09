"""
BrightSign Player (Local DWS) — Simulator.

Simulates a BrightSign player's Local Diagnostic Web Server REST API: the
route list, player information, health, the clock, the video mode, every
HDMI output (status, power-save, modes, EDID), reboot (with the factory reset
and autorun variants), the local-DWS switch, the snapshot, sendCecX, the
registry (dump, read, write, delete, flush), the supervisor log level, the
network diagnostics, and, when configured as a Moka display, the
display-control endpoints. Every reply is wrapped the way the player wraps
it: {"data": {"result": ...}} on success, {"data": {"error": {"status",
"message"}}} on an error.

Serves HTTPS with a self-signed certificate like a player on BrightSignOS
9.0.218 or later, so the driver connects to it exactly as it does to a real
player (Use HTTPS on, verification off).

Authentication: with a ``password`` configured, every route demands RFC 2617
HTTP Digest as user ``admin`` against it, and a password of ``invalid`` is
always refused, which is how the driver's auth-failure path is exercised.
With the password blank the player is open, which BrightSign allows and the
connect-lifecycle smoke relies on.

Configuration: ``outputs`` (1, 2 or 4 HDMI outputs, default 1), ``moka``
(True for a Moka display with the display-control API), ``password`` (blank
= open), ``reboot_downtime`` (seconds the player answers nothing after a
reboot; default 2), ``udp_port`` (0 = off; otherwise the simulator also
listens on that UDP port and records the last message it received, which is
what a presentation's UDP Input event would match).

Driver: brightsign_player
Transport: http
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
from datetime import datetime
from typing import Any
from urllib.parse import unquote, urlsplit

from openavc.simulator.http_simulator import HTTPSimulator

SERIAL = "SIMD5X000001"
REALM = "BrightSign"
API = "/api/v1"

# Model -> HDMI output count, from BrightSign's own note on the video
# endpoints: 0 on single-output players, 0-1 on the XC2055 and XT2145,
# 0-3 on the XC4055.
OUTPUT_COUNTS = {"XC4055": 4, "XC2055": 2, "XT2145": 2}

_MODE_1080 = {
    "colorDepth": "8bit", "colorSpace": "rgb", "dropFrame": False, "frequency": 60,
    "graphicsPlaneHeight": 1080, "graphicsPlaneWidth": 1920, "height": 1080,
    "interlaced": False, "modeName": "1920x1080x60p", "overscan": False,
    "preferred": True, "width": 1920,
}
_MODE_4K = {
    "colorDepth": "8bit", "colorSpace": "yuv420", "dropFrame": False, "frequency": 60,
    "graphicsPlaneHeight": 2160, "graphicsPlaneWidth": 3840, "height": 2160,
    "interlaced": False, "modeName": "3840x2160x60p", "overscan": False,
    "preferred": False, "width": 3840,
}
_MODES = [_MODE_1080, _MODE_4K]

# The registry sections a fresh player carries, abridged from the reference dump.
_DEFAULT_REGISTRY: dict[str, dict[str, str]] = {
    "autorun": {"bootchecks": "", "bootstrap": "", "knowngood": "", "lasttried": ""},
    "brightscript": {"createdby": "Supervisor 2.1.10"},
    "networking": {
        "dwse": "yes", "dwsp": "", "enableremotesnapshot": "yes", "dhcp": "yes",
        "un": "XD5", "tz": "PST", "ts": "http://time.brightsignnetwork.com",
        "registered_with_bsn": "no", "wifi": "no",
    },
    "html": {"use-brightsign-media-player": "0"},
}


def _ok(result: Any, status: int = 200) -> tuple[int, dict]:
    return status, {"data": {"result": result}}


def _err(status: int, message: str) -> tuple[int, dict]:
    return status, {"data": {"error": {"status": status, "message": message}}}


class BrightSignPlayerSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "brightsign_player",
        "name": "BrightSign Player Simulator",
        "category": "streaming",
        "transport": "http",
        "default_port": 443,
        "tls": True,
        "initial_state": {
            "serial": SERIAL,
            "model": "XD1035",
            "family": "cobra",
            "firmware_version": "9.1.100",
            "boot_version": "9.1.85",
            "device_name": f"XD5-{SERIAL}",
            "device_description": "Lobby sign",
            "uptime_seconds": 1832,
            "ip_address": "192.168.1.174",
            "mac_address": "90:ac:3f:2a:01:79",
            "power_source": "AC",
            "poe_status": "inactive",
            "health": "active",
            "timezone_name": "America/New_York",
            "timezone_abbr": "EST",
            "clock_offset_s": 0,
            "video_mode": "1920x1080x60p",
            "local_dws_enabled": True,
            "log_level": 2,
            "output_0_connected": True,
            "output_0_powered": True,
            "output_0_power_save": False,
            "output_0_unstable": False,
            "output_1_connected": True,
            "output_1_powered": True,
            "output_1_power_save": False,
            "output_1_unstable": False,
            "output_2_connected": False,
            "output_2_powered": False,
            "output_2_power_save": False,
            "output_2_unstable": False,
            "output_3_connected": False,
            "output_3_powered": False,
            "output_3_power_save": False,
            "output_3_unstable": False,
            "display_power": "on",
            "display_volume": 50,
            "display_brightness": 50,
            "display_contrast": 45,
            "display_standby_timeout": 60,
            "display_video_output": "HDMI1",
            "display_always_on": False,
            "display_always_connected": True,
            "display_wb_red": 120,
            "display_wb_green": 120,
            "display_wb_blue": 120,
            "internet_ok": True,
            "last_cec_command": "",
            "last_snapshot_file": "",
            "last_udp_message": "",
            "reboot_count": 0,
            "last_reboot_kind": "",
        },
        "delays": {"command_response": 0.02},
        "controls": [
            {"type": "toggle", "key": "output_0_connected", "label": "HDMI 1: display connected"},
            {"type": "toggle", "key": "output_0_powered", "label": "HDMI 1: display powered"},
            {"type": "toggle", "key": "output_0_power_save", "label": "HDMI 1: power save"},
            {"type": "toggle", "key": "output_1_connected", "label": "HDMI 2: display connected (2+ outputs)"},
            {"type": "select", "key": "video_mode", "label": "Video mode", "options": ["1920x1080x60p", "3840x2160x60p"]},
            {"type": "select", "key": "log_level", "label": "Log level", "options": [0, 1, 2, 3],
             "labels": {0: "error", 1: "warn", 2: "info", 3: "trace"}},
            {"type": "toggle", "key": "internet_ok", "label": "Internet reachable"},
            {"type": "select", "key": "display_power", "label": "Moka display power", "options": ["on", "standby"]},
            {"type": "slider", "key": "display_volume", "min": 0, "max": 100, "label": "Moka display volume"},
            {"type": "indicator", "key": "last_udp_message", "label": "Last UDP message"},
            {"type": "indicator", "key": "last_cec_command", "label": "Last CEC payload"},
            {"type": "indicator", "key": "reboot_count", "label": "Reboots"},
        ],
        "error_modes": {
            "communication_timeout": {
                "description": "Player stops responding to HTTP requests",
                "behavior": "no_response",
            },
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        cfg = self.config or {}
        self._password = str(cfg.get("password", "") or "")
        self._outputs = int(cfg.get("outputs", 0) or 0)
        if self._outputs <= 0:
            self._outputs = OUTPUT_COUNTS.get(str(self.state.get("model", "")), 1)
        self._moka = bool(cfg.get("moka", False))
        self._reboot_downtime = float(cfg.get("reboot_downtime", 2.0))
        self._udp_port = int(cfg.get("udp_port", 0) or 0)
        self._registry: dict[str, dict[str, str]] = {k: dict(v) for k, v in _DEFAULT_REGISTRY.items()}
        self._nonces: set[str] = set()
        self._down_until = 0.0
        self._boot_at = time.time() - int(self.state.get("uptime_seconds", 0) or 0)
        self._udp_transport: asyncio.DatagramTransport | None = None
        self.calls: list[str] = []
        self.udp_messages: list[str] = []

    # ── Lifecycle (the UDP receiver beside the HTTP server) ──

    async def start(self, port: int) -> None:
        await super().start(port)
        if self._udp_port > 0:
            loop = asyncio.get_running_loop()
            self._udp_transport, _ = await loop.create_datagram_endpoint(
                lambda: _PresentationUdpProtocol(self),
                local_addr=("127.0.0.1", self._udp_port),
            )

    async def stop(self) -> None:
        if self._udp_transport is not None:
            self._udp_transport.close()
            self._udp_transport = None
        await super().stop()

    def udp_received(self, data: bytes) -> None:
        """A presentation's UDP Input: record the message string."""
        text = data.decode("utf-8", errors="replace")
        self.udp_messages.append(text)
        self.set_state("last_udp_message", text)
        self.log_protocol("in", f"UDP {text}")

    # ── Authentication (RFC 2617 Digest, user admin) ──

    def _challenge(self) -> tuple[int, str, dict[str, str]]:
        nonce = secrets.token_hex(16)
        self._nonces.add(nonce)
        return 401, "Unauthorized", {
            "WWW-Authenticate": f'Digest realm="{REALM}", nonce="{nonce}", algorithm=MD5, qop="auth"',
        }

    def _digest_ok(self, method: str, path: str, headers: dict[str, str]) -> bool:
        auth = ""
        for k, v in headers.items():
            if k.lower() == "authorization":
                auth = v
        if not auth.startswith("Digest "):
            return False
        fields = {k.lower(): v.strip('"') for k, v in re.findall(r'(\w+)=("[^"]*"|[^,\s]+)', auth[7:])}
        if fields.get("username", "") != "admin":
            return False
        nonce = fields.get("nonce", "")
        if nonce not in self._nonces:
            return False
        ha1 = hashlib.md5(f"admin:{fields.get('realm', '')}:{self._password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{fields.get('uri', '')}".encode()).hexdigest()
        if fields.get("qop"):
            expected = hashlib.md5(
                f"{ha1}:{nonce}:{fields.get('nc', '')}:{fields.get('cnonce', '')}:{fields.get('qop', '')}:{ha2}".encode()
            ).hexdigest()
        else:
            expected = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        return fields.get("response", "") == expected

    # ── Dispatch ──

    def handle_request(self, method: str, path: str, headers: dict[str, str], body: str):
        parts = urlsplit(path)
        route = unquote(parts.path).rstrip("/") or "/"
        if time.time() < self._down_until:
            # Rebooting: a real player drops the connection; the nearest an
            # HTTP answer gets is a gateway error the driver treats as no
            # answer at all.
            return 503, "Service Unavailable"
        if self._password and not self._digest_ok(method, path, headers):
            return self._challenge()
        if self._password == "invalid":
            return self._challenge()
        if not route.startswith(API):
            return 404, "Not Found"
        route = route[len(API):] or "/"
        self.calls.append(f"{method} {route}")
        try:
            payload = json.loads(body) if body else {}
        except ValueError:
            return _err(400, "Invalid JSON body")
        if not isinstance(payload, dict):
            payload = {}
        return self._route(method, route, payload)

    def _route(self, method: str, route: str, payload: dict[str, Any]):
        seg = [s for s in route.split("/") if s]
        if route == "/" and method == "GET":
            return _ok({"routes": self._route_list()})
        if route == "/health" and method == "GET":
            return _ok({"status": self.state.get("health", "active"), "statusTime": self._now_text()})
        if route == "/info" and method == "GET":
            return _ok(self._info())
        if route == "/time":
            if method == "GET":
                return _ok(self._time())
            if method == "PUT":
                return self._set_time(payload)
        if route == "/video-mode" and method == "GET":
            return _ok(self._video_mode())
        if seg[:3] == ["video", "hdmi", "output"] and len(seg) >= 4:
            return self._video_output(method, seg[3:], payload)
        if route == "/control/reboot" and method == "PUT":
            return self._reboot(payload)
        if route == "/control/local-dws":
            if method == "GET":
                return _ok({"success": True, "value": bool(self.state.get("local_dws_enabled", True))})
            if method == "PUT":
                self.set_state("local_dws_enabled", bool(payload.get("enable", True)))
                return _ok({"success": True, "reboot": True})
        if route == "/control/dws-password" and method == "GET":
            return _ok({"success": True, "password": {"isResultValid": True, "isBlank": not self._password}})
        if route == "/snapshot" and method == "POST":
            return self._snapshot(payload)
        if route == "/sendCecX" and method == "POST":
            return self._send_cec(payload)
        if seg[:1] == ["registry"]:
            return self._registry_route(method, seg[1:], payload)
        if route == "/system/supervisor/logging":
            if method == "GET":
                level = int(self.state.get("log_level", 2))
                return _ok({"status": 200, "level": str(level), "name": {0: "error", 1: "warn", 2: "info", 3: "trace"}[level]})
            if method == "PUT":
                level = payload.get("level")
                if level not in (0, 1, 2, 3):
                    return _err(400, "level must be 0, 1, 2 or 3")
                self.set_state("log_level", int(level))
                return _ok({"status": 200})
        if route == "/diagnostics" and method == "GET":
            return _ok(self._diagnostics())
        if seg[:1] == ["display-control"]:
            return self._display_control(method, seg[1:], payload)
        return _err(404, f"Route {method} {route} not found")

    # ── Player information ──

    def _now_text(self) -> str:
        now = datetime.now()
        offset = float(self.state.get("clock_offset_s", 0) or 0)
        stamp = datetime.fromtimestamp(now.timestamp() + offset)
        return f"{stamp.strftime('%Y-%m-%d %H:%M:%S')} {self.state.get('timezone_abbr', 'EST')}"

    def _uptime_seconds(self) -> int:
        return int(time.time() - self._boot_at)

    def _info(self) -> dict[str, Any]:
        mac = self.state.get("mac_address", "90:ac:3f:2a:01:79")
        ip = self.state.get("ip_address", "192.168.1.174")
        up = self._uptime_seconds()
        return {
            "serial": self.state.get("serial", SERIAL),
            "upTime": f"{up // 60} minutes",
            "upTimeSeconds": up,
            "model": self.state.get("model", "XD1035"),
            "FWVersion": self.state.get("firmware_version", "9.1.100"),
            "bootVersion": self.state.get("boot_version", "9.1.85"),
            "family": self.state.get("family", "cobra"),
            "isPlayer": True,
            "power": {"result": {"battery": "absent", "source": self.state.get("power_source", "AC"), "switch_mode": "hard"}},
            "poe": {"result": {"status": self.state.get("poe_status", "inactive")}},
            "extensions": {"result": {"extensions": []}},
            "blessings": {"result": {"ac3": False, "eac3": False}},
            "networking": {"result": {
                "description": self.state.get("device_description", ""),
                "name": self.state.get("device_name", f"XD5-{SERIAL}"),
            }},
            "hardware_features": {
                "hdmi": True, "ethernet": True, "usb": True, "cec": True, "wifi": False,
                "spdif": True, "onboard storage": True, "disable dws": False,
            },
            "api_features": {"video": True},
            "active_features": {"legacyDWS": True},
            "connectionType": "eth0",
            "ethernet": [{
                "interfaceName": "eth0", "interfaceType": "Ethernet",
                "IPv4": [{"address": ip, "netmask": "255.255.255.0", "family": "IPv4",
                          "mac": mac, "internal": False, "cidr": f"{ip}/24"}],
                "IPv6": [],
            }],
            "wireless": [],
            "interfaces": [],
            "bsnce": False,
        }

    def _time(self) -> dict[str, Any]:
        offset = float(self.state.get("clock_offset_s", 0) or 0)
        stamp = datetime.fromtimestamp(time.time() + offset)
        return {
            "time": f"{stamp.strftime('%Y-%m-%d %H:%M:%S')} {self.state.get('timezone_abbr', 'EST')}",
            "timezone_mins": None,
            "timezone_name": self.state.get("timezone_name", "America/New_York"),
            "timezone_abbr": self.state.get("timezone_abbr", "EST"),
            "year": stamp.year, "month": stamp.month, "date": stamp.day,
            "hour": stamp.hour, "minute": stamp.minute, "second": stamp.second,
            "millisecond": 0,
        }

    def _set_time(self, payload: dict[str, Any]):
        # Accept the flat body (the reference prose and BrightSign's own CLI)
        # and the wrapped one the reference example shows.
        body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        date = str(body.get("date", ""))
        clock = str(body.get("time", "")).split(" ")[0]
        try:
            if len(clock) == 5:
                clock += ":00"
            wanted = datetime.strptime(f"{date} {clock}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return _err(400, "date must be YYYY-MM-DD and time HH:mm")
        # A float, so the clock reads exactly the time that was set plus the
        # seconds elapsed since, never a second behind it.
        self.set_state("clock_offset_s", wanted.timestamp() - time.time())
        return _ok(True)

    def _mode(self) -> dict[str, Any]:
        name = self.state.get("video_mode", "1920x1080x60p")
        for mode in _MODES:
            if mode["modeName"] == name:
                return dict(mode)
        return dict(_MODE_1080)

    def _video_mode(self) -> dict[str, Any]:
        mode = self._mode()
        return {
            "isAutoMode": False,
            "name": mode["modeName"],
            "width": str(mode["width"]),
            "height": str(mode["height"]),
            "frames": str(mode["frequency"]),
            "scan": "i" if mode["interlaced"] else "p",
            "mode": mode,
        }

    # ── HDMI outputs ──

    def _output_index(self, seg: list[str]) -> int | None:
        try:
            n = int(seg[0])
        except (ValueError, IndexError):
            return None
        return n if 0 <= n < self._outputs else None

    def _output_status(self, n: int) -> dict[str, Any]:
        connected = bool(self.state.get(f"output_{n}_connected", False))
        powered = connected and bool(self.state.get(f"output_{n}_powered", False))
        power_save = bool(self.state.get(f"output_{n}_power_save", False))
        mode = self._mode()
        return {
            "resolutions": {"graphics": {"width": mode["graphicsPlaneWidth"], "height": mode["graphicsPlaneHeight"]},
                            "output": {"width": mode["width"], "height": mode["height"]},
                            "video": {"width": mode["width"], "height": mode["height"]}},
            "edid_identity": {"manufacturer": "SAM", "model": "S24"} if connected and not power_save else {},
            "edid": "00ffffffffffff00" if connected and not power_save else "0000000000000000",
            "status": {
                "audioBitsPerSample": 16, "audioChannelCount": 2, "audioFormat": "PCM",
                "audioSampleRate": 48000, "eotf": "SDR (GAMMA)",
                "outputPowered": powered and not power_save,
                "outputPresent": connected,
                "unstable": bool(self.state.get(f"output_{n}_unstable", False)),
            },
            "modes": [dict(m) for m in _MODES],
            "activeMode": mode,
            "bestMode": _MODE_4K["modeName"] if connected else "",
            "configuredMode": mode,
            "powerSaveStatus": power_save,
        }

    def _video_output(self, method: str, seg: list[str], payload: dict[str, Any]):
        n = self._output_index(seg)
        if n is None:
            return _err(404, "No such video output")
        sub = seg[1:]
        if not sub and method == "GET":
            return _ok(self._output_status(n))
        if sub == ["edid"] and method == "GET":
            return _ok(self._output_status(n)["edid"])
        if sub == ["modes"] and method == "GET":
            return _ok([dict(m) for m in _MODES])
        if sub == ["power-save"]:
            if method == "GET":
                status = self._output_status(n)["status"]
                return _ok({"is_connected": status["outputPresent"], "is_powered": status["outputPowered"],
                            "enabled": bool(self.state.get(f"output_{n}_power_save", False))})
            if method == "PUT":
                if "enabled" not in payload:
                    return _err(400, "enabled is required")
                self.set_state(f"output_{n}_power_save", bool(payload.get("enabled")))
                return _ok(True)
        return _err(404, "Route not found")

    # ── Control ──

    def _reboot(self, payload: dict[str, Any]):
        kind = "reboot"
        message = "A reboot has been initiated"
        if payload.get("factory_reset"):
            kind = "factory_reset"
            message += " (with factory reset)"
            self._registry = {k: dict(v) for k, v in _DEFAULT_REGISTRY.items()}
        elif payload.get("crash_report"):
            kind = "crash_report"
            message += " (with crash report)"
        elif payload.get("autorun") == "disable":
            kind = "autorun_disabled"
        self.set_state("reboot_count", int(self.state.get("reboot_count", 0)) + 1)
        self.set_state("last_reboot_kind", kind)
        self._down_until = time.time() + self._reboot_downtime
        self._boot_at = self._down_until
        return _ok({"success": True, "message": message})

    def _snapshot(self, payload: dict[str, Any]):
        stamp = datetime.now()
        name = f"/sd/remote_snapshots/img-{stamp.strftime('%Y-%m-%d-%H-%M-%S')}.jpg"
        self.set_state("last_snapshot_file", name)
        return _ok({
            "remoteSnapshotThumbnail": "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/",
            "filename": name,
            "timestamp": f"{stamp.strftime('%Y-%m-%d %H:%M:%S')} {self.state.get('timezone_abbr', 'EST')}",
            "devicename": self.state.get("device_name", f"XD5-{SERIAL}"),
            "width": int(payload.get("width", 640) or 640),
            "height": int(payload.get("height", 480) or 480),
        })

    def _send_cec(self, payload: dict[str, Any]):
        command = str(payload.get("hexCommand", "")).strip()
        if not command or not re.fullmatch(r"[0-9a-fA-F]+", command) or len(command) % 2:
            return _err(400, "Invalid request parameter")
        self.set_state("last_cec_command", command.lower())
        return _ok({"success": True})

    # ── Registry ──

    def _registry_route(self, method: str, seg: list[str], payload: dict[str, Any]):
        if not seg:
            if method == "GET":
                return _ok({"success": True, "value": {k: dict(v) for k, v in self._registry.items()}})
            return _err(404, "Route not found")
        if seg == ["flush"] and method == "PUT":
            return _ok({"success": True})
        if seg == ["recovery_url"]:
            if method == "GET":
                return _ok({"success": True, "value": self._registry.get("networking", {}).get("ru", "")})
            if method == "PUT":
                self._registry.setdefault("networking", {})["ru"] = str(payload.get("url", ""))
                return _ok({"success": True})
        section = seg[0]
        if len(seg) == 1:
            if method == "GET":
                if section not in self._registry:
                    return _err(404, f"Registry section '{section}' not found")
                return _ok({"success": True, "section": section, "value": dict(self._registry[section])})
            if method == "DELETE":
                self._registry.pop(section, None)
                return _ok({"success": True, "section": section})
        if len(seg) == 2:
            key = seg[1]
            if method == "GET":
                value = self._registry.get(section, {}).get(key)
                if value is None:
                    return _err(404, f"Registry key '{section}/{key}' not found")
                return _ok({"success": True, "section": section, "key": key, "value": value})
            if method == "PUT":
                if "value" not in payload:
                    return _err(400, "value is required")
                self._registry.setdefault(section, {})[key] = str(payload.get("value"))
                return _ok({"success": True, "section": section, "key": key, "value": str(payload.get("value"))})
            if method == "DELETE":
                self._registry.get(section, {}).pop(key, None)
                return _ok({"success": True, "section": section, "key": key})
        return _err(404, "Route not found")

    # ── Diagnostics ──

    def _diagnostics(self) -> dict[str, Any]:
        internet_ok = bool(self.state.get("internet_ok", True))
        return {
            "ethernet": {
                "diagnosis": "OK",
                "log": [
                    {"name": "Checking model networking support", "pass": True, "result": ""},
                    {"name": "Checking for Ethernet interface", "pass": True, "result": ""},
                    {"name": "Checking interface type", "pass": True, "result": "Ethernet"},
                    {"name": "Checking Ethernet link", "pass": True, "result": ""},
                    {"name": "Checking Ethernet gateway", "pass": True, "result": "PING 10/10: 1375/1537/1623us"},
                ],
                "ok": True,
            },
            "wifi": {
                "diagnosis": "WiFi interface not present",
                "log": [{"name": "Checking for WiFi interface", "pass": False, "result": "WiFi interface not present"}],
                "ok": False,
            },
            "modem": {"diagnosis": "Modem interface not present", "log": [], "ok": False},
            "internet": {
                "diagnosis": "OK" if internet_ok else "Internet connectivity failed",
                "log": [
                    {"name": "Checking DNS servers", "pass": True, "result": "At least one DNS server is valid"},
                    {"name": "Checking Internet connectivity", "pass": internet_ok,
                     "result": "PING 10/10: 87398/87760/88247us" if internet_ok else "PING 0/10"},
                    {"name": "Checking time server", "pass": internet_ok, "result": ""},
                ],
                "ok": internet_ok,
            },
        }

    # ── Display control (Moka displays only) ──

    def _display_control(self, method: str, seg: list[str], payload: dict[str, Any]):
        if not self._moka:
            return _err(404, "Display control is not available on this player")
        st = self.state
        if not seg and method == "GET":
            return _ok({
                "tvInfo": {"macAddress": "ff:ff:ff:ff:ff:ff", "wifiMacAddress": "ff:ff:ff:ff:ff:ff",
                           "serialNo": "1234567890", "osVersion": "V8-AM963BS-0020015", "hwRevision": 1},
                "whiteBalance": {"redBalance": st.get("display_wb_red"), "greenBalance": st.get("display_wb_green"),
                                 "blueBalance": st.get("display_wb_blue")},
                "volume": st.get("display_volume"),
                "brightness": st.get("display_brightness"),
                "contrast": st.get("display_contrast"),
                "idleStandbyTimeout": st.get("display_standby_timeout"),
                "powerSetting": st.get("display_power"),
                "videoOutput": st.get("display_video_output"),
                "sdConnection": "brightsign",
                "alwaysConnectedEnabled": bool(st.get("display_always_connected")),
            })
        if len(seg) != 1:
            return _err(404, "Route not found")
        leaf = seg[0]
        simple = {
            "brightness": ("display_brightness", "brightness", int),
            "contrast": ("display_contrast", "contrast", int),
            "volume": ("display_volume", "volume", int),
        }
        if leaf in simple:
            key, field, cast = simple[leaf]
            if method == "GET":
                return _ok({field: st.get(key)})
            if method == "PUT":
                if field not in payload:
                    return _err(400, f"{field} is required")
                value = cast(payload[field])
                if not 0 <= value <= 100:
                    return _err(400, f"{field} must be between 0 and 100")
                self.set_state(key, value)
                return _ok({"success": True, field: value})
        if leaf == "standby-timeout":
            if method == "GET":
                return _ok({"seconds": st.get("display_standby_timeout")})
            if method == "PUT":
                self.set_state("display_standby_timeout", int(payload.get("seconds", 0)))
                return _ok({"success": True, "seconds": int(payload.get("seconds", 0))})
        if leaf == "video-output":
            if method == "GET":
                return _ok({"output": st.get("display_video_output")})
            if method == "PUT":
                self.set_state("display_video_output", str(payload.get("output", "")))
                return _ok({"success": True, "output": str(payload.get("output", ""))})
        if leaf == "power-settings":
            if method == "GET":
                return _ok({"setting": st.get("display_power")})
            if method == "PUT":
                setting = str(payload.get("setting", ""))
                if setting not in ("on", "standby"):
                    return _err(400, "setting must be on or standby")
                self.set_state("display_power", setting)
                return _ok({"success": True, "setting": setting})
        if leaf in ("always-on", "always-connected"):
            key = "display_always_on" if leaf == "always-on" else "display_always_connected"
            if method == "GET":
                return _ok({"enabled": bool(st.get(key))})
            if method == "PUT":
                self.set_state(key, bool(payload.get("enable", False)))
                return _ok({"success": True, "enable": bool(payload.get("enable", False))})
        if leaf == "white-balance":
            if method == "GET":
                return _ok({"redBalance": st.get("display_wb_red"), "greenBalance": st.get("display_wb_green"),
                            "blueBalance": st.get("display_wb_blue")})
            if method == "PUT":
                for field, key in (("redBalance", "display_wb_red"), ("greenBalance", "display_wb_green"),
                                   ("blueBalance", "display_wb_blue")):
                    if field not in payload:
                        return _err(400, f"{field} is required")
                for field, key in (("redBalance", "display_wb_red"), ("greenBalance", "display_wb_green"),
                                   ("blueBalance", "display_wb_blue")):
                    self.set_state(key, int(payload[field]))
                return _ok({"success": True, "redBalance": int(payload["redBalance"]),
                            "greenBalance": int(payload["greenBalance"]), "blueBalance": int(payload["blueBalance"])})
        if leaf == "info" and method == "GET":
            return _ok({"macAddress": "ff:ff:ff:ff:ff:ff", "serialNo": "1234567890",
                        "osVersion": "V8-AM963BS-0020015", "hwRevision": 1, "wifiMacAddress": "ff:ff:ff:ff:ff:ff"})
        return _err(404, "Route not found")

    # ── The route list GET /api/v1/ answers with ──

    def _route_list(self) -> list[dict[str, str]]:
        routes = [
            ("GET", "/api/v1/health", "Informational"), ("GET", "/api/v1/info", "Informational"),
            ("GET", "/api/v1/time", "Informational"), ("PUT", "/api/v1/time", "Informational"),
            ("GET", "/api/v1/video-mode", "Informational"),
            ("PUT", "/api/v1/control/reboot", "ControlManagement"),
            ("GET", "/api/v1/control/local-dws", "ControlManagement"),
            ("PUT", "/api/v1/control/local-dws", "ControlManagement"),
            ("GET", "/api/v1/registry", "Informational"),
            ("GET", "/api/v1/registry/:section/:key", "Informational"),
            ("PUT", "/api/v1/registry/:section/:key", "Informational"),
            ("DELETE", "/api/v1/registry/:section/:key", "Informational"),
            ("PUT", "/api/v1/registry/flush", "Informational"),
            ("POST", "/api/v1/snapshot", "ControlManagement"),
            ("GET", "/api/v1/video/:connector/output/:device", "Informational"),
            ("GET", "/api/v1/video/:connector/output/:device/power-save", "Informational"),
            ("PUT", "/api/v1/video/:connector/output/:device/power-save", "Informational"),
            ("GET", "/api/v1/system/supervisor/logging", "ControlManagement"),
            ("PUT", "/api/v1/system/supervisor/logging", "ControlManagement"),
            ("GET", "/api/v1/diagnostics", "Informational"),
            ("POST", "/api/v1/sendCecX", "ControlManagement"),
        ]
        return [{"method": m, "route": r, "securityLevel": s} for m, r, s in routes]


class _PresentationUdpProtocol(asyncio.DatagramProtocol):
    """The presentation's UDP receiver: every datagram is one message string."""

    def __init__(self, sim: BrightSignPlayerSimulator) -> None:
        self._sim = sim

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._sim.udp_received(data)
