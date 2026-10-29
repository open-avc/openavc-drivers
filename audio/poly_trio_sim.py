"""
Poly Trio (Poly Voice Software REST API) — Simulator.

Simulates a Trio C60 or Trio 8300's REST API as the REST API Reference Manual
(July 2026) describes it: device information (v2, and the deprecated v1), the
call list (callStatus v2), communication info (the phone's mute), line
information (v2), the display's power status, poll-for-status, the transfer
type, and the call control, key, DTMF, configuration, restart, reboot and
reset requests. Every reply carries the phone's result code in ``Status``
("2000" for a success, a 4xxx or 5xxx code otherwise).

Calls: Dial places an outgoing call (connected at once); the "Ring an incoming
call" toggle rings one in, and "Far end hangs up" ends every call. A call has a
handle, ``Ringing`` "1" while it rings in, and a ``*`` on its
``UIAppearanceIndex`` while it is the active call. The manual gives no word
for a ringing or held call's CallState ("Connected" is the only one it shows;
sipStatus shows "Hold"), so this simulator uses "Offering" and "Hold"; the
driver reads neither word.

Serves HTTPS with a self-signed certificate (``"tls": True``), as the manual
says the API should be reached.

Authentication: user ``Polycom``. ``auth`` "digest" (default: PVOS 9.2.0 and
later) challenges with Digest, MD5 unless ``digest_algorithm`` is "SHA-256";
``auth`` "basic" challenges with Basic only (an earlier PVOS). With a
``password`` configured, only that password is accepted; with none, any
password but ``invalid`` is, which is how the driver's refused-password path is
exercised without one.

Error modes: ``communication_timeout`` (answers nothing), ``password_changed``
(every request is refused 401), ``busy`` (every request is answered 403, the
phone's answer to a request sent while it handles another), ``default_password``
(every request answers code 4010) and ``rest_api_off`` (every request 404).

Configuration: ``model`` ("Trio C60" or "Trio 8300", default Trio C60),
``password``, ``auth``, ``digest_algorithm``, ``reboot_downtime`` (seconds the
phone answers nothing after a reboot or restart; default 3).

Driver: poly_trio
Transport: http
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
from typing import Any
from urllib.parse import parse_qs, urlsplit

from openavc.simulator.http_simulator import HTTPSimulator

USERNAME = "Polycom"
REALM = "Poly Simulator"

KEYS_C60 = {"MicMute", "VolDown", "VolUp"}
KEYS_8300 = (
    {f"SoftKey{n}" for n in range(1, 6)}
    | {f"Line{n}" for n in range(1, 13)}
    | {"ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Select", "Delete"}
    | {f"Dialpad{n}" for n in range(10)}
    | {"DialpadStar", "DialpadPound", "Handsfree", "Headset", "MicMute",
       "Transfer", "VolDown", "VolUp", "Menu", "Redial", "Applications"}
)

_HASHES = {"MD5": hashlib.md5, "SHA-256": hashlib.sha256}


def _ok(data: Any = None) -> tuple[int, dict]:
    body: dict[str, Any] = {"Status": "2000"}
    if data is not None:
        body["data"] = data
    return 200, body


def _fail(code: str, http: int = 200) -> tuple[int, dict]:
    return http, {"Status": code}


class PolyTrioSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "poly_trio",
        "name": "Poly Trio Simulator",
        "category": "audio",
        "transport": "http",
        "default_port": 443,
        "tls": True,
        "initial_state": {
            "model": "Trio C60",
            "firmware": "9.3.0.2026",
            "serial": "8G21480A1B2C",
            "mac": "64167F2A0B1C",
            "system_name": "Board Room Trio",
            "provider": "Generic",
            "mute": False,
            "registered": True,
            "do_not_disturb": False,
            "line_label": "Board Room",
            "line_user": "4100",
            "display": "PowerOn",
            "phone_status": "Idle",
            "transfer_type": "Consultative",
            "incoming_call": False,
            "caller_name": "Front Desk",
            "caller_number": "4001",
            "far_end_hang_up": False,
            "call_count": 0,
            "volume": 5,
            "last_dialed": "",
            "last_dtmf": "",
            "last_key": "",
            "last_transfer": "",
            "restart_count": 0,
        },
        "delays": {"command_response": 0.02},
        "controls": [
            {"type": "toggle", "key": "incoming_call", "label": "Ring an incoming call"},
            {"type": "toggle", "key": "far_end_hang_up", "label": "Far end hangs up"},
            {"type": "indicator", "key": "call_count", "label": "Calls"},
            {"type": "toggle", "key": "mute", "label": "Mute (the phone's own button)"},
            {"type": "toggle", "key": "registered", "label": "Line registered"},
            {"type": "toggle", "key": "do_not_disturb", "label": "Do not disturb"},
            {"type": "select", "key": "display", "label": "Display",
             "options": ["PowerOn", "PowerIdle", "PowerOff", "PowerInCall"]},
            {"type": "select", "key": "phone_status", "label": "Phone status",
             "options": ["Idle", "Active", "Error"]},
            {"type": "indicator", "key": "volume", "label": "Volume (0-10, keys only)"},
            {"type": "indicator", "key": "last_dialed", "label": "Last dialed"},
            {"type": "indicator", "key": "last_dtmf", "label": "DTMF sent"},
            {"type": "indicator", "key": "last_key", "label": "Last key"},
            {"type": "indicator", "key": "restart_count", "label": "Restarts"},
        ],
        "error_modes": {
            "communication_timeout": {
                "description": "Phone stops answering",
                "behavior": "no_response",
            },
            "password_changed": {
                "description": "Admin password changed on the phone: every request is refused (HTTP 401)",
                "behavior": "custom",
            },
            "busy": {
                "description": "Every request is answered 403, as while the phone handles another request",
                "behavior": "custom",
            },
            "default_password": {
                "description": "The phone still has its default admin password: every request answers 4010",
                "behavior": "custom",
            },
            "rest_api_off": {
                "description": "The REST API is turned off: every request answers 404",
                "behavior": "custom",
            },
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        cfg = self.config or {}
        model = str(cfg.get("model", "") or "").strip()
        if model:
            self.set_state("model", model)
        self._password = str(cfg.get("password", "") or "")
        self._auth_mode = "basic" if str(cfg.get("auth", "")).strip().lower() == "basic" else "digest"
        algorithm = str(cfg.get("digest_algorithm", "MD5") or "MD5").strip().upper()
        self._algorithm = "SHA-256" if algorithm in ("SHA-256", "SHA256") else "MD5"
        self._reboot_downtime = float(cfg.get("reboot_downtime", 3.0) or 0)
        self._nonces: set[str] = set()
        self._calls: list[dict[str, Any]] = []
        self._config_values: dict[str, str] = {}
        self.requests: list[str] = []

    # ── Simulator UI toggles ──

    def set_state(self, key: str, value: Any) -> None:
        super().set_state(key, value)
        if key == "incoming_call":
            if value and not any(c["Ringing"] == "1" for c in getattr(self, "_calls", [])):
                self._ring_in()
            elif not value and hasattr(self, "_calls"):
                # The caller gave up before anyone answered.
                self._calls = [c for c in self._calls if c["Ringing"] != "1"]
                self._calls_changed()
        elif key == "far_end_hang_up" and value:
            self._calls = []
            self._calls_changed()
            super().set_state("far_end_hang_up", False)

    # ── Calls ──

    def _new_call(self, kind: str, name: str, number: str, state: str, ringing: bool) -> dict[str, Any]:
        handle = secrets.token_hex(4)
        return {
            "CallHandle": handle,
            "Type": kind,
            "RemotePartyName": name,
            "RemotePartyNumber": number,
            "CallState": state,
            "Protocol": "SIP",
            "StartTime": "2026-10-29T09:00:00",
            "DurationSeconds": "0",
            "LineID": "1",
            "CallSequence": str(len(self._calls) + 1),
            "UIAppearanceIndex": str(len(self._calls) + 1),
            "Ringing": "1" if ringing else "0",
            "Muted": "1" if self.state.get("mute") else "0",
            "Media Direction": "sendrecv",
            "Remote Connection IP": "192.0.2.50",
            "RTPPort": "2262",
            "RTCPPort": "2263",
        }

    def _ring_in(self) -> None:
        self._calls.append(self._new_call(
            "Incoming", str(self.state.get("caller_name", "")),
            str(self.state.get("caller_number", "")), "Offering", True,
        ))
        self._calls_changed()

    def _make_active(self, call: dict[str, Any]) -> None:
        """One active call: any other connected call goes on hold, as a phone
        does when a second call is taken."""
        for other in self._calls:
            if other is not call and other["UIAppearanceIndex"].endswith("*"):
                other["UIAppearanceIndex"] = other["UIAppearanceIndex"].rstrip("*")
                other["CallState"] = "Hold"
        call["CallState"] = "Connected"
        call["Ringing"] = "0"
        if not call["UIAppearanceIndex"].endswith("*"):
            call["UIAppearanceIndex"] += "*"

    def _calls_changed(self) -> None:
        super().set_state("call_count", len(self._calls))
        if not any(c["Ringing"] == "1" for c in self._calls):
            super().set_state("incoming_call", False)

    def _find(self, ref: Any) -> dict[str, Any] | None:
        ref = str(ref or "").strip()
        return next((c for c in self._calls if c["CallHandle"] == ref), None)

    # ── Authentication ──

    def _challenge(self) -> tuple[int, str, dict[str, str]]:
        if self._auth_mode == "basic":
            return 401, "Unauthorized", {"WWW-Authenticate": f'Basic realm="{REALM}"'}
        nonce = secrets.token_hex(16)
        self._nonces.add(nonce)
        return 401, "Unauthorized", {
            "WWW-Authenticate": (
                f'Digest realm="{REALM}", nonce="{nonce}", algorithm={self._algorithm}, qop="auth"'
            ),
        }

    def _password_ok(self, password: str) -> bool:
        if self._password:
            return password == self._password
        return password != "invalid"

    def _authorized(self, method: str, path: str, headers: dict[str, str]) -> bool:
        auth = next((v for k, v in headers.items() if k.lower() == "authorization"), "")
        if self._auth_mode == "basic":
            if not auth.startswith("Basic "):
                return False
            try:
                user, _, password = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
            except (ValueError, UnicodeDecodeError):
                return False
            return user == USERNAME and self._password_ok(password)
        if not auth.startswith("Digest "):
            return False
        fields = {k.lower(): v.strip('"') for k, v in re.findall(r'(\w+)=("[^"]*"|[^,\s]+)', auth[7:])}
        if fields.get("username") != USERNAME or fields.get("nonce") not in self._nonces:
            return False
        hash_fn = _HASHES.get(fields.get("algorithm", "MD5").upper(), hashlib.md5)

        def expected(password: str) -> str:
            ha1 = hash_fn(f"{USERNAME}:{fields.get('realm', '')}:{password}".encode()).hexdigest()
            ha2 = hash_fn(f"{method}:{fields.get('uri', '')}".encode()).hexdigest()
            if fields.get("qop"):
                tail = f"{fields.get('nc', '')}:{fields.get('cnonce', '')}:{fields.get('qop', '')}:"
            else:
                tail = ""
            return hash_fn(f"{ha1}:{fields['nonce']}:{tail}{ha2}".encode()).hexdigest()

        answer = fields.get("response", "")
        if self._password:
            return answer == expected(self._password)
        # No password set: any password is taken except the sentinel.
        return answer != expected("invalid")

    # ── Dispatch ──

    def handle_request(self, method: str, path: str, headers: dict[str, str], body: str):
        parts = urlsplit(path)
        route = parts.path.rstrip("/") or "/"
        query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        if "rest_api_off" in self.active_errors:
            return 404, "Not Found"
        if "password_changed" in self.active_errors or not self._authorized(method, path, headers):
            return self._challenge()
        if "busy" in self.active_errors:
            return _fail("4403", 403)
        if "default_password" in self.active_errors:
            return _fail("4010")
        self.requests.append(f"{method} {route}")
        try:
            payload = json.loads(body) if body else {}
        except ValueError:
            return _fail("4400", 400)
        data = payload.get("data") if isinstance(payload, dict) else None
        return self._route(method, route, query, data)

    def _route(self, method: str, route: str, query: dict[str, str], data: Any):
        if method == "GET":
            getters = {
                "/api/v2/mgmt/device/info": self._device_info,
                "/api/v1/mgmt/device/info": self._device_info_v1,
                "/api/v2/webCallControl/callStatus": lambda: self._call_status(query),
                "/api/v1/mgmt/media/communicationInfo": self._communication_info,
                "/api/v2/mgmt/lineInfo": self._line_info,
                "/api/v1/mgmt/powerStatus": lambda: (200, {"CurrentState": self.state["display"], "Status": "2000"}),
                "/api/v1/mgmt/pollForStatus": self._poll_for_status,
                "/api/v1/mgmt/transferType/get": lambda: _ok({"Type": self.state["transfer_type"]}),
            }
            getter = getters.get(route)
            return getter() if getter else (404, "Not Found")
        if method != "POST":
            return 404, "Not Found"
        data = data if isinstance(data, dict) else {}
        posts = {
            "/api/v1/callctrl/dial": self._dial,
            "/api/v1/callctrl/answerCall": self._answer,
            "/api/v1/callctrl/rejectCall": self._reject,
            "/api/v1/callctrl/ignoreCall": self._reject,
            "/api/v1/callctrl/endCall": self._end,
            "/api/v1/callctrl/holdCall": self._hold,
            "/api/v1/callctrl/resumeCall": self._resume,
            "/api/v1/callctrl/mute": self._mute,
            "/api/v1/callctrl/sendDTMF": self._dtmf,
            "/api/v1/callctrl/transferCall": self._transfer,
            "/api/v1/mgmt/simulateKeyEvent": self._key,
            "/api/v1/mgmt/transferType/set": self._set_transfer_type,
            "/api/v1/mgmt/config/set": self._set_config,
            "/api/v1/mgmt/updateConfiguration": lambda d: _ok({"ProvisioningState": "0"}),
            "/api/v1/mgmt/safeReboot": lambda d: self._restart(),
            "/api/v1/mgmt/safeRestart": lambda d: self._restart(),
            "/api/v1/mgmt/factoryReset": lambda d: _ok(),
        }
        if route.startswith("/api/v1/mgmt/configReset"):
            scope = route[len("/api/v1/mgmt/configReset"):].strip("/")
            return _ok() if scope in ("", "cloud", "local", "web", "device") else (404, "Not Found")
        handler = posts.get(route)
        return handler(data) if handler else (404, "Not Found")

    # ── Reads ──

    def _device_info(self):
        s = self.state
        return _ok({
            "ModelNumber": s["model"],
            "DeviceVendor": "Polycom",
            "DeviceType": "HardwareEndpoint",
            "MACAddress": s["mac"],
            "Firmware": {"Application": s["firmware"], "Updater": "9.3.0.0001", "BootBlock": "3.0.0.0001"},
            "IPAddress": "192.0.2.20",
            "IPStack": "IPv4 Only",
            "PreferredNetwork": "IPv4",
            "UpTime": {"Days": "2", "Hours": "3", "Minutes": "4", "Seconds": "5"},
            "AttachedHardware": {"Camera": "False"},
            "CanApplyShutdownRequest": "True",
            "IntendToShutdown": "False",
            "AppState": "AppStateIdle" if not self._calls else "AppStateCall",
            "ReadyToUse": "True",
            "Provider": s["provider"],
            "SerialNumber": s["serial"],
            "SystemName": s["system_name"],
            "IsBetaSoftware": "False",
        })

    def _device_info_v1(self):
        s = self.state
        # The deprecated reply, as the manual prints it: a trailing space in
        # one key, FirmwareRelease rather than Firmware.Application.
        return _ok({
            "ModelNumber ": s["model"],
            "FirmwareRelease": s["firmware"],
            "DeviceType": "HardwareEndPoint",
            "DeviceVendor": "Polycom",
            "UpTimeSinceLastReboot": "2 days 3 hours 4 minutes 5 seconds",
            "IPV4Address": "192.0.2.20",
            "MACAddress": s["mac"],
        })

    def _call_status(self, query: dict[str, str]):
        calls = self._calls
        if "handle" in query:
            calls = [c for c in calls if c["CallHandle"] == query["handle"]]
        elif "line" in query:
            calls = [c for c in calls if c["LineID"] == query["line"]]
        if not calls:
            return _fail("4007")
        return _ok([dict(c) for c in calls])

    def _communication_info(self):
        kind = "RxTx" if any(c["CallState"] == "Connected" for c in self._calls) else "Idle"
        return _ok({
            "CommunicationType": [kind],
            "FarEndMuteState": [],
            "PhoneMuteState": "True" if self.state.get("mute") else "False",
        })

    def _line_info(self):
        s = self.state
        return _ok([{
            "ID": "1",
            "Label": s["line_label"],
            "Username": s["line_user"],
            "Assigned": "True",
            "RegistrationStatus": "Registered" if s.get("registered") else "Unregistered",
            "Type": "Private",
            "Active": "True" if self._calls else "False",
            "DoNotDisturb": "True" if s.get("do_not_disturb") else "False",
            "CallAppearances": [c["CallHandle"] for c in self._calls],
            "CallServers": [],
            "ForwardingConfig": {"Always": "", "DoNotDisturb": "", "Busy": "", "NoAnswer": ""},
        }])

    def _poll_for_status(self):
        state = str(self.state.get("phone_status", "Idle"))
        detail = {
            "Idle": "2026-10-29T09:00:00",
            "Active": "00:00:42",
            "Error": "All Phone applications are not ready.",
        }.get(state, "")
        return _ok({"State": state, "StateData": detail})

    # ── Call control ──

    def _dial(self, data: dict):
        dest = str(data.get("Dest", "") or "").strip()
        if not dest:
            return _fail("4000")
        if not self.state.get("registered"):
            return _fail("4002")
        call = self._new_call("Outgoing", "", dest, "Connected", False)
        self._calls.append(call)
        self._make_active(call)
        self.set_state("last_dialed", dest)
        self._calls_changed()
        return _ok()

    def _answer(self, data: dict):
        call = self._find(data.get("Ref"))
        if call is None:
            return _fail("4007")
        if call["Ringing"] != "1":
            return _fail("4003")
        self._make_active(call)
        self._calls_changed()
        return _ok()

    def _reject(self, data: dict):
        call = self._find(data.get("Ref"))
        if call is None:
            return _fail("4007")
        if call["Ringing"] != "1":
            return _fail("4003")
        self._calls.remove(call)
        self._calls_changed()
        return _ok()

    def _end(self, data: dict):
        call = self._find(data.get("Ref"))
        if call is None:
            return _fail("4007")
        self._calls.remove(call)
        self._calls_changed()
        return _ok()

    def _hold(self, data: dict):
        call = self._find(data.get("Ref"))
        if call is None:
            return _fail("4007")
        if call["Ringing"] == "1" or call["CallState"] == "Hold":
            return _fail("4003")
        call["CallState"] = "Hold"
        call["UIAppearanceIndex"] = call["UIAppearanceIndex"].rstrip("*")
        return _ok()

    def _resume(self, data: dict):
        call = self._find(data.get("Ref"))
        if call is None:
            return _fail("4007")
        if call["CallState"] != "Hold":
            return _fail("4003")
        self._make_active(call)
        return _ok()

    def _mute(self, data: dict):
        wanted = str(data.get("state", "")).strip()
        if wanted not in ("0", "1"):
            return _fail("4000")
        if not self._calls:
            return _fail("4007")
        if (wanted == "1") == bool(self.state.get("mute")):
            # "an API request is received that doesn't result in a change in
            # mute state" (manual page 70).
            return _fail("4003")
        self.set_state("mute", wanted == "1")
        return _ok()

    def _dtmf(self, data: dict):
        digits = str(data.get("Digits", "") or "")
        if not digits:
            return _fail("4000")
        if not any(c["CallState"] == "Connected" for c in self._calls):
            return _fail("4007")
        self.set_state("last_dtmf", str(self.state.get("last_dtmf", "")) + digits)
        return _ok()

    def _transfer(self, data: dict):
        call = self._find(data.get("Ref"))
        dest = str(data.get("TransferDest", "") or "").strip()
        if not dest:
            return _fail("4000")
        if call is None:
            return _fail("4007")
        self._calls.remove(call)
        self.set_state("last_transfer", dest)
        self._calls_changed()
        return _ok()

    # ── Management ──

    def _key(self, data: dict):
        key = str(data.get("KeyName", "") or "")
        allowed = KEYS_C60 if "C60" in str(self.state.get("model", "")) else KEYS_8300
        if data.get("Type") != "Tap" or key not in allowed:
            return _fail("4000")
        self.set_state("last_key", key)
        if key == "MicMute":
            self.set_state("mute", not self.state.get("mute"))
        elif key == "VolUp":
            self.set_state("volume", min(10, int(self.state.get("volume", 5)) + 1))
        elif key == "VolDown":
            self.set_state("volume", max(0, int(self.state.get("volume", 5)) - 1))
        return _ok()

    def _set_transfer_type(self, data: dict):
        kind = str(data.get("Type", "") or "")
        if kind not in ("Consultative", "Blind", "Safe"):
            return _fail("4000")
        self.set_state("transfer_type", kind)
        return _ok()

    def _set_config(self, data: dict):
        if not data:
            return _fail("4000")
        for name, value in data.items():
            self._config_values[str(name)] = str(value)
        return _ok()

    def _restart(self):
        """safeReboot / safeRestart: every call ends, then the phone answers
        nothing while it starts again."""
        self._calls = []
        self._calls_changed()
        self.set_state("restart_count", int(self.state.get("restart_count", 0)) + 1)
        if self._reboot_downtime > 0:
            self.inject_error("communication_timeout")
            asyncio.get_running_loop().call_later(
                self._reboot_downtime, self.clear_error, "communication_timeout",
            )
        return _ok()
