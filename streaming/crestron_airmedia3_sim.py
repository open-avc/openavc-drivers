"""
Crestron AirMedia Series 3 receiver — Simulator

Serves the receiver's CresNext API over HTTPS on 443 with a self-signed
certificate ("tls": True), so the driver connects here as it does to a
receiver.

The session follows the API's Authentication and Make API Calls topics: GET
/userlogin.html sets TRACKID; a POST of the URL-encoded credentials answers
200 with the session cookies (userstr, userid, iv, tag, AuthByPasswd) and a
CREST-XSRF-TOKEN header; AuthByPasswd changes on every answer; a POST without
the matching X-CREST-XSRF-TOKEN ends the session; a request without a session
answers 403. A password of ``invalid`` is the wrong one, and so is any other
when the simulator's config gives a ``password``.

GET /Device/Longpoll is held until something changes and answers with only the
changed properties (a command, a setting, or a change in the Simulator UI), or
answers ``{}`` after 25 s. The API does not say what its timeout answer looks
like or how long the receiver holds one; ``{}`` and 25 s are this simulator's
choices.

The model comes from the device config (``model``) or the ``model`` state: an
AM-3000-WF answers without wireless conferencing and without an HDMI input, an
AM-3100-WF without an HDMI input, an AM-3200 without AirMedia Connect
adaptors (the manual's feature table). What the receiver answers for an
object its model lacks is not documented; here it is the "UNSUPPORTED
PROPERTY" string the NVX answers. How a presenting AirMedia user appears in
SourcesV2 is not documented either: here each presenting user is a sub-input
of the wireless sources, active while presenting.

Driver: crestron_airmedia3
Transport: http (HTTPS on 443)
"""

import asyncio
import json
import secrets
from urllib.parse import parse_qsl

from openavc.simulator.http_simulator import HTTPSimulator

_FAMILIES = ("AM-3000-WF", "AM-3100-WF", "AM-3200", "AM-3200-WF")
_HAS_CONFERENCING = ("AM-3100-WF", "AM-3200", "AM-3200-WF")
_HAS_HDMI_INPUT = ("AM-3200", "AM-3200-WF")
_HAS_ADAPTORS = ("AM-3000-WF", "AM-3100-WF", "AM-3200-WF")

_LOGIN_MODES = ("Disabled", "Random", "Random8", "Fixed")
_SYSTEM_MODES = ("OptimizedForVideoQuality", "OptimizedForMultiplePresentations", "Custom")
_ROUTING_MODES = ("Auto", "AirMediaOnly", "AutoWithPriority", "Wired")
_UNSUPPORTED = "UNSUPPORTED PROPERTY, CHECK REST API!!"
_HOLD_S = 25.0
_MAX_ADAPTORS = 8

# Simulator UI changes that the receiver would report on a long poll, and the
# object each belongs to.
_CLIENT_KEYS = ("connected_users", "presenting_users")
_CONFERENCING_STATUS_KEYS = {
    "conferencing_status": "ConferencingStatus",
    "peripherals_blocked": "IsPeripheralBlocked",
    "peripherals_blocked_reason": "PeripheralBlockedReason",
    "mic_detected": "IsMicDetected", "mic_in_use": "IsMicInUse", "mic_model": "MicModel",
    "speaker_detected": "IsSpeakerDetected", "speaker_in_use": "IsSpeakerInUse",
    "speaker_model": "SpeakerModel",
    "camera_detected": "IsCameraDetected", "camera_in_use": "IsCameraInUse",
    "camera_model": "CameraModel",
}
_CONFERENCING_CONFIG_KEYS = {
    "conferencing_enabled": "IsEnabled",
    "peripheral_volume": "PeripheralVolume",
    "peripheral_mute": "PeripheralMute",
    "peripheral_volume_supported": "IsPeripheralVolumeSupported",
}
_AIRMEDIA_KEYS = {
    "login_code_mode": "LoginCodeMode",
    "active_login_code_mode": "ActiveLoginCodeMode",
    "login_code": "LoginCode",
    "active_login_code": "ActiveLoginCode",
    "show_login_code": "ShowLoginCode",
    "system_mode": "SystemMode",
    "canvas_enabled": "IsCanvasEnabled",
    "airmedia_discovery": "IsDiscoveryEnabled",
    "inactivity_timeout": "InactivityTimeout",
}


def _merge(into: dict, change: dict) -> None:
    for key, value in change.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            _merge(into[key], value)
        else:
            into[key] = value


def _adaptor_id(n: int) -> str:
    return f"3eac4f4c-228b-3e02-a621-b82764ab30{n:02d}"


class CrestronAirMedia3Simulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "crestron_airmedia3",
        "name": "Crestron AirMedia Series 3 Simulator",
        "category": "streaming",
        "transport": "http",
        "default_port": 443,
        "tls": True,
        "initial_state": {
            "model": "AM-3200-WF",
            "serial_number": "2234NEJ01234",
            "firmware": "3.0800.0157",
            "device_name": "AM-3200-WF-SIM",
            "mac_address": "c4:42:68:12:34:56",
            "reboot_reason": "poweron",
            "presentation_status": "Idle",
            "connected_users": 0,
            "presenting_users": 0,
            "wifi_users_connected": False,
            "active_source": "None",
            "login_code_mode": "Random",
            "active_login_code_mode": "Random",
            "login_code": "12345678",
            "active_login_code": "12345678",
            "show_login_code": True,
            "system_mode": "OptimizedForMultiplePresentations",
            "routing_mode": "Auto",
            "miracast_enabled": True,
            "canvas_enabled": True,
            "airmedia_discovery": True,
            "inactivity_timeout": 45,
            "display_connected": True,
            "output_resolution": "3840x2160@30",
            "output_disabled": False,
            "hdmi_input_sync": False,
            "conferencing_enabled": True,
            "conferencing_status": "Available",
            "peripheral_volume": 50,
            "peripheral_mute": False,
            "peripheral_volume_supported": True,
            "peripherals_blocked": False,
            "peripherals_blocked_reason": "",
            "mic_detected": True,
            "mic_in_use": False,
            "mic_model": "Speak 750",
            "speaker_detected": True,
            "speaker_in_use": False,
            "speaker_model": "Speak 750",
            "camera_detected": True,
            "camera_in_use": False,
            "camera_model": "PanaCast 50",
            "pairing_status": "Not_Started",
            "adaptor_count": 2,
            "adaptor_sharing": 0,
            "occupancy": "Vacant",
        },
        "delays": {"command_response": 0.02},
        "error_modes": {
            "wrong_password": {
                "description": (
                    "The administrator password was changed on the receiver "
                    "(the open session ends and every sign-in is refused, HTTP 403)"
                ),
                "behavior": "custom",
            },
            "ip_blocked": {
                "description": (
                    "The receiver has blocked this address after failed sign-ins "
                    "(HTTP 403 \"Your IP address is blocked\")"
                ),
                "behavior": "custom",
            },
            "session_timeout": {
                "description": "The session times out once (the next request answers 403)",
                "behavior": "custom",
            },
            "no_admin_account": {
                "description": "A new receiver nobody has signed in to: the sign-in page asks to create an administrator",
                "behavior": "custom",
            },
        },
        "controls": [
            {"type": "indicator", "key": "model", "label": "Model"},
            {"type": "slider", "key": "connected_users", "label": "Connected Users",
             "min": 0, "max": 30, "step": 1},
            {"type": "slider", "key": "presenting_users", "label": "Presenting Users",
             "min": 0, "max": 4, "step": 1},
            {"type": "indicator", "key": "presentation_status", "label": "Presentation"},
            {"type": "select", "key": "active_source", "label": "Source on the Display",
             "options": ["None", "HDMI", "AirMedia", "Adaptor"]},
            {"type": "toggle", "key": "display_connected", "label": "Display Connected"},
            {"type": "toggle", "key": "output_disabled", "label": "HDMI Output Off"},
            {"type": "toggle", "key": "hdmi_input_sync", "label": "HDMI Input Signal"},
            {"type": "slider", "key": "peripheral_volume", "label": "USB Speaker Volume",
             "min": 0, "max": 100, "step": 1},
            {"type": "toggle", "key": "peripheral_mute", "label": "USB Speaker Mute"},
            {"type": "toggle", "key": "camera_in_use", "label": "Camera In Use"},
            {"type": "slider", "key": "adaptor_count", "label": "Paired Adaptors",
             "min": 0, "max": 8, "step": 1},
            {"type": "slider", "key": "adaptor_sharing", "label": "Adaptor Sharing (0 = none)",
             "min": 0, "max": 8, "step": 1},
            {"type": "select", "key": "occupancy", "label": "Occupancy",
             "options": ["Occupied", "Vacant"]},
        ],
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        picked = str((self.config or {}).get("model") or "").strip().upper()
        if picked in _FAMILIES:
            # The model picked for the device plays here.
            super().set_state("model", picked)
            super().set_state("device_name", f"{picked}-SIM")
        self._session: dict | None = None
        self._trackid = ""
        self._pending: dict = {}
        self._waiters: set[asyncio.Event] = set()
        self._timers: list = []

    # ── Model ──

    def _family(self) -> str:
        model = str(self.get_state("model", "AM-3200-WF") or "").strip().upper()
        if model.endswith("-I"):
            model = model[:-2]
        return "AM-3200" if model == "AM-3200-GV" else model

    def _has(self, models) -> bool:
        return self._family() in models

    # ── Session ──

    @staticmethod
    def _cookies(headers: dict) -> dict:
        raw = ""
        for name, value in headers.items():
            if name.lower() == "cookie":
                raw = value
                break
        jar = {}
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name:
                jar[name] = value
        return jar

    @staticmethod
    def _header(headers: dict, wanted: str) -> str:
        for name, value in headers.items():
            if name.lower() == wanted.lower():
                return value
        return ""

    def _refusing(self) -> bool:
        return bool({"wrong_password", "ip_blocked"} & set(self.active_errors))

    def _session_ok(self, headers: dict) -> bool:
        if self._refusing():
            self._session = None
        if "session_timeout" in self.active_errors and self._session is not None:
            # One timeout: this request is refused, the next sign-in works.
            self._session = None
            self.clear_error("session_timeout")
            return False
        session = self._session
        if session is None:
            return False
        jar = self._cookies(headers)
        return all(jar.get(name) == session[name] for name in ("userid", "iv", "tag", "userstr"))

    def _rotate(self) -> list:
        """AuthByPasswd changes on every answer to a signed-in request."""
        if self._session is None:
            return []
        self._session["AuthByPasswd"] = secrets.token_hex(8)
        return [("AuthByPasswd", self._session["AuthByPasswd"])]

    def _sign_in(self, body: str):
        if "ip_blocked" in self.active_errors:
            self._session = None
            return 403, "<html><body><p>Your IP address is blocked.</p></body></html>", {}, []
        fields = dict(parse_qsl(body, keep_blank_values=True))
        password = fields.get("passwd", "")
        expected = str((self.config or {}).get("password", "") or "")
        if (
            "wrong_password" in self.active_errors
            or not fields.get("login")
            or password == "invalid"
            or (expected and password != expected)
        ):
            self._session = None
            return 403, "Forbidden", {}, []
        self._session = {
            "TRACKID": self._trackid or secrets.token_hex(8),
            "userstr": secrets.token_hex(8),
            "userid": secrets.token_hex(8),
            "iv": secrets.token_hex(8),
            "tag": secrets.token_hex(8),
            "AuthByPasswd": secrets.token_hex(8),
            "xsrf": secrets.token_hex(16),
        }
        self._pending = {}
        cookies = [(name, self._session[name]) for name in
                   ("TRACKID", "userstr", "userid", "iv", "tag", "AuthByPasswd")]
        return 200, "", {"CREST-XSRF-TOKEN": self._session["xsrf"]}, cookies

    # ── Requests ──

    def dispatch(self, method: str, path: str, headers: dict, body: str):
        """One request: (status, body, response headers, cookies to set)."""
        path = path.split("?", 1)[0]
        if path == "/userlogin.html":
            if method == "POST":
                return self._sign_in(body)
            if "no_admin_account" in self.active_errors:
                return 302, "", {"Location": "/createUser.html"}, []
            self._trackid = secrets.token_hex(8)
            return 200, "<html><body>Sign in</body></html>", {}, [("TRACKID", self._trackid)]
        if path == "/logout":
            self._session = None
            return 200, "<html><body>Signed out</body></html>", {}, []
        if not path.startswith("/Device"):
            return 404, {"error": "Not Found"}, {}, []
        if not self._session_ok(headers):
            return 403, "Forbidden", {}, []
        if method == "POST" and path == "/Device":
            if self._header(headers, "X-CREST-XSRF-TOKEN") != self._session["xsrf"]:
                # Make API Calls: a POST without the token invalidates every cookie.
                self._session = None
                return 403, "Forbidden", {}, []
            return 200, self._post(body), {}, self._rotate()
        if method == "GET":
            return 200, self._get(path), {}, self._rotate()
        return 405, {"error": "Method Not Allowed"}, {}, []

    def handle_request(self, method, path, headers, body):
        """The platform's sync entry point. respond_http below is what runs:
        it sets several cookies and holds the long poll."""
        status, payload, response_headers, _cookies = self.dispatch(method, path, headers, body)
        return status, payload, response_headers

    async def longpoll(self, headers: dict):
        """GET /Device/Longpoll: the properties changed since the last one,
        as soon as there are any, or ``{}`` after the hold."""
        if not self._session_ok(headers):
            return 403, "Forbidden", {}, []
        if not self._pending:
            event = asyncio.Event()
            self._waiters.add(event)
            try:
                await asyncio.wait_for(event.wait(), _HOLD_S)
            except asyncio.TimeoutError:
                pass
            finally:
                self._waiters.discard(event)
        if self._session is None:
            return 403, "Forbidden", {}, []
        changes, self._pending = self._pending, {}
        payload = {"Device": changes} if changes else {}
        return 200, payload, {}, self._rotate()

    async def respond_http(self, request, method, path, headers, body):
        from aiohttp import web

        try:
            if method == "GET" and path.split("?", 1)[0] == "/Device/Longpoll":
                status, payload, response_headers, cookies = await self.longpoll(headers)
                if self._silenced:
                    await asyncio.sleep(30)
                    return web.Response(status=504, text="Gateway Timeout")
            else:
                status, payload, response_headers, cookies = self.dispatch(
                    method, path, headers, body,
                )
        except Exception:  # noqa: BLE001 - a simulator bug answers 500, never hangs the driver
            status, payload, response_headers, cookies = 500, "Internal simulator error", {}, []
        if isinstance(payload, dict):
            text, content_type = json.dumps(payload), "application/json"
        else:
            text, content_type = str(payload), "text/html"
        self.log_protocol("out", f"{status} | {text[:200]}")
        response = web.Response(
            status=status, text=text, content_type=content_type,
            headers=response_headers or None,
        )
        for name, value in cookies:
            response.set_cookie(name, value, path="/", secure=True, httponly=True)
        return response

    # ── Change reporting ──

    def _report(self, change: dict) -> None:
        _merge(self._pending, change)
        for event in list(self._waiters):
            event.set()

    def set_state(self, key, value):
        previous = self.get_state(key)
        super().set_state(key, value)
        if previous == value:
            return
        if key in _CLIENT_KEYS:
            self._settle_users()
        change = self._change_for(key)
        if change:
            self._report(change)

    def _change_for(self, key: str) -> dict | None:
        """The partial Device tree a long poll answers for a changed key."""
        if key in _CLIENT_KEYS or key in ("presentation_status", "wifi_users_connected"):
            return {"AirMedia": {"ClientData": self._client_data()}}
        if key in _AIRMEDIA_KEYS:
            return {"AirMedia": {_AIRMEDIA_KEYS[key]: self.get_state(key)}}
        if key == "miracast_enabled":
            return {"AirMedia": {"Miracast": {"IsEnabled": bool(self.get_state(key))}}}
        if key in _CONFERENCING_CONFIG_KEYS and self._has(_HAS_CONFERENCING):
            return {"AirMedia": {"WirelessConferencing": {"Configuration": {
                _CONFERENCING_CONFIG_KEYS[key]: self.get_state(key),
            }}}}
        if key in _CONFERENCING_STATUS_KEYS and self._has(_HAS_CONFERENCING):
            return {"AirMedia": {"WirelessConferencing": {"Status": {
                _CONFERENCING_STATUS_KEYS[key]: self.get_state(key),
            }}}}
        if key == "routing_mode":
            return {"SourceSelectionConfiguration": {"RoutingMode": self.get_state(key)}}
        if key in ("active_source", "adaptor_count", "adaptor_sharing", "hdmi_input_sync"):
            change = {"SourceSelectionConfiguration": {"SourcesV2": self._sources()}}
            if key == "hdmi_input_sync" and self._has(_HAS_HDMI_INPUT):
                change["AvioV2"] = {"Inputs": {"Input1": {"InputInfo": {"Ports": {
                    "Port1": {"IsSyncDetected": bool(self.get_state(key))},
                }}}}}
            if key in ("adaptor_count", "adaptor_sharing") and self._has(_HAS_ADAPTORS):
                change["AirMediaTx3"] = {"Tx3DevicesMap": self._adaptor_map()}
            return change
        if key in ("display_connected", "output_disabled"):
            field = "IsSinkConnected" if key == "display_connected" else None
            port = ({field: bool(self.get_state(key))} if field
                    else {"Digital": {"IsOutputDisabled": bool(self.get_state(key))}})
            return {"AvioV2": {"Outputs": {"Output1": {"OutputInfo": {"Ports": {"Port1": port}}}}}}
        if key == "pairing_status" and self._has(_HAS_ADAPTORS):
            return {"AirMediaTx3": {"PairingStatus": self.get_state(key)}}
        if key == "occupancy":
            return {"App": {"Config": {"OccupancySensor": {"Sensors": self._sensors()}}}}
        return None

    def _settle_users(self) -> None:
        """Keep the user counts and the overall status consistent, as the
        receiver reports them, and apply a login code change once nobody is
        connected (AirMedia object, ActiveLoginCode)."""
        users = max(0, int(self.get_state("connected_users", 0) or 0))
        presenting = max(0, min(int(self.get_state("presenting_users", 0) or 0), users))
        if presenting != self.get_state("presenting_users"):
            super().set_state("presenting_users", presenting)
        status = "Presenting" if presenting else ("Active" if users else "Idle")
        super().set_state("presentation_status", status)
        super().set_state("wifi_users_connected", users > 0)
        if users == 0:
            self._apply_login_code_now()

    def _apply_login_code_now(self) -> None:
        for pending, active in (("login_code", "active_login_code"),
                                ("login_code_mode", "active_login_code_mode")):
            if self.get_state(active) != self.get_state(pending):
                super().set_state(active, self.get_state(pending))
                self._report({"AirMedia": {_AIRMEDIA_KEYS[active]: self.get_state(pending)}})

    # ── Objects ──

    def _get(self, path: str) -> dict:
        parts = [p for p in path.split("/") if p][1:]  # after "Device"
        if not parts:
            return {"Device": {}}
        name = parts[0]
        builders = {
            "DeviceInfo": self._device_info,
            "AirMedia": self._airmedia,
            "AirMediaTx3": self._tx3,
            "SourceSelectionConfiguration": self._source_selection,
            "AvioV2": self._avio,
            "App": self._app_object,
            "DeviceOperations": lambda: {"UpgradeStatus": "Puf Upgrade Not Initiated",
                                         "Version": "2.7.0"},
        }
        build = builders.get(name)
        obj = build() if build else _UNSUPPORTED
        if obj is None:
            obj = _UNSUPPORTED
        # A sub-path answers the same tree, cut down to that branch.
        node = obj
        for part in parts[1:]:
            if not isinstance(node, dict) or part not in node:
                return {"Device": {name: _UNSUPPORTED}}
            node = node[part]
        tree = node
        for part in reversed(parts):
            tree = {part: tree}
        return {"Device": tree}

    def _device_info(self) -> dict:
        model = self.get_state("model", "AM-3200-WF")
        return {
            "Model": model,
            "Category": "AirMedia",
            "Manufacturer": "Crestron",
            "ModelId": "0x7A07",
            "DeviceId": self.get_state("mac_address", ""),
            "SerialNumber": self.get_state("serial_number", ""),
            "Name": self.get_state("device_name", ""),
            "DeviceVersion": self.get_state("firmware", ""),
            "PufVersion": self.get_state("firmware", ""),
            "BuildDate": "Nov 20 2025",
            "MacAddress": self.get_state("mac_address", ""),
            "RebootReason": self.get_state("reboot_reason", "poweron"),
            "Version": "2.3.1",
        }

    def _client_data(self) -> dict:
        users = int(self.get_state("connected_users", 0) or 0)
        presenting = int(self.get_state("presenting_users", 0) or 0)
        clients = {}
        for n in range(1, users + 1):
            clients[f"Client{n}"] = {
                "SessionType": "AirMedia",
                "PeripheralsInUse": False,
                "IsClientActive": n <= presenting,
                "Platform": "Windows",
                "SenderVersion": "3.8.0",
                "OsVersion": "11",
                "Manufacturer": "Dell",
                "Model": "Latitude",
                "DeviceId": f"client-{n}",
                "IpAddress": f"192.168.1.{100 + n}",
                "UserName": f"Presenter {n}",
                "Language": "en-US",
            }
        return {
            "Status": self.get_state("presentation_status", "Idle"),
            "TotalUsers": users,
            "IsAnyWifiUserConnected": bool(self.get_state("wifi_users_connected", False)),
            "ConnectedClients": clients,
        }

    def _airmedia(self) -> dict:
        obj = {key: self.get_state(state) for state, key in _AIRMEDIA_KEYS.items()}
        obj.update({
            "IsEnabled": True,
            "Miracast": {
                "IsEnabled": bool(self.get_state("miracast_enabled", True)),
                "DefaultWindowsExperience": "Miracast",
                "IsWifiDirectModeEnabled": True,
                "IsMsMiceModeEnabled": True,
                "PreferWifiDirect": True,
            },
            "ClientData": self._client_data(),
            "SystemModeSupported": list(_SYSTEM_MODES),
            "Version": "2.9.3",
        })
        if self._has(_HAS_CONFERENCING):
            obj["WirelessConferencing"] = {
                "Configuration": {key: self.get_state(state)
                                  for state, key in _CONFERENCING_CONFIG_KEYS.items()},
                "Status": {key: self.get_state(state)
                           for state, key in _CONFERENCING_STATUS_KEYS.items()},
            }
        return obj

    def _adaptor_map(self) -> dict:
        count = max(0, min(int(self.get_state("adaptor_count", 0) or 0), _MAX_ADAPTORS))
        sharing = int(self.get_state("adaptor_sharing", 0) or 0)
        adaptors = {}
        for n in range(1, count + 1):
            adaptors[_adaptor_id(n)] = {
                "FriendlyName": f"AirMedia TX3-{n}",
                "Model": "AM-TX3-100",
                "Name": f"AM-TX3-{n:03d}",
                "SerialNumber": f"2233GSP0{n:04d}\n",
                "MacAddress": f"c4:42:68:17:a8:{n:02x}",
                "FirmwareVersion": "2.0.0.2",
                "SignalStrengthPercentage": max(40, 99 - 7 * n),
                "SignalStrengthDbm": -40 - 3 * n,
                "ConnectionStatus": "Online_Sharing" if n == sharing else "Online",
                "ScreenShareStatus": "Playing" if n == sharing else "Stopped",
                "FirmwareUpgradeStatus": "None",
                "SourceConnectionStatus": "ReadyToShare",
                "AutoUpdateStatus": "None",
            }
        return adaptors

    def _tx3(self) -> dict | None:
        if not self._has(_HAS_ADAPTORS):
            return None
        return {
            "PairingStatus": self.get_state("pairing_status", "Not_Started"),
            "MaxPairedDevices": _MAX_ADAPTORS,
            "IsLocalPairingEnabled": True,
            "IsAutoShareEnabled": True,
            "AutoConnectMode": "AutoPresentAndAutoConference",
            "Tx3DevicesMap": self._adaptor_map(),
            "Version": "2.1.2",
        }

    def _sources(self) -> dict:
        active = self.get_state("active_source", "None")
        sources = {}
        if self._has(_HAS_HDMI_INPUT):
            sync = bool(self.get_state("hdmi_input_sync", False))
            sources["Source1"] = {
                "Id": "HDMI", "Name": "Wired", "Type": "HDMI", "IsSource": True,
                "IsActive": active == "HDMI",
                "Status": "Sync Detected" if sync else "Sync Not Detected",
                "Resolution": "1920x1080@60" if sync else "",
            }
        presenting = int(self.get_state("presenting_users", 0) or 0)
        sub = {
            f"SubSource{n}": {
                "Id": f"client-{n}", "Name": f"Presenter {n}", "Type": "AirMedia",
                "IsActive": active == "AirMedia", "Status": "Presenting",
            }
            for n in range(1, presenting + 1)
        }
        sources["Source2"] = {
            "Id": "AmWirelessSources", "Name": "Wireless", "IsSource": False,
            "Type": "AirMedia Wireless Sources",
            "Status": f"{int(self.get_state('connected_users', 0) or 0)} Devices Connected",
            "SourceSubInputs": sub,
        }
        if self._has(_HAS_ADAPTORS):
            sharing = int(self.get_state("adaptor_sharing", 0) or 0)
            sources["Source3"] = {
                "Id": "AirMedia Connected Devices", "IsSource": False,
                "SourceSubInputs": {
                    f"SubSource{n}": {
                        "Id": _adaptor_id(n), "Name": f"AirMedia TX3-{n}",
                        "Type": "AM-TX3-100",
                        "IsActive": active == "Adaptor" and n == sharing,
                        "Status": "Online",
                    }
                    for n in range(1, int(self.get_state("adaptor_count", 0) or 0) + 1)
                },
            }
        return {"SourceGroup1": {"Sources": sources}}

    def _source_selection(self) -> dict:
        return {
            "RoutingMode": self.get_state("routing_mode", "Auto"),
            "IsAutoRoutingEnabled": True,
            "IsPriorityRoutingEnabled": self.get_state("routing_mode") == "AutoWithPriority",
            "SourcesV2": self._sources(),
            "Version": "2.8.0",
        }

    def _avio(self) -> dict:
        output_port = {
            "PortType": "Hdmi",
            "IsSinkConnected": bool(self.get_state("display_connected", True)),
            "CurrentResolution": self.get_state("output_resolution", "3840x2160@30"),
            "Digital": {
                "IsOutputDisabled": bool(self.get_state("output_disabled", False)),
                "IsBlankingEnabled": False,
                "HdcpTransmitterMode": "Auto",
            },
        }
        obj = {
            "Outputs": {"Output1": {"Id": "Output1", "OutputInfo": {
                "Id": "Port1", "Ports": {"Port1": output_port}}}},
            "Inputs": {},
            "Version": "3.0.40",
        }
        if self._has(_HAS_HDMI_INPUT):
            sync = bool(self.get_state("hdmi_input_sync", False))
            obj["Inputs"]["Input1"] = {
                "UserSpecifiedName": "HDMI 1",
                "InputInfo": {"Id": "Port1", "Ports": {"Port1": {
                    "PortType": "Hdmi",
                    "IsSyncDetected": sync,
                    "CurrentResolution": "1920x1080@60" if sync else "",
                    "HorizontalResolution": 1920 if sync else 0,
                    "VerticalResolution": 1080 if sync else 0,
                    "FramesPerSecond": 60 if sync else 0,
                }}},
            }
        return obj

    def _sensors(self) -> dict:
        return {"Sensor1": {
            "IsEnabled": True, "IpId": 18, "Type": "Poe", "Model": "CEN-ODT-C-POE",
            "Manufacturer": "Crestron", "Name": "Occupancy Sensor",
            "DisplayName": "Occupancy Sensor", "ConnectionStatus": "Online",
            "Status": self.get_state("occupancy", "Vacant"),
        }}

    def _app_object(self) -> dict:
        return {"Config": {"OccupancySensor": {"Sensors": self._sensors(), "Version": "2.2.0"}},
                "Version": "3.2.0"}

    # ── Writes ──

    def _post(self, body: str) -> dict:
        try:
            dev = json.loads(body)["Device"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return {"Actions": [{"Operation": "SetPartial", "Results": [
                {"Path": "Device", "StatusId": -4, "StatusInfo": "Generic error"}]}]}
        results = []

        def result(path, prop, status=0, info="OK"):
            results.append({"Path": path, "Property": prop, "StatusId": status,
                            "StatusInfo": info})

        if isinstance(dev.get("AirMedia"), dict):
            self._post_airmedia(dev["AirMedia"], result)
        if isinstance(dev.get("SourceSelectionConfiguration"), dict):
            mode = dev["SourceSelectionConfiguration"].get("RoutingMode")
            if mode is not None:
                if mode in _ROUTING_MODES:
                    self.set_state("routing_mode", mode)
                    result("Device.SourceSelectionConfiguration", "RoutingMode")
                else:
                    result("Device.SourceSelectionConfiguration", "RoutingMode", -1,
                           "value out of range")
        if isinstance(dev.get("AvioV2"), dict):
            self._post_avio(dev["AvioV2"], result)
        if isinstance(dev.get("AirMediaTx3"), dict):
            if not self._has(_HAS_ADAPTORS):
                result("Device.AirMediaTx3", "PairCmd", 3, "Unsupported")
            elif dev["AirMediaTx3"].get("PairCmd"):
                self._start_pairing()
                result("Device.AirMediaTx3", "PairCmd")
        if isinstance(dev.get("DeviceOperations"), dict):
            for prop in dev["DeviceOperations"]:
                if prop == "Reboot":
                    super().set_state("reboot_reason", "manual")
                result("Device.DeviceOperations", prop)
        return {"Actions": [{"Operation": "SetPartial", "Results": results}]}

    def _post_airmedia(self, am: dict, result) -> None:
        base = "Device.AirMedia"
        for prop, value in am.items():
            if prop == "DisconnectAllUsers":
                if value:
                    self.set_state("presenting_users", 0)
                    self.set_state("connected_users", 0)
                    if self.get_state("active_source") == "AirMedia":
                        self.set_state("active_source", "None")
                result(base, prop)
            elif prop == "LoginCodeMode":
                if value in _LOGIN_MODES:
                    self.set_state("login_code_mode", value)
                    if not self.get_state("connected_users"):
                        self._apply_login_code_now()
                    result(base, prop)
                else:
                    result(base, prop, -1, "value out of range")
            elif prop == "LoginCode":
                text = str(value)
                if len(text) == 8 and text.isdigit():
                    self.set_state("login_code", text)
                    if not self.get_state("connected_users"):
                        self._apply_login_code_now()
                    result(base, prop)
                else:
                    result(base, prop, -1, "value out of range")
            elif prop == "SystemMode":
                if value in _SYSTEM_MODES:
                    self.set_state("system_mode", value)
                    result(base, prop)
                else:
                    result(base, prop, -1, "value out of range")
            elif prop == "InactivityTimeout":
                if isinstance(value, int) and 1 <= value <= 60:
                    self.set_state("inactivity_timeout", value)
                    result(base, prop)
                else:
                    result(base, prop, -1, "value out of range")
            elif prop in ("ShowLoginCode", "IsCanvasEnabled", "IsDiscoveryEnabled"):
                state = {"ShowLoginCode": "show_login_code", "IsCanvasEnabled": "canvas_enabled",
                         "IsDiscoveryEnabled": "airmedia_discovery"}[prop]
                self.set_state(state, bool(value))
                result(base, prop)
            elif prop == "Miracast" and isinstance(value, dict):
                if "IsEnabled" in value:
                    self.set_state("miracast_enabled", bool(value["IsEnabled"]))
                result(base + ".Miracast", "IsEnabled")
            elif prop == "WirelessConferencing" and isinstance(value, dict):
                self._post_conferencing(value.get("Configuration") or {}, result)
            else:
                result(base, prop, -2, "The property is read only")

    def _post_conferencing(self, cfg: dict, result) -> None:
        base = "Device.AirMedia.WirelessConferencing.Configuration"
        if not self._has(_HAS_CONFERENCING):
            for prop in cfg:
                result(base, prop, 3, "Unsupported")
            return
        for prop, value in cfg.items():
            if prop == "PeripheralVolume":
                if isinstance(value, int) and 0 <= value <= 100:
                    self.set_state("peripheral_volume", value)
                    result(base, prop)
                else:
                    result(base, prop, -1, "value out of range")
            elif prop == "PeripheralMute":
                self.set_state("peripheral_mute", bool(value))
                result(base, prop)
            elif prop == "IsEnabled":
                self.set_state("conferencing_enabled", bool(value))
                result(base, prop)
            else:
                result(base, prop, -2, "The property is read only")

    def _post_avio(self, avio: dict, result) -> None:
        outputs = avio.get("Outputs")
        for out_key, out in (outputs or {}).items():
            ports = ((out or {}).get("OutputInfo") or {}).get("Ports") or {}
            for port_key, port in ports.items():
                digital = (port or {}).get("Digital") or {}
                path = f"Device.AvioV2.Outputs.{out_key}.OutputInfo.Ports.{port_key}.Digital"
                if out_key != "Output1" or port_key != "Port1":
                    result(path, "IsOutputDisabled", -4, "Generic error")
                    continue
                if "IsOutputDisabled" in digital:
                    self.set_state("output_disabled", bool(digital["IsOutputDisabled"]))
                    result(path, "IsOutputDisabled")

    def _start_pairing(self) -> None:
        if int(self.get_state("adaptor_count", 0) or 0) >= _MAX_ADAPTORS:
            self.set_state("pairing_status", "Max_Reached")
            return
        self.set_state("pairing_status", "In_Progress")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._timers.append(loop.call_later(3.0, self._finish_pairing))

    def _finish_pairing(self) -> None:
        count = min(int(self.get_state("adaptor_count", 0) or 0) + 1, _MAX_ADAPTORS)
        self.set_state("adaptor_count", count)
        self.set_state("pairing_status", "Max_Reached" if count >= _MAX_ADAPTORS else "Not_Started")

    async def stop(self) -> None:
        for timer in self._timers:
            timer.cancel()
        self._timers = []
        for event in list(self._waiters):
            event.set()
        await super().stop()
