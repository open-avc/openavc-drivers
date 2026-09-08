"""
ONVIF Camera — Simulator

Simulates an ONVIF Profile S/T PTZ camera over SOAP: the device, media
(Media 1 and Media 2), PTZ, imaging, Device IO and event services, with the
real-time pull-point notification interface (Core spec 9.1) delivering
property events for a digital input, a relay output, motion, tampering and
video signal loss, and the PTZ preset Invoked/Reached events.

Authentication is off by default (``require_auth``), which is what the
connect-lifecycle smoke and a first look in the Simulator UI need. With it on
the simulator checks a WS-Security UsernameToken digest (WSS UsernameToken
Profile 1.1) against its configured password, including the Created timestamp
against its own clock, and in ``auth_mode: "digest"`` it demands RFC 2617
HTTP Digest instead — exactly the two paths Core spec 5.9.1 defines, so the
driver's fallback is exercised. A configurable clock skew stands in for the
camera whose clock is minutes wrong.

Movement integrates continuous velocities over time so a joystick drive
changes the position the driver reads back with GetStatus.

Driver: onvif_camera
Transport: http
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from xml.sax.saxutils import escape

from defusedxml.ElementTree import ParseError as _XMLParseError
from defusedxml.ElementTree import fromstring as _xml_fromstring

from openavc.simulator.http_simulator import HTTPSimulator

NS_SOAP = "http://www.w3.org/2003/05/soap-envelope"
NS_TDS = "http://www.onvif.org/ver10/device/wsdl"
NS_TRT = "http://www.onvif.org/ver10/media/wsdl"
NS_TR2 = "http://www.onvif.org/ver20/media/wsdl"
NS_TPTZ = "http://www.onvif.org/ver20/ptz/wsdl"
NS_TIMG = "http://www.onvif.org/ver20/imaging/wsdl"
NS_TEV = "http://www.onvif.org/ver10/events/wsdl"
NS_TMD = "http://www.onvif.org/ver10/deviceIO/wsdl"
NS_TT = "http://www.onvif.org/ver10/schema"
NS_TNS1 = "http://www.onvif.org/ver10/topics"
NS_WSNT = "http://docs.oasis-open.org/wsn/b-2"
NS_WSA = "http://www.w3.org/2005/08/addressing"
NS_TER = "http://www.onvif.org/ver10/error"
NS_SIM = "http://openavc.example/onvif-sim"

PATH_DEVICE = "/onvif/device_service"
PATH_MEDIA = "/onvif/media_service"
PATH_MEDIA2 = "/onvif/media2_service"
PATH_PTZ = "/onvif/ptz_service"
PATH_IMAGING = "/onvif/imaging_service"
PATH_EVENTS = "/onvif/event_service"
PATH_DEVICEIO = "/onvif/deviceio_service"
PULLPOINT_PREFIX = "/onvif/event_service/pullpoint_"

PROFILE_MAIN = "profile_1"
PROFILE_SUB = "profile_2"
VIDEO_SOURCE = "vs_1"
PTZ_NODE = "ptz_node_1"
RELAY_TOKEN = "relay_1"
INPUT_TOKEN = "input_1"

# Operations a device answers without credentials (access class PRE_AUTH).
PRE_AUTH_OPS = {
    "GetSystemDateAndTime", "GetServices", "GetCapabilities",
    "GetServiceCapabilities", "GetRelayOutputOptions", "GetWsdlUrl",
}

# Full traverse of an axis at velocity 1 takes this long (seconds).
PAN_TRAVERSE_S = 2.5
ZOOM_TRAVERSE_S = 2.0
FOCUS_TRAVERSE_S = 2.0

SPACE_PT_POS = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace"
SPACE_ZOOM_POS = "http://www.onvif.org/ver10/tptz/ZoomSpaces/PositionGenericSpace"
SPACE_PT_TRANS = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationGenericSpace"
SPACE_ZOOM_TRANS = "http://www.onvif.org/ver10/tptz/ZoomSpaces/TranslationGenericSpace"
SPACE_PT_VEL = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace"
SPACE_ZOOM_VEL = "http://www.onvif.org/ver10/tptz/ZoomSpaces/VelocityGenericSpace"
SPACE_PT_SPEED = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/GenericSpeedSpace"
SPACE_ZOOM_SPEED = "http://www.onvif.org/ver10/tptz/ZoomSpaces/ZoomGenericSpeedSpace"

# State keys whose change is a property event, with the topic and how the
# message is built (source item name, source value, data item name).
PROPERTY_EVENTS = {
    "input_1": ("Device/Trigger/DigitalInput", "InputToken", INPUT_TOKEN, "LogicalState"),
    "relay_1": ("Device/Trigger/Relay", "RelayToken", RELAY_TOKEN, "LogicalState"),
    "motion": ("VideoSource/MotionAlarm", "Source", VIDEO_SOURCE, "State"),
    "tamper_dark": ("VideoSource/ImageTooDark/ImagingService", "Source", VIDEO_SOURCE, "State"),
    "signal_loss": ("VideoSource/SignalLoss", "Source", VIDEO_SOURCE, "State"),
}

IMAGING_LEVELS = ("Brightness", "ColorSaturation", "Contrast", "Sharpness")
IMAGING_LEVEL_KEYS = {
    "Brightness": "brightness",
    "ColorSaturation": "color_saturation",
    "Contrast": "contrast",
    "Sharpness": "sharpness",
}
IMAGING_PRESETS = {"indoor": ("Indoor", "Indoor"), "outdoor": ("Outdoor", "Outdoor")}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _ns(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def _child(elem, *path: str):
    node = elem
    for step in path:
        if node is None:
            return None
        node = next((c for c in node if _local(c.tag) == step), None)
    return node


def _children(elem, name: str) -> list:
    return [c for c in elem if _local(c.tag) == name] if elem is not None else []


def _descendant(elem, name: str):
    if elem is None:
        return None
    for node in elem.iter():
        if node is not elem and _local(node.tag) == name:
            return node
    return None


def _text(elem, *path: str, default: str = "") -> str:
    node = _child(elem, *path) if path else elem
    if node is None or node.text is None:
        return default
    return node.text.strip()


def _fmt_time(value: datetime) -> str:
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _parse_time(text: str) -> datetime | None:
    text = (text or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _duration_s(text: str) -> float | None:
    m = re.fullmatch(
        r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?", (text or "").strip(),
    )
    if not m or m.group(0) in ("P", "PT"):
        return None
    d, h, mi, s = m.groups()
    return float(d or 0) * 86400 + float(h or 0) * 3600 + float(mi or 0) * 60 + float(s or 0)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _envelope(inner: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<SOAP-ENV:Envelope xmlns:SOAP-ENV="{NS_SOAP}" xmlns:tt="{NS_TT}" '
        f'xmlns:tds="{NS_TDS}" xmlns:trt="{NS_TRT}" xmlns:tr2="{NS_TR2}" '
        f'xmlns:tptz="{NS_TPTZ}" xmlns:timg="{NS_TIMG}" xmlns:tev="{NS_TEV}" '
        f'xmlns:tmd="{NS_TMD}" xmlns:wsnt="{NS_WSNT}" xmlns:wsa="{NS_WSA}" '
        f'xmlns:tns1="{NS_TNS1}" xmlns:ter="{NS_TER}">'
        f"<SOAP-ENV:Header/><SOAP-ENV:Body>{inner}</SOAP-ENV:Body></SOAP-ENV:Envelope>"
    )


def _ok(prefix: str, op: str, inner: str = "") -> tuple[int, str]:
    return 200, _envelope(f"<{prefix}:{op}Response>{inner}</{prefix}:{op}Response>")


def _fault(kind: str, subcode: str, reason: str, status: int = 500) -> tuple[int, str]:
    """A SOAP 1.2 fault the way Core spec 5.8.2 shows it: Code/Value is
    SOAP-ENV:Sender or Receiver, the ONVIF detail rides in Subcode."""
    sub = ""
    parts = [p for p in subcode.split("/") if p]
    for part in reversed(parts):
        sub = f"<SOAP-ENV:Subcode><SOAP-ENV:Value>ter:{part}</SOAP-ENV:Value>{sub}</SOAP-ENV:Subcode>"
    body = (
        "<SOAP-ENV:Fault>"
        f"<SOAP-ENV:Code><SOAP-ENV:Value>SOAP-ENV:{kind}</SOAP-ENV:Value>{sub}</SOAP-ENV:Code>"
        f'<SOAP-ENV:Reason><SOAP-ENV:Text xml:lang="en">{escape(reason)}</SOAP-ENV:Text></SOAP-ENV:Reason>'
        "</SOAP-ENV:Fault>"
    )
    return status, _envelope(body)


def _space2d(tag: str, uri: str, xr: tuple[float, float], yr: tuple[float, float]) -> str:
    return (
        f"<tt:{tag}><tt:URI>{uri}</tt:URI>"
        f"<tt:XRange><tt:Min>{xr[0]}</tt:Min><tt:Max>{xr[1]}</tt:Max></tt:XRange>"
        f"<tt:YRange><tt:Min>{yr[0]}</tt:Min><tt:Max>{yr[1]}</tt:Max></tt:YRange></tt:{tag}>"
    )


def _space1d(tag: str, uri: str, xr: tuple[float, float]) -> str:
    return (
        f"<tt:{tag}><tt:URI>{uri}</tt:URI>"
        f"<tt:XRange><tt:Min>{xr[0]}</tt:Min><tt:Max>{xr[1]}</tt:Max></tt:XRange></tt:{tag}>"
    )


class _PullPoint:
    def __init__(self, path: str, ends: datetime):
        self.path = path
        self.ends = ends
        self.messages: list[str] = []
        self.event = asyncio.Event()

    def push(self, message: str) -> None:
        self.messages.append(message)
        self.event.set()

    def drain(self, limit: int) -> list[str]:
        out, self.messages = self.messages[:limit], self.messages[limit:]
        if not self.messages:
            self.event.clear()
        return out


class OnvifCameraSimulator(HTTPSimulator):
    """A Profile S/T PTZ camera with I/O and events, over SOAP."""

    SIMULATOR_INFO = {
        "driver_id": "onvif_camera",
        "name": "ONVIF Camera Simulator",
        "category": "camera",
        "transport": "http",
        "default_port": 80,
        "initial_state": {
            "manufacturer": "OpenAVC",
            "model": "Simulated PTZ Camera",
            "firmware_version": "1.2.3",
            "serial_number": "SIM-ONVIF-0001",
            "hardware_id": "SIM-HW-1",
            "pan": 0.0,
            "tilt": 0.0,
            "zoom": 0.0,
            "pan_velocity": 0.0,
            "tilt_velocity": 0.0,
            "zoom_velocity": 0.0,
            "focus_position": 0.5,
            "focus_velocity": 0.0,
            "focus_mode": "AUTO",
            "exposure_mode": "AUTO",
            "wb_mode": "AUTO",
            "ir_cut_filter": "AUTO",
            "backlight": "OFF",
            "wdr": "OFF",
            "brightness": 50.0,
            "contrast": 50.0,
            "color_saturation": 50.0,
            "sharpness": 50.0,
            "imaging_preset": "",
            "motion": False,
            "tamper_dark": False,
            "signal_loss": False,
            "input_1": False,
            "relay_1": False,
            "preset_count": 2,
            "subscriptions": 0,
            "last_aux": "",
            "rebooted": False,
        },
        "controls": [
            {"type": "toggle", "key": "motion", "label": "Motion Detected"},
            {"type": "toggle", "key": "input_1", "label": "Digital Input 1"},
            {"type": "toggle", "key": "tamper_dark", "label": "Lens Covered (Tamper)"},
            {"type": "toggle", "key": "signal_loss", "label": "Video Signal Loss"},
            {"type": "indicator", "key": "relay_1", "label": "Relay 1"},
            {"type": "slider", "key": "pan", "min": -1, "max": 1, "label": "Pan"},
            {"type": "slider", "key": "tilt", "min": -1, "max": 1, "label": "Tilt"},
            {"type": "slider", "key": "zoom", "min": 0, "max": 1, "label": "Zoom"},
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        cfg = self.config
        self._require_auth = bool(cfg.get("require_auth", False))
        self._auth_mode = str(cfg.get("auth_mode", "wsse"))
        self._password = str(cfg.get("password", "secret"))
        self._media2 = bool(cfg.get("media2", True))
        self._legacy_capabilities = bool(cfg.get("legacy_capabilities", False))
        self._ptz = bool(cfg.get("ptz", True))
        self._imaging = bool(cfg.get("imaging", True))
        self._events = bool(cfg.get("events", True))
        self._xaddr_host = cfg.get("xaddr_host")
        self._clock_skew = timedelta(seconds=float(cfg.get("clock_skew_s", 0) or 0))
        self._max_term_s = float(cfg.get("max_term_s", 120) or 120)
        self._require_reference_params = bool(cfg.get("require_reference_params", True))
        self._presets: dict[str, dict[str, Any]] = {
            "1": {"name": "Wide", "pan": 0.0, "tilt": 0.0, "zoom": 0.0},
            "2": {"name": "Lectern", "pan": 0.4, "tilt": -0.2, "zoom": 0.6},
        }
        self._home = (0.0, 0.0, 0.0)
        self._pullpoints: dict[str, _PullPoint] = {}
        self._next_pullpoint = 0
        self._digest_nonces: set[str] = set()
        self._move_deadline: float | None = None
        self._motion_task: asyncio.Task | None = None
        self._last_tick: float | None = None
        self.calls: list[str] = []
        self.renew_count = 0
        self.subscribe_count = 0
        self.unsubscribe_count = 0
        self.last_request_host = ""

    # ── Time, movement ──

    def _now(self) -> datetime:
        return datetime.now(timezone.utc) + self._clock_skew

    def tick(self, dt: float) -> None:
        """Advance movement by ``dt`` seconds (the motion task calls this; a
        test calls it directly)."""
        if self._move_deadline is not None and time.monotonic() >= self._move_deadline:
            self._set_velocity(0.0, 0.0, 0.0)
            self._move_deadline = None
        pv, tv, zv = self.get_state("pan_velocity"), self.get_state("tilt_velocity"), self.get_state("zoom_velocity")
        if pv or tv:
            self.set_state("pan", round(_clamp(self.get_state("pan") + pv * dt / PAN_TRAVERSE_S * 2, -1.0, 1.0), 4))
            self.set_state("tilt", round(_clamp(self.get_state("tilt") + tv * dt / PAN_TRAVERSE_S * 2, -1.0, 1.0), 4))
        if zv:
            self.set_state("zoom", round(_clamp(self.get_state("zoom") + zv * dt / ZOOM_TRAVERSE_S, 0.0, 1.0), 4))
        fv = self.get_state("focus_velocity")
        if fv:
            pos = _clamp(self.get_state("focus_position") + fv * dt / FOCUS_TRAVERSE_S, 0.0, 1.0)
            self.set_state("focus_position", round(pos, 4))
            if pos in (0.0, 1.0):
                self.set_state("focus_velocity", 0.0)

    async def _motion_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.05)
                now = time.monotonic()
                dt = 0.05 if self._last_tick is None else now - self._last_tick
                self._last_tick = now
                self.tick(dt)
        except asyncio.CancelledError:
            pass

    async def start(self, port: int) -> None:
        await super().start(port)
        self._motion_task = asyncio.create_task(self._motion_loop())

    async def stop(self) -> None:
        if self._motion_task is not None:
            self._motion_task.cancel()
            self._motion_task = None
        await super().stop()

    def _set_velocity(self, pan: float | None, tilt: float | None, zoom: float | None) -> None:
        if pan is not None:
            self.set_state("pan_velocity", pan)
        if tilt is not None:
            self.set_state("tilt_velocity", tilt)
        if zoom is not None:
            self.set_state("zoom_velocity", zoom)

    def _moving(self) -> bool:
        return any(self.get_state(k) for k in ("pan_velocity", "tilt_velocity", "zoom_velocity"))

    # ── Events ──

    def set_state(self, key: str, value: Any) -> None:
        changed = self.get_state(key) != value
        super().set_state(key, value)
        if changed and key in PROPERTY_EVENTS:
            self._broadcast(self._property_message(key, "Changed"))

    def _property_message(self, key: str, operation: str) -> str:
        topic, src_name, src_value, data_name = PROPERTY_EVENTS[key]
        value = self.get_state(key)
        if key == "relay_1":
            wire = "active" if value else "inactive"
        else:
            wire = "true" if value else "false"
        return self._notification(topic, {src_name: src_value}, {data_name: wire}, operation)

    def _notification(self, topic: str, source: dict[str, str], data: dict[str, str],
                      operation: str = "") -> str:
        op_attr = f' PropertyOperation="{operation}"' if operation else ""
        src = "".join(f'<tt:SimpleItem Name="{n}" Value="{escape(v)}"/>' for n, v in source.items())
        dat = "".join(f'<tt:SimpleItem Name="{n}" Value="{escape(v)}"/>' for n, v in data.items())
        return (
            "<wsnt:NotificationMessage>"
            '<wsnt:Topic Dialect="http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet">'
            f"tns1:{topic}</wsnt:Topic>"
            f'<wsnt:Message><tt:Message UtcTime="{_fmt_time(self._now())}"{op_attr}>'
            f"<tt:Source>{src}</tt:Source><tt:Data>{dat}</tt:Data>"
            "</tt:Message></wsnt:Message></wsnt:NotificationMessage>"
        )

    def _broadcast(self, message: str) -> None:
        for pp in self._pullpoints.values():
            pp.push(message)

    def drop_pullpoints(self) -> None:
        """Forget every pull point, as a rebooted camera does. A pull that is
        waiting on one of them is released so it answers now; the next call
        on that pull point is the ResourceUnknown fault."""
        for pp in self._pullpoints.values():
            pp.event.set()
        self._pullpoints.clear()
        self.set_state("subscriptions", 0)

    def _preset_events(self, token: str) -> None:
        name = self._presets.get(token, {}).get("name", "")
        for kind in ("Invoked", "Reached"):
            self._broadcast(self._notification(
                f"PTZController/PTZPresets/{kind}",
                {"PTZConfigurationToken": "ptzc_1"},
                {"PresetToken": token, "PresetName": name},
            ))

    # ── Request handling ──

    def _base_url(self, headers: dict[str, str]) -> str:
        host = self._xaddr_host
        if not host:
            raw = ""
            for k, v in headers.items():
                if k.lower() == "host":
                    raw = v
                    break
            host = raw.split(":", 1)[0] if raw else "127.0.0.1"
        port = getattr(self, "port", None) or 80
        return f"http://{host}:{port}"

    def handle_request(self, method: str, path: str, headers: dict[str, str], body: str):
        """The sync entry point every HTTPSimulator has. PullMessages answers
        with whatever is queued rather than waiting; the async path in
        respond_http / handle_request_async does the long poll."""
        kind, payload = self._prepare(method, path, headers, body)
        if kind == "pull":
            pp, limit, _timeout = payload
            return self._pull_response(pp, limit)
        return payload

    async def handle_request_async(self, method: str, path: str, headers: dict[str, str], body: str):
        kind, payload = self._prepare(method, path, headers, body)
        if kind != "pull":
            return payload
        pp, limit, timeout = payload
        if not pp.messages:
            try:
                await asyncio.wait_for(pp.event.wait(), timeout)
            except asyncio.TimeoutError:
                pass
        return self._pull_response(pp, limit)

    async def respond_http(self, request, method, path, headers, body):
        from aiohttp import web

        response_headers: dict[str, str] = {}
        try:
            result = await self.handle_request_async(method, path, headers, body)
            if len(result) == 3:
                status_code, response_body, response_headers = result
            else:
                status_code, response_body = result
        except Exception:  # noqa: BLE001 - a sim bug answers 500, never hangs the driver
            status_code, response_body = 500, "Internal simulator error"
        text = str(response_body)
        self.log_protocol("out", f"{status_code} | {text[:200]}")
        return web.Response(
            status=status_code, text=text, content_type="application/soap+xml",
            headers=response_headers or None,
        )

    def _prepare(self, method: str, path: str, headers: dict[str, str], body: str):
        if method != "POST":
            return "response", (405, "Method Not Allowed")
        try:
            root = _xml_fromstring(body)
        except (_XMLParseError, ValueError):
            return "response", _fault("Sender", "WellFormed", "The request is not well-formed XML", 400)
        soap_body = _child(root, "Body")
        if soap_body is None or len(soap_body) == 0:
            return "response", _fault("Sender", "WellFormed", "No SOAP body", 400)
        req = soap_body[0]
        op = _local(req.tag)
        ns = _ns(req.tag)
        self.calls.append(op)
        for k, v in headers.items():
            if k.lower() == "host":
                self.last_request_host = v
        refused = self._check_auth(method, path, headers, root, op)
        if refused is not None:
            return "response", refused
        base = self._base_url(headers)
        path_only = path.split("?", 1)[0]
        if path_only.startswith(PULLPOINT_PREFIX):
            return self._handle_pullpoint(path_only, root, req, op)
        if ns == NS_TDS or path_only == PATH_DEVICE:
            return "response", self._handle_device(op, req, base)
        if ns == NS_TR2:
            return "response", self._handle_media2(op, req, base)
        if ns == NS_TRT:
            return "response", self._handle_media1(op, req, base)
        if ns == NS_TPTZ:
            return "response", self._handle_ptz(op, req)
        if ns == NS_TIMG:
            return "response", self._handle_imaging(op, req)
        if ns == NS_TEV:
            return "response", self._handle_events(op, req, base)
        if ns == NS_TMD:
            return "response", self._handle_deviceio(op, req)
        return "response", _fault("Receiver", "ActionNotSupported", f"Unknown operation {op}")

    # ── Authentication (Core spec 5.9.1) ──

    def _check_auth(self, method, path, headers, root, op):
        if not self._require_auth or op in PRE_AUTH_OPS:
            return None
        if self._auth_mode == "digest":
            return self._check_digest(method, path, headers)
        header = _child(root, "Header")
        token = _descendant(header, "UsernameToken") if header is not None else None
        if token is None:
            return _fault("Sender", "NotAuthorized", "Sender not authorized", 400)
        password = _child(token, "Password")
        nonce_b64 = _text(token, "Nonce")
        created = _text(token, "Created")
        if password is None or not nonce_b64 or not created:
            return _fault("Sender", "NotAuthorized", "Username token needs a digest, a nonce and a timestamp", 400)
        created_at = _parse_time(created)
        if created_at is None or abs((created_at - self._now()).total_seconds()) > 120:
            return _fault("Sender", "NotAuthorized", "Username token timestamp is not fresh", 400)
        try:
            nonce = base64.b64decode(nonce_b64)
        except ValueError:
            return _fault("Sender", "NotAuthorized", "Bad nonce", 400)
        expected = base64.b64encode(
            hashlib.sha1(nonce + created.encode() + self._password.encode()).digest()
        ).decode()
        if (password.text or "").strip() != expected:
            return _fault("Sender", "NotAuthorized", "Sender not authorized", 400)
        return None

    def _digest_challenge(self):
        nonce = secrets.token_hex(16)
        self._digest_nonces.add(nonce)
        return 401, "", {
            "WWW-Authenticate": f'Digest realm="ONVIF", nonce="{nonce}", qop="auth", algorithm=MD5',
        }

    def _check_digest(self, method: str, path: str, headers: dict[str, str]):
        auth = ""
        for k, v in headers.items():
            if k.lower() == "authorization":
                auth = v
        if not auth.startswith("Digest "):
            return self._digest_challenge()
        fields = {
            k: v.strip('"')
            for k, v in re.findall(r'(\w+)=("[^"]*"|[^,\s]+)', auth[7:])
        }
        nonce = fields.get("nonce", "")
        if nonce not in self._digest_nonces:
            return self._digest_challenge()
        ha1 = hashlib.md5(f"{fields.get('username', '')}:{fields.get('realm', '')}:{self._password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{fields.get('uri', '')}".encode()).hexdigest()
        expected = hashlib.md5(
            f"{ha1}:{nonce}:{fields.get('nc', '')}:{fields.get('cnonce', '')}:{fields.get('qop', '')}:{ha2}".encode()
        ).hexdigest()
        if fields.get("response", "") != expected:
            return self._digest_challenge()
        return None

    # ── Device service ──

    def _services_xml(self, base: str) -> str:
        entries = [
            (NS_TDS, PATH_DEVICE, 26, 6),
            (NS_TRT, PATH_MEDIA, 24, 12),
            (NS_TMD, PATH_DEVICEIO, 22, 6),
        ]
        if self._media2:
            entries.append((NS_TR2, PATH_MEDIA2, 26, 6))
        if self._ptz:
            entries.append((NS_TPTZ, PATH_PTZ, 26, 6))
        if self._imaging:
            entries.append((NS_TIMG, PATH_IMAGING, 22, 6))
        if self._events:
            entries.append((NS_TEV, PATH_EVENTS, 26, 6))
        return "".join(
            f"<tds:Service><tds:Namespace>{ns}</tds:Namespace><tds:XAddr>{base}{p}</tds:XAddr>"
            f"<tds:Version><tt:Major>{maj}</tt:Major><tt:Minor>{mi}</tt:Minor></tds:Version></tds:Service>"
            for ns, p, maj, mi in entries
        )

    def _handle_device(self, op: str, req, base: str):
        if op == "GetSystemDateAndTime":
            now = self._now()
            return _ok("tds", op,
                "<tds:SystemDateAndTime><tt:DateTimeType>NTP</tt:DateTimeType>"
                "<tt:DaylightSavings>false</tt:DaylightSavings><tt:TimeZone><tt:TZ>UTC0</tt:TZ></tt:TimeZone>"
                "<tt:UTCDateTime>"
                f"<tt:Time><tt:Hour>{now.hour}</tt:Hour><tt:Minute>{now.minute}</tt:Minute><tt:Second>{now.second}</tt:Second></tt:Time>"
                f"<tt:Date><tt:Year>{now.year}</tt:Year><tt:Month>{now.month}</tt:Month><tt:Day>{now.day}</tt:Day></tt:Date>"
                "</tt:UTCDateTime></tds:SystemDateAndTime>")
        if op == "GetServices":
            if self._legacy_capabilities:
                return _fault("Receiver", "ActionNotSupported", "GetServices is not supported")
            return _ok("tds", op, self._services_xml(base))
        if op == "GetCapabilities":
            caps = f"<tt:Device><tt:XAddr>{base}{PATH_DEVICE}</tt:XAddr></tt:Device>"
            if self._events:
                caps += (f"<tt:Events><tt:XAddr>{base}{PATH_EVENTS}</tt:XAddr>"
                         "<tt:WSSubscriptionPolicySupport>false</tt:WSSubscriptionPolicySupport>"
                         "<tt:WSPullPointSupport>true</tt:WSPullPointSupport>"
                         "<tt:WSPausableSubscriptionManagerInterfaceSupport>false</tt:WSPausableSubscriptionManagerInterfaceSupport></tt:Events>")
            if self._imaging:
                caps += f"<tt:Imaging><tt:XAddr>{base}{PATH_IMAGING}</tt:XAddr></tt:Imaging>"
            caps += (f"<tt:Media><tt:XAddr>{base}{PATH_MEDIA}</tt:XAddr><tt:StreamingCapabilities>"
                     "<tt:RTPMulticast>false</tt:RTPMulticast><tt:RTP_TCP>true</tt:RTP_TCP><tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP>"
                     "</tt:StreamingCapabilities></tt:Media>")
            if self._ptz:
                caps += f"<tt:PTZ><tt:XAddr>{base}{PATH_PTZ}</tt:XAddr></tt:PTZ>"
            return _ok("tds", op, f"<tds:Capabilities>{caps}</tds:Capabilities>")
        if op == "GetDeviceInformation":
            s = self.get_state
            return _ok("tds", op,
                f"<tds:Manufacturer>{escape(s('manufacturer'))}</tds:Manufacturer>"
                f"<tds:Model>{escape(s('model'))}</tds:Model>"
                f"<tds:FirmwareVersion>{escape(s('firmware_version'))}</tds:FirmwareVersion>"
                f"<tds:SerialNumber>{escape(s('serial_number'))}</tds:SerialNumber>"
                f"<tds:HardwareId>{escape(s('hardware_id'))}</tds:HardwareId>")
        if op == "GetRelayOutputs":
            return _ok("tds", op,
                f'<tds:RelayOutputs token="{RELAY_TOKEN}"><tt:Properties><tt:Mode>Bistable</tt:Mode>'
                "<tt:DelayTime>PT0S</tt:DelayTime><tt:IdleState>open</tt:IdleState></tt:Properties></tds:RelayOutputs>")
        if op == "SetRelayOutputState":
            token = _text(req, "RelayOutputToken")
            if token != RELAY_TOKEN:
                return _fault("Sender", "InvalidArgVal/RelayToken", "Unknown relay token")
            self.set_state("relay_1", _text(req, "LogicalState").lower() == "active")
            return _ok("tds", op)
        if op == "SystemReboot":
            self.set_state("rebooted", True)
            self.drop_pullpoints()
            return _ok("tds", op, "<tds:Message>Rebooting</tds:Message>")
        if op == "GetScopes":
            return _ok("tds", op,
                "<tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>onvif://www.onvif.org/type/video_encoder</tt:ScopeItem></tds:Scopes>")
        return _fault("Receiver", "ActionNotSupported", f"{op} is not supported")

    # ── Media ──

    def _profile_defs(self) -> list[dict[str, Any]]:
        return [
            {"token": PROFILE_MAIN, "name": "MainStream", "width": 1920, "height": 1080, "fps": 30,
             "ptz": self._ptz, "stream": 1},
            {"token": PROFILE_SUB, "name": "SubStream", "width": 640, "height": 360, "fps": 15,
             "ptz": False, "stream": 2},
        ]

    def _profile(self, token: str) -> dict[str, Any] | None:
        return next((p for p in self._profile_defs() if p["token"] == token), None)

    @staticmethod
    def _ptz_config_xml(tag: str) -> str:
        return (
            f'<{tag} token="ptzc_1"><tt:Name>PTZ Config</tt:Name><tt:UseCount>1</tt:UseCount>'
            f"<tt:NodeToken>{PTZ_NODE}</tt:NodeToken>"
            f"<tt:DefaultAbsolutePantTiltPositionSpace>{SPACE_PT_POS}</tt:DefaultAbsolutePantTiltPositionSpace>"
            f"<tt:DefaultAbsoluteZoomPositionSpace>{SPACE_ZOOM_POS}</tt:DefaultAbsoluteZoomPositionSpace>"
            f"<tt:DefaultRelativePanTiltTranslationSpace>{SPACE_PT_TRANS}</tt:DefaultRelativePanTiltTranslationSpace>"
            f"<tt:DefaultRelativeZoomTranslationSpace>{SPACE_ZOOM_TRANS}</tt:DefaultRelativeZoomTranslationSpace>"
            f"<tt:DefaultContinuousPanTiltVelocitySpace>{SPACE_PT_VEL}</tt:DefaultContinuousPanTiltVelocitySpace>"
            f"<tt:DefaultContinuousZoomVelocitySpace>{SPACE_ZOOM_VEL}</tt:DefaultContinuousZoomVelocitySpace>"
            "<tt:DefaultPTZTimeout>PT60S</tt:DefaultPTZTimeout>"
            f"</{tag}>"
        )

    def _stream_url(self, base: str, stream: int) -> str:
        host = base.split("://", 1)[1].rsplit(":", 1)[0]
        return f"rtsp://{host}:554/stream{stream}"

    def _handle_media2(self, op: str, req, base: str):
        if not self._media2:
            return _fault("Receiver", "ActionNotSupported", "Media2 is not supported")
        if op == "GetProfiles":
            out = ""
            for p in self._profile_defs():
                out += (
                    f'<tr2:Profiles token="{p["token"]}" fixed="true"><tt:Name>{p["name"]}</tt:Name>'
                    "<tr2:Configurations>"
                    f'<tr2:VideoSource token="vsc_1"><tt:Name>VideoSourceConfig</tt:Name><tt:UseCount>2</tt:UseCount>'
                    f"<tt:SourceToken>{VIDEO_SOURCE}</tt:SourceToken>"
                    '<tt:Bounds x="0" y="0" width="1920" height="1080"/></tr2:VideoSource>'
                    f'<tr2:VideoEncoder token="vec_{p["stream"]}"><tt:Name>Encoder{p["stream"]}</tt:Name><tt:UseCount>1</tt:UseCount>'
                    f"<tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>{p['width']}</tt:Width><tt:Height>{p['height']}</tt:Height></tt:Resolution>"
                    f"<tt:RateControl><tt:FrameRateLimit>{p['fps']}</tt:FrameRateLimit><tt:BitrateLimit>4096</tt:BitrateLimit></tt:RateControl>"
                    "<tt:Quality>4</tt:Quality></tr2:VideoEncoder>"
                    + (self._ptz_config_xml("tr2:PTZ") if p["ptz"] else "")
                    + "</tr2:Configurations></tr2:Profiles>"
                )
            return _ok("tr2", op, out)
        if op in ("GetStreamUri", "GetSnapshotUri"):
            token = _text(req, "ProfileToken")
            prof = self._profile(token)
            if prof is None:
                return _fault("Sender", "InvalidArgVal/NoProfile", "The requested profile token does not exist")
            if op == "GetStreamUri":
                proto = _text(req, "Protocol")
                if proto not in ("RtspUnicast", "RtspMulticast", "RTSP", "RtspsUnicast", "RtspsMulticast", "RtspOverHttp"):
                    return _fault("Sender", "InvalidArgVal/InvalidStreamSetup", "The specified Protocol is not supported")
                return _ok("tr2", op, f"<tr2:Uri>{self._stream_url(base, prof['stream'])}</tr2:Uri>")
            return _ok("tr2", op, f"<tr2:Uri>{base}/snapshot{prof['stream']}.jpg</tr2:Uri>")
        return _fault("Receiver", "ActionNotSupported", f"{op} is not supported")

    def _handle_media1(self, op: str, req, base: str):
        if op == "GetProfiles":
            out = ""
            for p in self._profile_defs():
                out += (
                    f'<trt:Profiles token="{p["token"]}" fixed="true"><tt:Name>{p["name"]}</tt:Name>'
                    f'<tt:VideoSourceConfiguration token="vsc_1"><tt:Name>VideoSourceConfig</tt:Name><tt:UseCount>2</tt:UseCount>'
                    f"<tt:SourceToken>{VIDEO_SOURCE}</tt:SourceToken>"
                    '<tt:Bounds x="0" y="0" width="1920" height="1080"/></tt:VideoSourceConfiguration>'
                    f'<tt:VideoEncoderConfiguration token="vec_{p["stream"]}"><tt:Name>Encoder{p["stream"]}</tt:Name><tt:UseCount>1</tt:UseCount>'
                    f"<tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>{p['width']}</tt:Width><tt:Height>{p['height']}</tt:Height></tt:Resolution>"
                    "<tt:Quality>4</tt:Quality>"
                    f"<tt:RateControl><tt:FrameRateLimit>{p['fps']}</tt:FrameRateLimit><tt:EncodingInterval>1</tt:EncodingInterval><tt:BitrateLimit>4096</tt:BitrateLimit></tt:RateControl>"
                    "<tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type><tt:IPv4Address>0.0.0.0</tt:IPv4Address></tt:Address>"
                    "<tt:Port>0</tt:Port><tt:TTL>1</tt:TTL><tt:AutoStart>false</tt:AutoStart></tt:Multicast>"
                    "<tt:SessionTimeout>PT60S</tt:SessionTimeout></tt:VideoEncoderConfiguration>"
                    + (self._ptz_config_xml("tt:PTZConfiguration") if p["ptz"] else "")
                    + "</trt:Profiles>"
                )
            return _ok("trt", op, out)
        if op in ("GetStreamUri", "GetSnapshotUri"):
            token = _text(req, "ProfileToken")
            prof = self._profile(token)
            if prof is None:
                return _fault("Sender", "InvalidArgVal/NoProfile", "The media profile does not exist")
            uri = self._stream_url(base, prof["stream"]) if op == "GetStreamUri" else f"{base}/snapshot{prof['stream']}.jpg"
            return _ok("trt", op,
                f"<trt:MediaUri><tt:Uri>{uri}</tt:Uri><tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
                "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot><tt:Timeout>PT0S</tt:Timeout></trt:MediaUri>")
        return _fault("Receiver", "ActionNotSupported", f"{op} is not supported")

    # ── PTZ ──

    def _ptz_profile_check(self, req):
        token = _text(req, "ProfileToken")
        prof = self._profile(token)
        if prof is None:
            return _fault("Sender", "InvalidArgVal/NoProfile", "The requested profile token does not exist")
        if not prof["ptz"]:
            return _fault("Sender", "InvalidArgVal/NoPTZProfile", "The requested profile token does not reference a PTZ configuration")
        return None

    def _node_xml(self) -> str:
        spaces = (
            _space2d("AbsolutePanTiltPositionSpace", SPACE_PT_POS, (-1.0, 1.0), (-1.0, 1.0))
            + _space1d("AbsoluteZoomPositionSpace", SPACE_ZOOM_POS, (0.0, 1.0))
            + _space2d("RelativePanTiltTranslationSpace", SPACE_PT_TRANS, (-1.0, 1.0), (-1.0, 1.0))
            + _space1d("RelativeZoomTranslationSpace", SPACE_ZOOM_TRANS, (-1.0, 1.0))
            + _space2d("ContinuousPanTiltVelocitySpace", SPACE_PT_VEL, (-1.0, 1.0), (-1.0, 1.0))
            + _space1d("ContinuousZoomVelocitySpace", SPACE_ZOOM_VEL, (-1.0, 1.0))
            + _space1d("PanTiltSpeedSpace", SPACE_PT_SPEED, (0.0, 1.0))
            + _space1d("ZoomSpeedSpace", SPACE_ZOOM_SPEED, (0.0, 1.0))
        )
        return (
            f'<tptz:PTZNode token="{PTZ_NODE}" FixedHomePosition="false"><tt:Name>PTZ Node</tt:Name>'
            f"<tt:SupportedPTZSpaces>{spaces}</tt:SupportedPTZSpaces>"
            "<tt:MaximumNumberOfPresets>32</tt:MaximumNumberOfPresets><tt:HomeSupported>true</tt:HomeSupported>"
            "<tt:AuxiliaryCommands>tt:IRLamp|On</tt:AuxiliaryCommands><tt:AuxiliaryCommands>tt:IRLamp|Off</tt:AuxiliaryCommands>"
            "<tt:AuxiliaryCommands>tt:Wiper|On</tt:AuxiliaryCommands></tptz:PTZNode>"
        )

    def _position_xml(self, pan, tilt, zoom) -> str:
        return (
            f'<tt:PanTilt x="{pan}" y="{tilt}" space="{SPACE_PT_POS}"/>'
            f'<tt:Zoom x="{zoom}" space="{SPACE_ZOOM_POS}"/>'
        )

    def _handle_ptz(self, op: str, req):
        if not self._ptz:
            return _fault("Receiver", "ActionNotSupported", "PTZ is not supported")
        if op == "GetNodes":
            return _ok("tptz", op, self._node_xml())
        if op == "GetNode":
            if _text(req, "NodeToken") != PTZ_NODE:
                return _fault("Sender", "InvalidArgVal/NoEntity", "No such PTZ node on the device")
            return _ok("tptz", op, self._node_xml())
        if op == "GetConfigurations":
            return _ok("tptz", op, self._ptz_config_xml("tptz:PTZConfiguration"))
        bad = self._ptz_profile_check(req)
        if bad is not None:
            return bad
        if op == "ContinuousMove":
            velocity = _child(req, "Velocity")
            pt = _child(velocity, "PanTilt") if velocity is not None else None
            zm = _child(velocity, "Zoom") if velocity is not None else None
            pan = tilt = zoom = None
            if pt is not None:
                pan, tilt = float(pt.get("x", 0)), float(pt.get("y", 0))
                if not (-1 <= pan <= 1 and -1 <= tilt <= 1):
                    return _fault("Sender", "InvalidArgVal/InvalidVelocity", "The requested speed is out of bounds")
            if zm is not None:
                zoom = float(zm.get("x", 0))
                if not -1 <= zoom <= 1:
                    return _fault("Sender", "InvalidArgVal/InvalidVelocity", "The requested speed is out of bounds")
            self._set_velocity(pan, tilt, zoom)
            timeout = _duration_s(_text(req, "Timeout"))
            self._move_deadline = time.monotonic() + timeout if timeout else None
            return _ok("tptz", op)
        if op == "Stop":
            pt = _text(req, "PanTilt", default="true").lower() != "false"
            zm = _text(req, "Zoom", default="true").lower() != "false"
            self._set_velocity(0.0 if pt else None, 0.0 if pt else None, 0.0 if zm else None)
            return _ok("tptz", op)
        if op == "AbsoluteMove":
            position = _child(req, "Position")
            pt = _child(position, "PanTilt") if position is not None else None
            zm = _child(position, "Zoom") if position is not None else None
            if pt is not None:
                pan, tilt = float(pt.get("x", 0)), float(pt.get("y", 0))
                if not (-1 <= pan <= 1 and -1 <= tilt <= 1):
                    return _fault("Sender", "InvalidArgVal/InvalidPosition", "The requested position is out of bounds")
                self._set_velocity(0.0, 0.0, None)
                self.set_state("pan", pan)
                self.set_state("tilt", tilt)
            if zm is not None:
                zoom = float(zm.get("x", 0))
                if not 0 <= zoom <= 1:
                    return _fault("Sender", "InvalidArgVal/InvalidPosition", "The requested position is out of bounds")
                self._set_velocity(None, None, 0.0)
                self.set_state("zoom", zoom)
            return _ok("tptz", op)
        if op == "RelativeMove":
            translation = _child(req, "Translation")
            pt = _child(translation, "PanTilt") if translation is not None else None
            zm = _child(translation, "Zoom") if translation is not None else None
            if pt is not None:
                self.set_state("pan", round(_clamp(self.get_state("pan") + float(pt.get("x", 0)), -1, 1), 4))
                self.set_state("tilt", round(_clamp(self.get_state("tilt") + float(pt.get("y", 0)), -1, 1), 4))
            if zm is not None:
                self.set_state("zoom", round(_clamp(self.get_state("zoom") + float(zm.get("x", 0)), 0, 1), 4))
            return _ok("tptz", op)
        if op == "GetStatus":
            moving_pt = "MOVING" if (self.get_state("pan_velocity") or self.get_state("tilt_velocity")) else "IDLE"
            moving_z = "MOVING" if self.get_state("zoom_velocity") else "IDLE"
            return _ok("tptz", op,
                "<tptz:PTZStatus><tt:Position>"
                + self._position_xml(self.get_state("pan"), self.get_state("tilt"), self.get_state("zoom"))
                + f"</tt:Position><tt:MoveStatus><tt:PanTilt>{moving_pt}</tt:PanTilt><tt:Zoom>{moving_z}</tt:Zoom></tt:MoveStatus>"
                f"<tt:UtcTime>{_fmt_time(self._now())}</tt:UtcTime></tptz:PTZStatus>")
        if op == "GetPresets":
            out = "".join(
                f'<tptz:Preset token="{t}"><tt:Name>{escape(p["name"])}</tt:Name>'
                f"<tt:PTZPosition>{self._position_xml(p['pan'], p['tilt'], p['zoom'])}</tt:PTZPosition></tptz:Preset>"
                for t, p in self._presets.items()
            )
            return _ok("tptz", op, out)
        if op == "GotoPreset":
            token = _text(req, "PresetToken")
            preset = self._presets.get(token)
            if preset is None:
                return _fault("Sender", "InvalidArgVal/NoToken", "The requested preset token does not exist")
            self._set_velocity(0.0, 0.0, 0.0)
            self.set_state("pan", preset["pan"])
            self.set_state("tilt", preset["tilt"])
            self.set_state("zoom", preset["zoom"])
            self._preset_events(token)
            return _ok("tptz", op)
        if op == "SetPreset":
            if self._moving():
                return _fault("Receiver", "Action/MovingPTZ", "Preset cannot be set while PTZ unit is moving")
            name = _text(req, "PresetName")
            token = _text(req, "PresetToken")
            if token and token not in self._presets:
                return _fault("Sender", "InvalidArgVal/NoToken", "The requested preset token does not exist")
            if name and any(p["name"] == name and t != token for t, p in self._presets.items()):
                return _fault("Sender", "InvalidArgVal/PresetExist", "The requested name already exist for another preset")
            if len(self._presets) >= 32 and not token:
                return _fault("Receiver", "Action/TooManyPresets", "Maximum number of Presets reached")
            if not token:
                token = str(max((int(t) for t in self._presets if t.isdigit()), default=0) + 1)
            self._presets[token] = {
                "name": name or self._presets.get(token, {}).get("name") or f"Preset {token}",
                "pan": self.get_state("pan"), "tilt": self.get_state("tilt"), "zoom": self.get_state("zoom"),
            }
            self.set_state("preset_count", len(self._presets))
            return _ok("tptz", op, f"<tptz:PresetToken>{token}</tptz:PresetToken>")
        if op == "RemovePreset":
            token = _text(req, "PresetToken")
            if token not in self._presets:
                return _fault("Sender", "InvalidArgVal/NoToken", "The requested preset token does not exist")
            del self._presets[token]
            self.set_state("preset_count", len(self._presets))
            return _ok("tptz", op)
        if op == "GotoHomePosition":
            self._set_velocity(0.0, 0.0, 0.0)
            self.set_state("pan", self._home[0])
            self.set_state("tilt", self._home[1])
            self.set_state("zoom", self._home[2])
            return _ok("tptz", op)
        if op == "SetHomePosition":
            self._home = (self.get_state("pan"), self.get_state("tilt"), self.get_state("zoom"))
            return _ok("tptz", op)
        if op == "SendAuxiliaryCommand":
            data = _text(req, "AuxiliaryData")
            self.set_state("last_aux", data)
            return _ok("tptz", op, f"<tptz:AuxiliaryResponse>{escape(data)}</tptz:AuxiliaryResponse>")
        return _fault("Receiver", "ActionNotSupported", f"{op} is not supported")

    # ── Imaging ──

    def _imaging_settings_xml(self) -> str:
        s = self.get_state
        return (
            "<timg:ImagingSettings>"
            f"<tt:BacklightCompensation><tt:Mode>{s('backlight')}</tt:Mode><tt:Level>0.5</tt:Level></tt:BacklightCompensation>"
            f"<tt:Brightness>{s('brightness')}</tt:Brightness>"
            f"<tt:ColorSaturation>{s('color_saturation')}</tt:ColorSaturation>"
            f"<tt:Contrast>{s('contrast')}</tt:Contrast>"
            f"<tt:Exposure><tt:Mode>{s('exposure_mode')}</tt:Mode><tt:Priority>LowNoise</tt:Priority>"
            "<tt:MinExposureTime>10</tt:MinExposureTime><tt:MaxExposureTime>40000</tt:MaxExposureTime>"
            "<tt:ExposureTime>8000</tt:ExposureTime><tt:Gain>6</tt:Gain><tt:Iris>0</tt:Iris></tt:Exposure>"
            f"<tt:Focus><tt:AutoFocusMode>{s('focus_mode')}</tt:AutoFocusMode><tt:DefaultSpeed>0.5</tt:DefaultSpeed>"
            "<tt:NearLimit>0.1</tt:NearLimit><tt:FarLimit>0</tt:FarLimit></tt:Focus>"
            f"<tt:IrCutFilter>{s('ir_cut_filter')}</tt:IrCutFilter>"
            f"<tt:Sharpness>{s('sharpness')}</tt:Sharpness>"
            f"<tt:WideDynamicRange><tt:Mode>{s('wdr')}</tt:Mode><tt:Level>0.5</tt:Level></tt:WideDynamicRange>"
            f"<tt:WhiteBalance><tt:Mode>{s('wb_mode')}</tt:Mode><tt:CrGain>1</tt:CrGain><tt:CbGain>1</tt:CbGain></tt:WhiteBalance>"
            "</timg:ImagingSettings>"
        )

    def _handle_imaging(self, op: str, req):
        if not self._imaging:
            return _fault("Receiver", "ActionNotSupported", "Imaging is not supported")
        if _text(req, "VideoSourceToken") != VIDEO_SOURCE:
            return _fault("Sender", "InvalidArgVal/NoSource", "The requested VideoSource does not exist")
        if op == "GetImagingSettings":
            return _ok("timg", op, self._imaging_settings_xml())
        if op == "SetImagingSettings":
            settings = _child(req, "ImagingSettings")
            if settings is None:
                return _fault("Sender", "InvalidArgVal/SettingsInvalid", "The requested settings are incorrect")
            pending: dict[str, Any] = {}
            for elem in settings:
                name = _local(elem.tag)
                if name in IMAGING_LEVELS:
                    try:
                        value = float((elem.text or "").strip())
                    except ValueError:
                        return _fault("Sender", "InvalidArgVal/SettingsInvalid", "The requested settings are incorrect")
                    if not 0 <= value <= 100:
                        return _fault("Sender", "InvalidArgVal/SettingsInvalid", "The requested settings are incorrect")
                    pending[IMAGING_LEVEL_KEYS[name]] = value
                elif name == "IrCutFilter":
                    mode = (elem.text or "").strip()
                    if mode not in ("ON", "OFF", "AUTO"):
                        return _fault("Sender", "InvalidArgVal/SettingsInvalid", "The requested settings are incorrect")
                    pending["ir_cut_filter"] = mode
                elif name in ("Exposure", "WhiteBalance"):
                    mode = _text(elem, "Mode")
                    if mode not in ("AUTO", "MANUAL"):
                        return _fault("Sender", "InvalidArgVal/SettingsInvalid", "The requested settings are incorrect")
                    pending["exposure_mode" if name == "Exposure" else "wb_mode"] = mode
                elif name == "Focus":
                    mode = _text(elem, "AutoFocusMode")
                    if mode not in ("AUTO", "MANUAL"):
                        return _fault("Sender", "InvalidArgVal/SettingsInvalid", "The requested settings are incorrect")
                    pending["focus_mode"] = mode
                elif name in ("BacklightCompensation", "WideDynamicRange"):
                    mode = _text(elem, "Mode")
                    if mode not in ("ON", "OFF"):
                        return _fault("Sender", "InvalidArgVal/SettingsInvalid", "The requested settings are incorrect")
                    pending["backlight" if name == "BacklightCompensation" else "wdr"] = mode
            for key, value in pending.items():
                self.set_state(key, value)
            self.set_state("imaging_preset", "")
            return _ok("timg", op)
        if op == "GetOptions":
            rng = "<tt:Min>0</tt:Min><tt:Max>100</tt:Max>"
            return _ok("timg", op,
                "<timg:ImagingOptions>"
                "<tt:BacklightCompensation><tt:Mode>OFF</tt:Mode><tt:Mode>ON</tt:Mode><tt:Level><tt:Min>0</tt:Min><tt:Max>1</tt:Max></tt:Level></tt:BacklightCompensation>"
                f"<tt:Brightness>{rng}</tt:Brightness><tt:ColorSaturation>{rng}</tt:ColorSaturation><tt:Contrast>{rng}</tt:Contrast>"
                "<tt:Exposure><tt:Mode>AUTO</tt:Mode><tt:Mode>MANUAL</tt:Mode></tt:Exposure>"
                "<tt:Focus><tt:AutoFocusModes>AUTO</tt:AutoFocusModes><tt:AutoFocusModes>MANUAL</tt:AutoFocusModes></tt:Focus>"
                "<tt:IrCutFilterModes>ON</tt:IrCutFilterModes><tt:IrCutFilterModes>OFF</tt:IrCutFilterModes><tt:IrCutFilterModes>AUTO</tt:IrCutFilterModes>"
                f"<tt:Sharpness>{rng}</tt:Sharpness>"
                "<tt:WideDynamicRange><tt:Mode>OFF</tt:Mode><tt:Mode>ON</tt:Mode></tt:WideDynamicRange>"
                "<tt:WhiteBalance><tt:Mode>AUTO</tt:Mode><tt:Mode>MANUAL</tt:Mode></tt:WhiteBalance>"
                "</timg:ImagingOptions>")
        if op == "Move":
            focus = _child(req, "Focus")
            cont = _child(focus, "Continuous") if focus is not None else None
            absolute = _child(focus, "Absolute") if focus is not None else None
            relative = _child(focus, "Relative") if focus is not None else None
            if cont is not None:
                speed = float(_text(cont, "Speed") or 0)
                if not -1 <= speed <= 1:
                    return _fault("Sender", "InvalidArgVal/SettingsInvalid", "Speed out of range")
                self.set_state("focus_velocity", speed)
            elif absolute is not None:
                position = float(_text(absolute, "Position") or 0)
                if not 0 <= position <= 1:
                    return _fault("Sender", "InvalidArgVal/SettingsInvalid", "Position out of range")
                self.set_state("focus_velocity", 0.0)
                self.set_state("focus_position", position)
            elif relative is not None:
                distance = float(_text(relative, "Distance") or 0)
                self.set_state("focus_velocity", 0.0)
                self.set_state("focus_position", round(_clamp(self.get_state("focus_position") + distance, 0, 1), 4))
            else:
                return _fault("Sender", "InvalidArgVal/SettingsInvalid", "No focus move given")
            self.set_state("focus_mode", "MANUAL")
            return _ok("timg", op)
        if op == "Stop":
            self.set_state("focus_velocity", 0.0)
            return _ok("timg", op)
        if op == "GetStatus":
            moving = "MOVING" if self.get_state("focus_velocity") else "IDLE"
            return _ok("timg", op,
                f"<timg:Status><tt:FocusStatus20><tt:Position>{self.get_state('focus_position')}</tt:Position>"
                f"<tt:MoveStatus>{moving}</tt:MoveStatus><tt:Error></tt:Error></tt:FocusStatus20></timg:Status>")
        if op == "GetMoveOptions":
            return _ok("timg", op,
                "<timg:MoveOptions><tt:Absolute><tt:Position><tt:Min>0</tt:Min><tt:Max>1</tt:Max></tt:Position>"
                "<tt:Speed><tt:Min>0</tt:Min><tt:Max>1</tt:Max></tt:Speed></tt:Absolute>"
                "<tt:Relative><tt:Distance><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:Distance>"
                "<tt:Speed><tt:Min>0</tt:Min><tt:Max>1</tt:Max></tt:Speed></tt:Relative>"
                "<tt:Continuous><tt:Speed><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:Speed></tt:Continuous></timg:MoveOptions>")
        if op == "GetPresets":
            return _ok("timg", op, "".join(
                f'<timg:Preset token="{t}" type="{kind}"><tt:Name>{name}</tt:Name></timg:Preset>'
                for t, (name, kind) in IMAGING_PRESETS.items()
            ))
        if op == "GetCurrentPreset":
            current = self.get_state("imaging_preset")
            if current in IMAGING_PRESETS:
                name, kind = IMAGING_PRESETS[current]
                return _ok("timg", op, f'<timg:CurrentPreset token="{current}" type="{kind}"><tt:Name>{name}</tt:Name></timg:CurrentPreset>')
            return _ok("timg", op)
        if op == "SetCurrentPreset":
            token = _text(req, "PresetToken")
            if token not in IMAGING_PRESETS:
                return _fault("Sender", "InvalidArgVal/NoToken", "The requested preset token does not exist")
            self.set_state("imaging_preset", token)
            self.set_state("ir_cut_filter", "OFF" if token == "indoor" else "AUTO")
            return _ok("timg", op)
        return _fault("Receiver", "ActionNotSupported", f"{op} is not supported")

    # ── Device IO ──

    def _handle_deviceio(self, op: str, req):
        if op == "GetDigitalInputs":
            return _ok("tmd", op, f'<tmd:DigitalInputs token="{INPUT_TOKEN}" IdleState="closed"/>')
        if op == "GetRelayOutputs":
            return _ok("tmd", op,
                f'<tmd:RelayOutputs token="{RELAY_TOKEN}"><tt:Properties><tt:Mode>Bistable</tt:Mode>'
                "<tt:DelayTime>PT0S</tt:DelayTime><tt:IdleState>open</tt:IdleState></tt:Properties></tmd:RelayOutputs>")
        return _fault("Receiver", "ActionNotSupported", f"{op} is not supported")

    # ── Events ──

    def _handle_events(self, op: str, req, base: str):
        if not self._events:
            return _fault("Receiver", "ActionNotSupported", "Events are not supported")
        if op == "CreatePullPointSubscription":
            wanted = _duration_s(_text(req, "InitialTerminationTime")) or 60.0
            term = min(wanted, self._max_term_s)
            self._next_pullpoint += 1
            path = f"{PULLPOINT_PREFIX}{self._next_pullpoint}"
            pp = _PullPoint(path, self._now() + timedelta(seconds=term))
            for key in PROPERTY_EVENTS:
                pp.push(self._property_message(key, "Initialized"))
            self._pullpoints[path] = pp
            self.subscribe_count += 1
            self.set_state("subscriptions", len(self._pullpoints))
            return _ok("tev", op,
                f"<tev:SubscriptionReference><wsa:Address>{base}{path}</wsa:Address>"
                f'<wsa:ReferenceParameters><sim:SubscriptionId xmlns:sim="{NS_SIM}">{self._next_pullpoint}</sim:SubscriptionId>'
                "</wsa:ReferenceParameters></tev:SubscriptionReference>"
                f"<wsnt:CurrentTime>{_fmt_time(self._now())}</wsnt:CurrentTime>"
                f"<wsnt:TerminationTime>{_fmt_time(pp.ends)}</wsnt:TerminationTime>")
        if op == "GetEventProperties":
            return _ok("tev", op, "<tev:TopicNamespaceLocation>http://www.onvif.org/onvif/ver10/topics/topicns.xml</tev:TopicNamespaceLocation>")
        return _fault("Receiver", "ActionNotSupported", f"{op} is not supported")

    def _handle_pullpoint(self, path: str, root, req, op: str):
        pp = self._pullpoints.get(path)
        if pp is None or pp.ends < self._now():
            self._pullpoints.pop(path, None)
            self.set_state("subscriptions", len(self._pullpoints))
            return "response", _fault("Receiver", "ResourceUnknownFault", "The pull point reference is invalid")
        if self._require_reference_params:
            header = _child(root, "Header")
            sub_id = _descendant(header, "SubscriptionId") if header is not None else None
            if sub_id is None or (sub_id.text or "").strip() != path[len(PULLPOINT_PREFIX):]:
                return "response", _fault("Receiver", "ResourceUnknownFault", "The pull point reference is invalid")
        if op == "PullMessages":
            timeout = _duration_s(_text(req, "Timeout")) or 1.0
            try:
                limit = int(_text(req, "MessageLimit") or 10)
            except ValueError:
                limit = 10
            return "pull", (pp, max(1, min(limit, 100)), min(timeout, 60.0))
        if op == "Renew":
            wanted = _duration_s(_text(req, "TerminationTime")) or 60.0
            pp.ends = self._now() + timedelta(seconds=min(wanted, self._max_term_s))
            self.renew_count += 1
            return "response", _ok("wsnt", op,
                f"<wsnt:TerminationTime>{_fmt_time(pp.ends)}</wsnt:TerminationTime>"
                f"<wsnt:CurrentTime>{_fmt_time(self._now())}</wsnt:CurrentTime>")
        if op == "Unsubscribe":
            self._pullpoints.pop(path, None)
            self.unsubscribe_count += 1
            self.set_state("subscriptions", len(self._pullpoints))
            return "response", _ok("wsnt", op)
        return "response", _fault("Receiver", "ActionNotSupported", f"{op} is not supported on a pull point")

    def _pull_response(self, pp: _PullPoint, limit: int) -> tuple[int, str]:
        messages = pp.drain(limit)
        return _ok("tev", "PullMessages",
            f"<tev:CurrentTime>{_fmt_time(self._now())}</tev:CurrentTime>"
            f"<tev:TerminationTime>{_fmt_time(pp.ends)}</tev:TerminationTime>"
            + "".join(messages))
