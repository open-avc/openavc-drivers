"""
Bose Professional Videobar (Videobar REST API) — Simulator.

Simulates a Videobar VB1 or VB-S's REST API as the REST API Guide v1.5
describes it, with each property's models, actions and values from the SNMP
Admin Guide v1.5: ``GET /api?query=<ids>``, ``GET /api?delete=<ids>`` (back to
the default), and ``PUT`` / ``POST /api`` with a ``data`` field holding a JSON
object of id and value strings. ``data`` is taken as a form field or in a JSON
body. Each property answers for itself: ``{"status": "success", "value": ...}``
to a read, ``{"status": "success", "code": "0xe000"}`` to a write, 0xe001 for
a write that changes nothing, 0xe002 for an id this model does not have (the
VB1-only ids on a VB-S), 0xe003 for a value outside the SNMP guide's pattern
and 0xe004 for an action the property does not take. The guides do not show a
refused property's answer; this simulator writes ``{"status": "error", "code":
"0xe00N"}``.

Behavior: Volume Up and Volume Down move the volume one step and unmute the
loudspeaker. Applying the active preset moves the camera to that preset's
saved pan, tilt and zoom; saving a preset stores the camera's current ones.
Pairing turns itself off after ``pairing_timeout`` seconds (the SNMP guide's
bluetooth.pairingTimeout default, 180). A reboot, or a new device name, makes
the bar answer nothing for ``reboot_downtime`` seconds. Turning off the
interface the bar is reached through (Ethernet on a VB1, Wi-Fi on a VB-S, or
``connected_via``) makes it answer nothing until that interface is turned back
on in the Simulator UI. The pan, tilt and zoom steps are one unit; the guide
gives no step size or direction.

Serves HTTPS with a self-signed certificate (``"tls": True``), as the bar does.

Authentication: HTTP Basic. With ``username`` and ``password`` configured, only
those are accepted; with none, any credentials but a username or password of
``invalid`` are, which is how the driver's refused-password path is exercised.

Error modes: ``communication_timeout`` (answers nothing), ``password_changed``
(every request is refused 401), ``access_denied`` (every property answers
0xe006) and ``api_off`` (every request answers 404; what a bar with API access
off answers is not documented).

Configuration: ``model`` ("VB1" or "VB-S", default VB1), ``username``,
``password``, ``connected_via`` ("ethernet" or "wifi"), ``reboot_downtime``
(seconds, default 5), ``pairing_timeout`` (seconds, default 180).

Driver: bose_videobar
Transport: http
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from openavc.simulator.http_simulator import HTTPSimulator

# id -> (state key, kind, actions, value pattern, VB1 only). Actions: r get,
# w put, d delete (back to the default), p post. Patterns are the SNMP Admin
# Guide's "Regex for Values".
_BOOL = r"1|0"
_PTZ_PAN = r"0|-10|10|-?[1-9]"
_POSITION = r"(0|-10|10|-?[1-9]) (0|-10|10|-?[1-9]) ([1-9]|10)"
_TEXT_128 = r".{0,128}"
_IP = r"((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)"
PROPS: dict[str, tuple[str, str, str, str | None, bool]] = {
    "2": ("mic_mute", "bool", "rw", _BOOL, False),
    "15": ("", "action", "p", None, False),          # audio.micMuteToggle
    "33": ("speaker_mute", "bool", "rw", _BOOL, False),
    "34": ("", "action", "p", None, False),          # audio.loudspeakerMuteToggle
    "3": ("volume", "int", "rwd", r"[1-9][0-9]?|100|0", False),
    "4": ("", "action", "p", None, False),           # audio.loudspeakerVolumeUp
    "5": ("", "action", "p", None, False),           # audio.loudspeakerVolumeDown
    "6": ("camera_zoom", "int", "rwd", r"[1-9]|10", False),
    "7": ("camera_pan", "int", "rwd", _PTZ_PAN, False),
    "8": ("camera_tilt", "int", "rwd", _PTZ_PAN, False),
    "9": ("", "action", "p", None, False),           # camera.zoomIn
    "A": ("", "action", "p", None, False),           # camera.zoomOut
    "B": ("", "action", "p", None, False),           # camera.panLeft
    "C": ("", "action", "p", None, False),           # camera.panRight
    "D": ("", "action", "p", None, False),           # camera.tiltUp
    "E": ("", "action", "p", None, False),           # camera.tiltDown
    "F": ("", "action", "p", None, False),           # camera.applyActivePreset
    "12": ("", "action", "p", None, False),          # camera.savePresetHome
    "17": ("", "action", "p", None, False),          # camera.savePresetFirst
    "18": ("", "action", "p", None, False),          # camera.savePresetSecond
    "56": ("home_preset_position", "text", "rwd", _POSITION, False),
    "57": ("preset_1_position", "text", "rwd", _POSITION, False),
    "58": ("preset_2_position", "text", "rwd", _POSITION, False),
    "13": ("active_preset", "int", "rwd", r"1|2|3", False),
    "60": ("camera_state", "text", "r", None, False),
    "19": ("autoframing", "bool", "rwd", _BOOL, False),
    "14": ("bluetooth_pairing", "bool", "rw", _BOOL, False),
    "C6": ("", "action", "p", None, False),          # bluetooth.pairingStateToggle
    "67": ("bluetooth_on", "bool", "r", None, False),
    "6A": ("bluetooth_paired_device", "text", "r", None, False),
    "6B": ("bluetooth_connected", "bool", "r", None, False),
    "C2": ("bluetooth_streaming", "bool", "r", None, False),
    "6C": ("bluetooth_in_call", "bool", "r", None, False),
    "E4": ("", "action", "p", r"1|2|3", False),     # bluetooth.disconnect
    "36": ("usb_connected", "bool", "r", None, False),
    "37": ("usb_in_call", "bool", "r", None, False),
    "C7": ("gpi_mute", "bool", "r", None, True),
    "DA": ("people_count", "int", "r", None, True),
    "DC": ("people_present", "bool", "r", None, True),
    "D6": ("model", "text", "r", None, False),
    "10": ("serial_number", "text", "r", None, False),
    "16": ("firmware_version", "text", "r", None, False),
    "25": ("device_name", "text", "rwd", r".{1,22}", False),
    "26": ("room", "text", "rwd", _TEXT_128, False),
    "27": ("floor", "text", "rwd", _TEXT_128, False),
    "28": ("building", "text", "rwd", _TEXT_128, False),
    "DF": ("max_occupancy", "text", "rwd", _TEXT_128, False),
    "3A": ("bluetooth_enabled", "bool", "rwd", _BOOL, False),
    "3B": ("wifi_enabled", "bool", "rwd", _BOOL, False),
    "38": ("ethernet_enabled", "bool", "rwd", _BOOL, True),
    "C9": ("hdmi_enabled", "bool", "rwd", _BOOL, True),
    "74": ("ethernet_dhcp", "bool", "rwd", _BOOL, True),
    "75": ("ethernet_static_ip", "text", "rwd", _IP, True),
    "7F": ("ethernet_state", "text", "r", None, True),
    "80": ("ethernet_mac", "text", "r", None, True),
    "A1": ("wifi_dhcp", "bool", "rwd", _BOOL, False),
    "A2": ("wifi_static_ip", "text", "rwd", _IP, False),
    "AC": ("wifi_mac", "text", "r", None, False),
    "B0": ("wifi_state", "text", "r", None, False),
    "32": ("", "action", "p", None, False),          # system.reboot
}

# Factory defaults (the SNMP guide's Default Value), for a delete.
DEFAULTS = {
    "volume": 50, "camera_zoom": 1, "camera_pan": 0, "camera_tilt": 0,
    "home_preset_position": "0 0 1", "preset_1_position": "0 0 1", "preset_2_position": "0 0 1",
    "active_preset": 1, "autoframing": False, "device_name": "vb1", "room": "", "floor": "",
    "building": "", "max_occupancy": "", "bluetooth_enabled": True, "wifi_enabled": True,
    "ethernet_enabled": True, "hdmi_enabled": False, "ethernet_dhcp": True,
    "ethernet_static_ip": "0.0.0.0", "wifi_dhcp": True, "wifi_static_ip": "0.0.0.0",
}

ACTIONS_SAVE = {"12": ("home_preset_position", 1), "17": ("preset_1_position", 2), "18": ("preset_2_position", 3)}
PRESET_KEYS = {1: "home_preset_position", 2: "preset_1_position", 3: "preset_2_position"}


def _norm(pid: Any) -> str:
    text = str(pid).strip().upper()
    try:
        return format(int(text, 16), "X")
    except ValueError:
        return text


def _error(code: str) -> dict[str, str]:
    return {"status": "error", "code": code}


def _ok(code: str = "0xe000") -> dict[str, str]:
    return {"status": "success", "code": code}


class BoseVideobarSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "bose_videobar",
        "name": "Bose Videobar Simulator",
        "category": "video",
        "transport": "http",
        "default_port": 443,
        "tls": True,
        "initial_state": {
            "mic_mute": False,
            "speaker_mute": False,
            "volume": 50,
            "camera_zoom": 1,
            "camera_pan": 0,
            "camera_tilt": 0,
            "home_preset_position": "0 0 1",
            "preset_1_position": "-4 1 3",
            "preset_2_position": "5 -2 6",
            "active_preset": 1,
            "camera_state": "active",
            "autoframing": False,
            "bluetooth_pairing": False,
            "bluetooth_on": True,
            "bluetooth_paired_device": "Presenter Phone",
            "bluetooth_connected": False,
            "bluetooth_streaming": False,
            "bluetooth_in_call": False,
            "usb_connected": True,
            "usb_in_call": False,
            "gpi_mute": False,
            "people_count": 3,
            "people_present": True,
            "model": "vb1",
            "serial_number": "0F95GF89432048DFA",
            "firmware_version": "1.2.13_fd6cc0e",
            "device_name": "Huddle Room VB1",
            "room": "Huddle 2",
            "floor": "3",
            "building": "North",
            "max_occupancy": "6",
            "bluetooth_enabled": True,
            "wifi_enabled": True,
            "ethernet_enabled": True,
            "hdmi_enabled": False,
            "ethernet_dhcp": True,
            "ethernet_static_ip": "0.0.0.0",
            "ethernet_state": "online",
            "ethernet_mac": "4c:87:5d:12:34:56",
            "wifi_dhcp": True,
            "wifi_static_ip": "0.0.0.0",
            "wifi_mac": "4c:87:5d:12:34:57",
            "wifi_state": "idle",
            "reboot_count": 0,
        },
        "delays": {"command_response": 0.02},
        "controls": [
            {"type": "toggle", "key": "mic_mute", "label": "Microphone mute (the bar's button)"},
            {"type": "toggle", "key": "speaker_mute", "label": "Loudspeaker mute"},
            {"type": "slider", "key": "volume", "label": "Volume", "min": 0, "max": 100, "step": 1},
            {"type": "toggle", "key": "usb_connected", "label": "Computer on USB"},
            {"type": "toggle", "key": "usb_in_call", "label": "In a call (USB)"},
            {"type": "select", "key": "camera_state", "label": "Camera",
             "options": ["active", "inactive", "upgrading"]},
            {"type": "toggle", "key": "autoframing", "label": "Autoframing"},
            {"type": "indicator", "key": "camera_pan", "label": "Pan"},
            {"type": "indicator", "key": "camera_tilt", "label": "Tilt"},
            {"type": "indicator", "key": "camera_zoom", "label": "Zoom"},
            {"type": "slider", "key": "people_count", "label": "People counted (VB1)", "min": 0, "max": 99, "step": 1},
            {"type": "toggle", "key": "gpi_mute", "label": "GPI mute input (VB1)"},
            {"type": "toggle", "key": "bluetooth_connected", "label": "Bluetooth device connected"},
            {"type": "toggle", "key": "bluetooth_streaming", "label": "Bluetooth streaming"},
            {"type": "toggle", "key": "bluetooth_in_call", "label": "In a call (Bluetooth)"},
            {"type": "indicator", "key": "bluetooth_pairing", "label": "Bluetooth pairing"},
            {"type": "toggle", "key": "ethernet_enabled", "label": "Ethernet enabled"},
            {"type": "toggle", "key": "wifi_enabled", "label": "Wi-Fi enabled"},
            {"type": "indicator", "key": "reboot_count", "label": "Reboots"},
        ],
        "error_modes": {
            "communication_timeout": {
                "description": "Bar stops answering",
                "behavior": "no_response",
            },
            "password_changed": {
                "description": "API username or password changed on the bar: every request is refused (HTTP 401)",
                "behavior": "custom",
            },
            "access_denied": {
                "description": "Every property answers access denied (0xe006)",
                "behavior": "custom",
            },
            "api_off": {
                "description": "API access turned off: every request answers 404",
                "behavior": "custom",
            },
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        cfg = self.config or {}
        self._vb1 = str(cfg.get("model", "VB1") or "VB1").strip().upper() != "VB-S"
        if not self._vb1:
            # The SNMP guide gives "vb1" as the VB1's model; the VB-S's text is
            # not documented.
            super().set_state("model", "vb-s")
            super().set_state("device_name", "Huddle Room VB-S")
            super().set_state("wifi_state", "online")
        via = str(cfg.get("connected_via", "") or "").strip().lower()
        self._via = via if via in ("ethernet", "wifi") else ("ethernet" if self._vb1 else "wifi")
        self._user = str(cfg.get("username", "") or "")
        self._secret = str(cfg.get("password", "") or "")
        self._downtime = float(cfg.get("reboot_downtime", 5.0) or 0)
        self._pairing_timeout = float(cfg.get("pairing_timeout", 180) or 0)
        self._pairing_handle: asyncio.TimerHandle | None = None
        self._interface_down = False
        self.requests: list[str] = []

    # ── Simulator UI changes ──

    def set_state(self, key: str, value: Any) -> None:
        super().set_state(key, value)
        if key == "people_count":
            super().set_state("people_present", int(value or 0) > 0)
        if key in ("ethernet_enabled", "wifi_enabled") and hasattr(self, "_via"):
            self._interface_changed()

    def _interface_changed(self) -> None:
        """The bar answers nothing while the interface it is reached through
        is off."""
        key = "ethernet_enabled" if self._via == "ethernet" else "wifi_enabled"
        down = not bool(self.state.get(key, True))
        if down and not self._interface_down:
            self._interface_down = True
            self.inject_error("communication_timeout")
        elif not down and self._interface_down:
            self._interface_down = False
            self.clear_error("communication_timeout")

    # ── Authentication ──

    def _authorized(self, headers: dict[str, str]) -> bool:
        auth = next((v for k, v in headers.items() if k.lower() == "authorization"), "")
        if not auth.startswith("Basic "):
            return False
        try:
            user, _, password = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        if self._user or self._secret:
            return user == self._user and password == self._secret
        return bool(user) and bool(password) and "invalid" not in (user, password)

    # ── Dispatch ──

    def handle_request(self, method: str, path: str, headers: dict[str, str], body: str):
        if "api_off" in self.active_errors:
            return 404, "Not Found"
        if "password_changed" in self.active_errors or not self._authorized(headers):
            return 401, "Unauthorized", {"WWW-Authenticate": 'Basic realm="Videobar"'}
        parts = urlsplit(path)
        if parts.path.rstrip("/") != "/api":
            return 404, "Not Found"
        query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        self.requests.append(f"{method} {path}" + (f" | {body}" if body else ""))
        if method == "GET" and "query" in query:
            return 200, self._get(query["query"].split(","))
        if method == "GET" and "delete" in query:
            return 200, self._delete(query["delete"].split(","))
        if method in ("PUT", "POST"):
            data = self._data(headers, body)
            if data is None:
                return 400, {"status": "error", "code": "0xe005"}
            return 200, self._put(data) if method == "PUT" else self._post(data)
        return 400, {"status": "error", "code": "0xe005"}

    def _data(self, headers: dict[str, str], body: str) -> dict[str, str] | None:
        """The data object, from a form field or a JSON body."""
        content_type = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
        raw: Any = None
        if "json" in content_type:
            try:
                raw = json.loads(body).get("data")
            except (ValueError, AttributeError):
                return None
        else:
            values = parse_qs(body, keep_blank_values=True).get("data")
            raw = values[-1] if values else None
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return None
        if not isinstance(raw, dict):
            return None
        return {str(k): "" if v is None else str(v) for k, v in raw.items()}

    def _prop(self, pid: str) -> tuple[str, str, str, str | None, bool] | None:
        spec = PROPS.get(_norm(pid))
        if spec is None or (spec[4] and not self._vb1):
            return None
        return spec

    def _denied(self) -> bool:
        return "access_denied" in self.active_errors

    def _wire(self, key: str, kind: str) -> str:
        value = self.state.get(key)
        if kind == "bool":
            return "1" if value else "0"
        return "" if value is None else str(value)

    def _get(self, ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for pid in ids:
            pid = pid.strip()
            if not pid:
                continue
            spec = self._prop(pid)
            if self._denied():
                out[pid] = _error("0xe006")
            elif spec is None:
                out[pid] = _error("0xe002")
            elif "r" not in spec[2]:
                out[pid] = _error("0xe004")
            else:
                out[pid] = {"status": "success", "value": self._wire(spec[0], spec[1])}
        return out

    def _delete(self, ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for pid in ids:
            pid = pid.strip()
            spec = self._prop(pid)
            if self._denied():
                out[pid] = _error("0xe006")
            elif spec is None:
                out[pid] = _error("0xe002")
            elif "d" not in spec[2] or spec[0] not in DEFAULTS:
                out[pid] = _error("0xe004")
            else:
                self.set_state(spec[0], DEFAULTS[spec[0]])
                out[pid] = _ok()
        return out

    def _put(self, data: dict[str, str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for pid, text in data.items():
            spec = self._prop(pid)
            if self._denied():
                out[pid] = _error("0xe006")
                continue
            if spec is None:
                out[pid] = _error("0xe002")
                continue
            key, kind, actions, pattern, _ = spec
            if "w" not in actions:
                out[pid] = _error("0xe004")
                continue
            if pattern and not re.fullmatch(pattern, text):
                out[pid] = _error("0xe003")
                continue
            if text == self._wire(key, kind):
                out[pid] = _ok("0xe001")
                continue
            value: Any = text
            if kind == "bool":
                value = text == "1"
            elif kind == "int":
                value = int(text)
            self.set_state(key, value)
            out[pid] = _ok()
            self._after_put(key, value)
        return out

    def _after_put(self, key: str, value: Any) -> None:
        if key == "bluetooth_pairing":
            self._arm_pairing(bool(value))
        elif key == "bluetooth_enabled":
            self.set_state("bluetooth_on", bool(value))
            if not value:
                self._bluetooth_dropped()
        elif key == "device_name":
            self._reboot()

    def _post(self, data: dict[str, str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for pid, text in data.items():
            spec = self._prop(pid)
            if self._denied():
                out[pid] = _error("0xe006")
                continue
            if spec is None:
                out[pid] = _error("0xe002")
                continue
            if "p" not in spec[2]:
                out[pid] = _error("0xe004")
                continue
            if spec[3] and not re.fullmatch(spec[3], text):
                out[pid] = _error("0xe003")
                continue
            out[pid] = _ok()
            self._perform(_norm(pid))
        return out

    def _step(self, key: str, delta: int, low: int, high: int) -> None:
        self.set_state(key, max(low, min(high, int(self.state.get(key, 0)) + delta)))

    def _perform(self, pid: str) -> None:
        if pid == "15":
            self.set_state("mic_mute", not self.state.get("mic_mute"))
        elif pid == "34":
            self.set_state("speaker_mute", not self.state.get("speaker_mute"))
        elif pid in ("4", "5"):
            self._step("volume", 1 if pid == "4" else -1, 0, 100)
            self.set_state("speaker_mute", False)
        elif pid in ("9", "A"):
            self._step("camera_zoom", 1 if pid == "9" else -1, 1, 10)
        elif pid in ("B", "C"):
            self._step("camera_pan", -1 if pid == "B" else 1, -10, 10)
        elif pid in ("D", "E"):
            self._step("camera_tilt", 1 if pid == "D" else -1, -10, 10)
        elif pid == "F":
            position = str(self.state.get(PRESET_KEYS.get(int(self.state.get("active_preset", 1)), ""), ""))
            parts = position.split()
            if len(parts) == 3:
                pan, tilt, zoom = (int(p) for p in parts)
                self.set_state("camera_pan", pan)
                self.set_state("camera_tilt", tilt)
                self.set_state("camera_zoom", zoom)
        elif pid in ACTIONS_SAVE:
            key, _number = ACTIONS_SAVE[pid]
            self.set_state(key, " ".join(str(self.state.get(k, 0)) for k in ("camera_pan", "camera_tilt", "camera_zoom")))
        elif pid == "C6":
            pairing = not self.state.get("bluetooth_pairing")
            self.set_state("bluetooth_pairing", pairing)
            self._arm_pairing(pairing)
        elif pid == "E4":
            self._bluetooth_dropped()
        elif pid == "32":
            self._reboot()

    def _bluetooth_dropped(self) -> None:
        for key in ("bluetooth_connected", "bluetooth_streaming", "bluetooth_in_call"):
            self.set_state(key, False)

    def _arm_pairing(self, on: bool) -> None:
        if self._pairing_handle is not None:
            self._pairing_handle.cancel()
            self._pairing_handle = None
        if on and self._pairing_timeout > 0:
            self._pairing_handle = asyncio.get_running_loop().call_later(
                self._pairing_timeout, self.set_state, "bluetooth_pairing", False,
            )

    def _reboot(self) -> None:
        """The bar answers nothing while it starts again; the camera starts on
        Home."""
        self.set_state("reboot_count", int(self.state.get("reboot_count", 0)) + 1)
        self.set_state("active_preset", 1)
        if self._downtime > 0:
            self.inject_error("communication_timeout")
            asyncio.get_running_loop().call_later(self._downtime, self._rebooted)

    def _rebooted(self) -> None:
        if not self._interface_down:
            self.clear_error("communication_timeout")
