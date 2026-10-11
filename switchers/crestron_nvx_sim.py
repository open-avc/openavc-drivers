"""
Crestron DM NVX — Simulator

Role-aware simulation of a DM NVX endpoint over the CresNext REST API. The
simulated ``device_mode`` (Transmitter or Receiver) decides which stream
object answers and which the driver sees as "UNSUPPORTED PROPERTY" — matching
real hardware, where an encoder has no StreamReceive and a decoder no
StreamTransmit. Default is Receiver; set ``device_mode`` in the launch config
to simulate an encoder.

Driver: crestron_nvx
Transport: http (HTTPS on 443)

The NVX REST API is HTTPS-only and the unit ships a self-signed certificate, so
the driver has an https:// base URL and leaves verification off. The simulator
serves TLS to match (``"tls": True``), which lets the driver connect here the
same way it connects to hardware.

The session follows what a DM-NVX-D30 on firmware 7.0.5057 did: GET
/userlogin.html sets TRACKID; a POST of the URL-encoded credentials answers
200 with an empty body and sets userstr, userid, iv, tag and AuthByPasswd,
none of which change afterwards, and no anti-forgery token. A request without
a live session is redirected to the sign-in page (301, Location
/userlogin.html, empty body), where the API document says 403; set
``ended_session_status`` to 403 to serve the document's form instead. A wrong
password is answered 403. A password of ``invalid`` is the wrong one, and so
is any other when the simulator's config gives a ``password``. Two error modes
refuse it on demand: ``wrong_password`` (the password was changed on the unit,
which also ends the open session) and ``ip_blocked`` (the unit has blocked
this address after failed sign-ins and says so).

GET /Device/Longpoll is held until something changes (a command, a setting,
or a change in the Simulator UI) and answers with only the changed
properties, or answers ``{"Device":"Response Timeout"}`` after 30 s, as the
D30 did. A change made while no long poll is open is kept for the next one. A
change inside the stream list or a port arrives the way the D30 sent it: a
one-element array holding only the changed properties, with no index.
Every JSON answer is text/plain ending in CRLF, as the D30's are.
"""

import asyncio
import json
import secrets
from urllib.parse import parse_qsl

from openavc.simulator.http_simulator import HTTPSimulator

_UNSUPPORTED = "UNSUPPORTED PROPERTY, CHECK REST API!!"
_TIMEOUT_ANSWER = {"Device": "Response Timeout"}
_SESSION_COOKIES = ("userstr", "userid", "iv", "tag", "AuthByPasswd")

# State keys the DeviceSpecific object reports, and their property names.
_DEVICE_SPECIFIC = {
    "device_mode": "DeviceMode",
    "video_source": "VideoSource",
    "audio_source": "AudioSource",
    "active_video_source": "ActiveVideoSource",
    "active_audio_source": "ActiveAudioSource",
    "leds_enabled": "LedsEnabled",
    "front_panel_locked": "IsFrontPanelLockoutEnabled",
}
_STREAM = {
    "stream_status": "Status",
    "stream_multicast": "MulticastAddress",
    "stream_location": "StreamLocation",
}
_TX_STREAM = {"bitrate": "Bitrate", "active_bitrate": "ActiveBitrate", "bitrate_mode": "BitrateMode"}
_PORT = {"h_res": "HorizontalResolution", "v_res": "VerticalResolution", "fps": "FramesPerSecond"}


def _merge(into: dict, change: dict) -> None:
    """Fold one change into the changes a long poll has not reported yet. A
    one-element array is merged into the element already waiting."""
    for key, value in change.items():
        old = into.get(key)
        if isinstance(value, dict) and isinstance(old, dict):
            _merge(old, value)
        elif (isinstance(value, list) and isinstance(old, list) and len(value) == len(old) == 1
              and isinstance(value[0], dict) and isinstance(old[0], dict)):
            _merge(old[0], value[0])
        else:
            into[key] = value


class CrestronNvxSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "crestron_nvx",
        "name": "Crestron DM NVX Simulator",
        "category": "switcher",
        "transport": "http",
        "default_port": 443,
        # HTTPS-only device: serve TLS with an ephemeral self-signed cert.
        "tls": True,
        "initial_state": {
            "device_mode": "Receiver",     # "Transmitter" | "Receiver"
            "model": "DM-NVX-D200",
            "serial_number": "SIM00000001",
            "firmware": "7.1.5259.00090",
            "device_name": "DM-NVX-D200-SIM",
            "device_ready": True,
            "video_source": "Stream",
            "audio_source": "AudioFollowsVideo",
            "active_video_source": "Stream",
            "active_audio_source": "AudioFollowsVideo",
            "leds_enabled": True,
            "front_panel_locked": False,
            # Stream (primary)
            "stream_status": "Stream stopped",
            "stream_multicast": "",
            "stream_location": "",
            "bitrate": 700,
            "active_bitrate": 0,
            "bitrate_mode": "Fixed",
            # AV I/O
            "input_sync": False,
            "output_connected": False,
            "h_res": 0,
            "v_res": 0,
            "fps": 0,
            "scaler_resolution": "Auto",
            "video_wall_mode": False,
        },
        "delays": {"command_response": 0.03},
        "error_modes": {
            "communication_timeout": {
                "description": "NVX stops responding to requests",
                "behavior": "no_response",
            },
            "no_sync": {
                "description": "No sync on the HDMI input/output",
                "set_state": {"input_sync": False, "h_res": 0, "v_res": 0},
            },
            "wrong_password": {
                "description": (
                    "The admin password was changed on the NVX (the open "
                    "session ends and every sign-in is refused, HTTP 403)"
                ),
                "behavior": "custom",
            },
            "ip_blocked": {
                "description": (
                    "The NVX has blocked this address after failed sign-ins "
                    "(HTTP 403 \"Your IP address is blocked\")"
                ),
                "behavior": "custom",
            },
            "session_timeout": {
                "description": (
                    "The session times out once (the next request is sent to "
                    "the sign-in page)"
                ),
                "behavior": "custom",
            },
        },
        "controls": [
            {"type": "indicator", "key": "device_mode", "label": "Mode"},
            {"type": "indicator", "key": "model", "label": "Model"},
            {"type": "select", "key": "video_source", "label": "Video Source",
             "options": ["None", "Input1", "Input2", "Stream"]},
            {"type": "select", "key": "audio_source", "label": "Audio Source",
             "options": ["AudioFollowsVideo", "Input1", "Input2", "AnalogAudio",
                         "PrimaryStreamAudio", "SecondaryStreamAudio"]},
            {"type": "toggle", "key": "leds_enabled", "label": "Front LEDs"},
            {"type": "indicator", "key": "stream_status", "label": "Stream"},
            {"type": "slider", "key": "bitrate", "label": "Bitrate", "min": 100, "max": 950, "step": 10},
            {"type": "toggle", "key": "input_sync", "label": "Input Sync",
             "color_map": {"true": "#22c55e", "false": "#6b7280"}},
            {"type": "toggle", "key": "output_connected", "label": "Display Connected"},
        ],
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._session: dict | None = None
        self._trackid = ""
        self._pending: dict = {}
        self._waiters: set[asyncio.Event] = set()
        # How long a long poll is held before its timeout answer (30 s on the
        # D30), and what a request without a live session is answered: 301
        # to the sign-in page (the D30) or 403 (the API document).
        self.longpoll_hold_s = 30.0
        self.ended_session_status = 301

    def _is_tx(self) -> bool:
        return self.get_state("device_mode", "Receiver") == "Transmitter"

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

    def _refusing(self) -> bool:
        return bool({"wrong_password", "ip_blocked"} & set(self.active_errors))

    def end_session(self) -> None:
        """The session times out: the next request has none."""
        self._session = None

    def _session_ok(self, headers: dict) -> bool:
        if self._refusing():
            self._session = None
        if "session_timeout" in self.active_errors and self._session is not None:
            self._session = None
            self.clear_error("session_timeout")
            return False
        if self._session is None:
            return False
        jar = self._cookies(headers)
        return all(jar.get(name) == self._session[name] for name in _SESSION_COOKIES)

    def _refused_session(self):
        if self.ended_session_status == 403:
            return 403, "Forbidden", {}, []
        return self.ended_session_status, "", {"Location": "/userlogin.html"}, []

    def _sign_in(self, body: str):
        if "ip_blocked" in self.active_errors:
            self._session = None
            return 403, "Your IP address is blocked", {}, []
        fields = dict(parse_qsl(body, keep_blank_values=True))
        password = fields.get("passwd", "")
        expected = str(self.config.get("password", "") or "")
        if (
            "wrong_password" in self.active_errors
            or password == "invalid"
            or (expected and password != expected)
        ):
            self._session = None
            return 403, "Forbidden", {}, []
        self._session = {name: secrets.token_hex(16) for name in _SESSION_COOKIES}
        # A new session starts with nothing to report (this simulator's choice).
        self._pending = {}
        return 200, "", {}, [(name, self._session[name]) for name in _SESSION_COOKIES]

    # ── Requests ──

    def dispatch(self, method: str, path: str, headers: dict, body: str):
        """One request: (status, body, response headers, cookies to set)."""
        path = path.split("?", 1)[0]
        if path == "/userlogin.html":
            if method == "POST":
                return self._sign_in(body)
            self._trackid = secrets.token_hex(16)
            return 200, "<html><body>Sign in</body></html>", {}, [("TRACKID", self._trackid)]
        if path == "/logout":
            self._session = None
            return 301, "", {"Location": "/"}, []
        if path == "/Device/Authentication" and method == "POST":
            return 200, self._create_admin(body), {}, []

        # ── Session gate: every API request needs a session ──
        if not path.startswith("/Device"):
            return 404, {"error": "Not Found"}, {}, []
        if not self._session_ok(headers):
            return self._refused_session()
        if method == "GET" and path.startswith("/Device/"):
            return 200, self._get_object(path[len("/Device/"):]), {}, []
        if method == "POST" and path == "/Device":
            status, payload = self._handle_post(body)
            return status, payload, {}, []
        return 404, {"error": "Not Found"}, {}, []

    def handle_request(self, method, path, headers, body):
        """The platform's sync entry point. respond_http below is what runs:
        it sets cookies and holds the long poll."""
        status, payload, response_headers, _cookies = self.dispatch(method, path, headers, body)
        return status, payload, response_headers

    async def longpoll(self, headers: dict):
        """GET /Device/Longpoll: the properties changed since the last one, as
        soon as there are any, or the timeout answer after the hold."""
        if not self._session_ok(headers):
            return self._refused_session()
        if not self._pending:
            event = asyncio.Event()
            self._waiters.add(event)
            try:
                await asyncio.wait_for(event.wait(), self.longpoll_hold_s)
            except asyncio.TimeoutError:
                pass
            finally:
                self._waiters.discard(event)
        # A long poll held when its session ended still answers (the D30's
        # did, with its timeout).
        changes, self._pending = self._pending, {}
        return 200, ({"Device": changes} if changes else _TIMEOUT_ANSWER), {}, []

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
            # The D30 answers JSON as text/plain, compact, ending in CRLF.
            text, content_type = json.dumps(payload, separators=(",", ":")) + "\r\n", "text/plain"
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
        change = self._change_for(key)
        if change:
            self._report(change)

    def _change_for(self, key: str) -> dict | None:
        """The partial Device tree a long poll answers for a changed key."""
        value = self.get_state(key)
        if key in _DEVICE_SPECIFIC:
            prop = _DEVICE_SPECIFIC[key]
            if prop in ("LedsEnabled", "IsFrontPanelLockoutEnabled"):
                value = bool(value)
            return {"DeviceSpecific": {prop: value}}
        if key == "device_ready":
            return {"DeviceSpecific": {"DeviceReady": 1 if value else 0}}
        if key == "video_wall_mode":
            if self._is_tx():
                return None
            return {"DeviceSpecific": {"VideoWallMode": 1 if value else 0}}
        if key == "device_name":
            return {"DeviceInfo": {"Name": value}}
        obj = "StreamTransmit" if self._is_tx() else "StreamReceive"
        if key in _STREAM:
            fields = {_STREAM[key]: value}
            if key == "stream_status":
                fields["Start"] = value == "Stream started"
                fields["Stop"] = value != "Stream started"
            return {obj: {"Streams": [fields]}}
        if key in _TX_STREAM:
            if self._is_tx():
                return {obj: {"Streams": [{_TX_STREAM[key]: value}]}}
            if key == "active_bitrate":
                return {obj: {"Streams": [{"Bitrate": value}]}}
            return None
        port: dict | None = None
        if key in _PORT:
            port = {_PORT[key]: value}
        elif key == "input_sync" and self._is_tx():
            port = {"IsSyncDetected": bool(value), "IsSourceDetected": bool(value)}
        elif key == "output_connected" and not self._is_tx():
            port = {"IsSinkConnected": bool(value)}
        elif key == "scaler_resolution" and not self._is_tx():
            port = {"Resolution": value}
        if port is None:
            return None
        side = "Inputs" if self._is_tx() else "Outputs"
        return {"AudioVideoInputOutput": {side: [{"Ports": [port]}]}}

    # ── GET builders ──

    def _get_object(self, obj: str) -> dict:
        if obj == "DeviceInfo":
            return {"Device": {"DeviceInfo": {
                "Model": self.get_state("model", "DM-NVX-D200"),
                "Manufacturer": "Crestron",
                "Category": "DM",
                "SerialNumber": self.get_state("serial_number", ""),
                "Name": self.get_state("device_name", ""),
                "PufVersion": self.get_state("firmware", ""),
                "DeviceVersion": self.get_state("firmware", ""),
                "MacAddress": "c4.42.68.00.00.01",
            }}}
        if obj == "DeviceSpecific":
            return {"Device": {"DeviceSpecific": {
                "DeviceMode": self.get_state("device_mode", "Receiver"),
                "DeviceReady": 1 if self.get_state("device_ready", True) else 0,
                "VideoSource": self.get_state("video_source", "Stream"),
                "AudioSource": self.get_state("audio_source", "AudioFollowsVideo"),
                "ActiveVideoSource": self.get_state("active_video_source", "Stream"),
                "ActiveAudioSource": self.get_state("active_audio_source", "AudioFollowsVideo"),
                "LedsEnabled": bool(self.get_state("leds_enabled", True)),
                "IsFrontPanelLockoutEnabled": bool(self.get_state("front_panel_locked", False)),
                **({"VideoWallMode": 1 if self.get_state("video_wall_mode") else 0}
                   if not self._is_tx() else {}),
                "Version": "2.1.0",
            }}}
        if obj == "AudioVideoInputOutput":
            return self._build_av_io()
        if obj == "StreamTransmit":
            if not self._is_tx():
                return {"Device": {"StreamTransmit": _UNSUPPORTED}}
            return {"Device": {"StreamTransmit": {"Streams": [self._build_stream(tx=True)], "Version": "2.1.7"}}}
        if obj == "StreamReceive":
            if self._is_tx():
                return {"Device": {"StreamReceive": _UNSUPPORTED}}
            return {"Device": {"StreamReceive": {"Streams": [self._build_stream(tx=False)], "Version": "2.0.1"}}}
        if obj == "DeviceOperations":
            return {"Device": {"DeviceOperations": {"UpgradeStatus": "Puf Upgrade Not Initiated", "Version": "2.3.0"}}}
        return {"Device": {obj: _UNSUPPORTED}}

    def _build_av_io(self) -> dict:
        h, v, fps = self.get_state("h_res", 0), self.get_state("v_res", 0), self.get_state("fps", 0)
        port_common = {
            "HorizontalResolution": h, "VerticalResolution": v, "FramesPerSecond": fps,
            "IsInterlacedDetected": False, "Hdmi": {"HdcpState": "Non-HDCPSource"},
        }
        if self._is_tx():
            inp = {**port_common, "IsSyncDetected": bool(self.get_state("input_sync", False)),
                   "IsSourceDetected": bool(self.get_state("input_sync", False)), "PortType": "Hdmi"}
            return {"Device": {"AudioVideoInputOutput": {
                "Inputs": [{"Name": "input0", "Ports": [inp], "VideoPortTypeSelect": "Hdmi"}],
                "Outputs": [], "Version": "2.4.10"}}}
        out = {**port_common, "IsSinkConnected": bool(self.get_state("output_connected", False)),
               "Resolution": self.get_state("scaler_resolution", "Auto"), "PortType": "Hdmi"}
        return {"Device": {"AudioVideoInputOutput": {
            "Inputs": [], "Outputs": [{"Name": "output0", "Ports": [out], "VideoPortTypeSelect": "Hdmi"}],
            "Version": "2.4.10"}}}

    def _build_stream(self, *, tx: bool) -> dict:
        s = {
            "Status": self.get_state("stream_status", "Stream stopped"),
            "MulticastAddress": self.get_state("stream_multicast", ""),
            "StreamLocation": self.get_state("stream_location", ""),
            "SessionInitiation": "Multicast via RTSP",
            "StreamType": "Primary",
            "Start": self.get_state("stream_status") == "Stream started",
            "Stop": self.get_state("stream_status") != "Stream started",
        }
        if tx:
            s.update({"Bitrate": self.get_state("bitrate", 700),
                      "ActiveBitrate": self.get_state("active_bitrate", 0),
                      "BitrateMode": self.get_state("bitrate_mode", "Fixed")})
        else:
            s.update({"InitiatorAddress": "", "Bitrate": self.get_state("active_bitrate", 0),
                      "StreamProfile": "High"})
        return s

    # ── POST handling ──

    @staticmethod
    def _create_admin(body: str) -> dict:
        """First-boot create-admin (the wizard). Accepts any well-formed request."""
        try:
            data = json.loads(body)
            auth = data["Device"]["Authentication"]["AuthenticationState"]
            if auth.get("AdminUsername") and auth.get("AdminPassword"):
                return {"Actions": [{"Operation": "SetPartial", "Results": [
                    {"Path": "Device.Authentication", "Property": "AuthenticationState",
                     "StatusId": 0, "StatusInfo": "OK"}]}]}
        except Exception:
            pass
        return {"Actions": [{"Operation": "SetPartial", "Results": [
            {"StatusId": -4, "StatusInfo": "Invalid createuser"}]}]}

    def _handle_post(self, body: str):
        try:
            data = json.loads(body)
            dev = data["Device"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return 400, {"error": "Invalid JSON"}

        results = []

        def ok(path, prop):
            results.append({"Path": path, "Property": prop, "StatusId": 0, "StatusInfo": "OK"})

        def err(path, prop, info="Generic error"):
            results.append({"Path": path, "Property": prop, "StatusId": -4, "StatusInfo": info})

        if "DeviceSpecific" in dev:
            for prop, val in dev["DeviceSpecific"].items():
                key = {"VideoSource": "video_source", "AudioSource": "audio_source",
                       "LedsEnabled": "leds_enabled",
                       "IsFrontPanelLockoutEnabled": "front_panel_locked"}.get(prop)
                if key:
                    self.set_state(key, val)
                    if prop == "VideoSource":
                        self.set_state("active_video_source", val)
                    if prop == "AudioSource":
                        self.set_state("active_audio_source", val)
                ok("Device.DeviceSpecific." + prop, prop)

        for obj in ("StreamTransmit", "StreamReceive"):
            if obj in dev and isinstance(dev[obj], dict):
                streams = dev[obj].get("Streams", [])
                if streams:
                    self._apply_stream(obj, streams[0], ok, err)

        if "DeviceOperations" in dev:
            for prop in dev["DeviceOperations"]:
                ok("Device.DeviceOperations." + prop, prop)

        if "AudioVideoInputOutput" in dev:
            outs = dev["AudioVideoInputOutput"].get("Outputs", [])
            if outs and outs[0].get("Ports"):
                res = outs[0]["Ports"][0].get("Resolution")
                if res is not None:
                    self.set_state("scaler_resolution", res)
                ok("Device.AudioVideoInputOutput.Outputs[0]", "Resolution")

        return 200, {"Actions": [{"Operation": "SetPartial", "Results": results}]}

    def _apply_stream(self, obj: str, fields: dict, ok, err) -> None:
        base = f"Device.{obj}.Streams[0]"
        running = self.get_state("stream_status") == "Stream started"
        for prop, val in fields.items():
            if prop == "MulticastAddress":
                # Real NVX rejects a multicast change while running (RTSP mode).
                if running:
                    err(base, prop)
                    continue
                self.set_state("stream_multicast", val)
                ok(base, prop)
            elif prop == "StreamLocation":
                self.set_state("stream_location", val)
                ok(base, prop)
            elif prop == "Bitrate":
                self.set_state("bitrate", val)
                self.set_state("active_bitrate", max(0, int(val) - 60))
                ok(base, prop)
            elif prop == "BitrateMode":
                self.set_state("bitrate_mode", val)
                ok(base, prop)
            elif prop == "Start" and val:
                self.set_state("stream_status", "Stream started")
                if obj == "StreamTransmit" and not self.get_state("stream_location"):
                    # Encoder auto-derives its RTSP URL when it starts.
                    self.set_state("stream_location", "rtsp://127.0.0.1:554/live.sdp")
                ok(base, prop)
            elif prop == "Stop" and val:
                self.set_state("stream_status", "Stream stopped")
                ok(base, prop)
            else:
                ok(base, prop)

    async def stop(self) -> None:
        for event in list(self._waiters):
            event.set()
        await super().stop()
