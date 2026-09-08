"""
OpenAVC Generic ONVIF Camera Driver.

Controls any camera or video encoder that implements the ONVIF Profile S / T
service set over SOAP: device management, media (Media 1 or Media 2), PTZ,
imaging, relay outputs, digital inputs and the event service. One driver
covers cameras from any vendor; the device's own manufacturer and model are
read back and published at connect.

Why Python
----------
ONVIF is SOAP over HTTP with WS-Security UsernameToken authentication (a
per-request SHA-1 digest of nonce + timestamp + password), HTTP Digest as the
alternative, service addresses that the device announces at runtime
(GetServices), media profiles enumerated from the device, and a pull-point
event subscription that has to be created, pulled in a long-poll loop, renewed
and released. None of that fits the declarative ``.avcdriver`` request/
response model or any of the four ``push:`` shapes, so this is a Python driver
that owns its own httpx session.

Push vs poll
------------
Hybrid. The event service's real-time pull-point interface (Core spec 9.1) is
the push channel: the driver creates a pull point, blocks on PullMessages and
reacts to property events (digital inputs, relay outputs, motion, tampering,
signal loss, PTZ preset reached). Every ONVIF device must implement it. PTZ
position, move status and focus status have no event and are polled
(``poll_interval``, default 5 s); imaging settings and the preset list are
re-read on a slower cadence inside the same loop.

Authentication
--------------
Core spec 5.9.1: a device is protected with HTTP Digest, with WS-UsernameToken
kept for legacy devices, and a client should never send both. Most cameras in
the field accept the UsernameToken, so the driver sends it first; a 401 means
the device only speaks HTTP Digest and the driver switches the session to that
and retries. The UsernameToken timestamp is generated in the DEVICE's clock:
GetSystemDateAndTime is read unauthenticated at connect (it is PRE_AUTH by
spec) and the offset is applied, so a camera whose clock is minutes off still
accepts the login. The offset is published as ``clock_offset_s`` because a
badly wrong clock is the commonest reason an ONVIF login fails.

Stream credentials
------------------
The stream URLs the camera returns carry no login, and the spec says a device
should authenticate RTSP with the same credentials. The published preview URLs
are bare by default so the password never enters state (state is shown in the
IDE and relayed to a paired cloud account); ``credentials_in_stream_url``
embeds them for a room that wants the Video Panel to play the stream directly.

Sources (all public, from ONVIF and OASIS):
  ONVIF Core Specification v26.06
    https://www.onvif.org/specs/core/ONVIF-Core-Specification.pdf
  ONVIF Media2 / Media / PTZ / Imaging / Device IO / Analytics Service Specs
    https://www.onvif.org/specs/srv/media/ONVIF-Media2-Service-Spec.pdf
    https://www.onvif.org/specs/srv/media/ONVIF-Media-Service-Spec.pdf
    https://www.onvif.org/specs/srv/ptz/ONVIF-PTZ-Service-Spec.pdf
    https://www.onvif.org/specs/srv/img/ONVIF-Imaging-Service-Spec.pdf
    https://www.onvif.org/specs/srv/io/ONVIF-DeviceIo-Service-Spec.pdf
    https://www.onvif.org/specs/srv/analytics/ONVIF-Analytics-Service-Spec.pdf
  ONVIF WSDL / XSD (element names and order)
    https://www.onvif.org/ver10/schema/onvif.xsd and common.xsd
  OASIS WSS UsernameToken Profile 1.1 (the password digest)
    http://docs.oasis-open.org/wss/v1.1/wss-v1.1-spec-os-UsernameTokenProfile.pdf
  OASIS WS-BaseNotification 1.3 (Renew / Unsubscribe)
    http://docs.oasis-open.org/wsn/wsn-ws_base_notification-1.3-spec-os.pdf
All are archived under driver-roadmap/reference-docs/onvif/.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
from xml.sax.saxutils import escape as _xml_escape

import httpx
from defusedxml.ElementTree import ParseError as _XMLParseError
from defusedxml.ElementTree import fromstring as _xml_fromstring

from openavc.drivers.base import (
    BaseDriver,
    ConnectionFaultError,
    DeviceSettingValueError,
)
from openavc.utils.logger import get_logger

log = get_logger(__name__)


# ── Namespaces (ONVIF WSDL / XSD, OASIS WSS and WSN) ──

NS_SOAP = "http://www.w3.org/2003/05/soap-envelope"
NS_WSSE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-wssecurity-secext-1.0.xsd"
)
NS_WSU = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-wssecurity-utility-1.0.xsd"
)
PASSWORD_DIGEST_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)
NONCE_ENCODING_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-soap-message-security-1.0#Base64Binary"
)
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

# WS-Addressing actions the event service requires (event.wsdl wsaw:Action,
# WS-BaseNotification 1.3 for the subscription manager).
ACTION_CREATE_PULLPOINT = (
    f"{NS_TEV}/EventPortType/CreatePullPointSubscriptionRequest"
)
ACTION_PULL_MESSAGES = f"{NS_TEV}/PullPointSubscription/PullMessagesRequest"
ACTION_RENEW = "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/RenewRequest"
ACTION_UNSUBSCRIBE = (
    "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/UnsubscribeRequest"
)
TOPIC_DIALECT_CONCRETE_SET = (
    "http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet"
)

# Service keys -> the WSDL namespace each announces in GetServices.
SERVICE_NAMESPACES = {
    "device": NS_TDS,
    "media": NS_TRT,
    "media2": NS_TR2,
    "ptz": NS_TPTZ,
    "imaging": NS_TIMG,
    "events": NS_TEV,
    "deviceio": NS_TMD,
}

# Generic PTZ spaces (PTZ spec 5.7). Every PTZ node must provide the generic
# velocity spaces; the position and translation ones whenever the movement
# kind is supported at all. Commands omit the space attribute and rely on the
# PTZ configuration's defaults, which are these on every camera seen so far;
# the spaces are named here for the readback check in _parse_ptz_status.
SPACE_PANTILT_POSITION = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace"
SPACE_ZOOM_POSITION = "http://www.onvif.org/ver10/tptz/ZoomSpaces/PositionGenericSpace"

# Pull-point cadence. The device must honour a PullMessages timeout of at
# least a minute; 10 s keeps a dead pull point noticed quickly while still
# being one request every ten seconds on an idle camera.
PULL_TIMEOUT_S = 10
PULL_MESSAGE_LIMIT = 100
SUBSCRIPTION_TERM_S = 120
RENEW_BELOW_S = 45
EVENT_RETRY_MIN_S = 5.0
EVENT_RETRY_MAX_S = 60.0

# Slow-cadence refresh inside poll(): imaging settings, preset lists.
SLOW_POLL_EVERY = 12

# Event topics (Device IO 5.10, Imaging 5.5, PTZ 5.11, Analytics Annex B).
TOPIC_DIGITAL_INPUT = "Device/Trigger/DigitalInput"
TOPIC_RELAY = "Device/Trigger/Relay"
TOPIC_MOTION = "VideoSource/MotionAlarm"
TOPIC_CELL_MOTION = "RuleEngine/CellMotionDetector/Motion"
TOPIC_SIGNAL_LOSS = "VideoSource/SignalLoss"
TOPIC_PRESET_PREFIX = "PTZController/PTZPresets/"
TAMPER_KINDS = {
    "ImageTooBlurry": "blurry",
    "ImageTooDark": "dark",
    "ImageTooBright": "bright",
    "GlobalSceneChange": "scene change",
}

# Imaging settings the driver reads and writes, in the ImagingSettings20
# schema order (the order the element is serialised in must match the XSD
# sequence or a strict device refuses the write).
IMAGING_ORDER = (
    "BacklightCompensation",
    "Brightness",
    "ColorSaturation",
    "Contrast",
    "Exposure",
    "Focus",
    "IrCutFilter",
    "Sharpness",
    "WideDynamicRange",
    "WhiteBalance",
)
# Sub-element order for the structured settings.
IMAGING_CHILD_ORDER = {
    "BacklightCompensation": ("Mode", "Level"),
    "Exposure": (
        "Mode", "Priority", "MinExposureTime", "MaxExposureTime", "MinGain",
        "MaxGain", "MinIris", "MaxIris", "ExposureTime", "Gain", "Iris",
    ),
    "Focus": ("AutoFocusMode", "DefaultSpeed", "NearLimit", "FarLimit"),
    "WideDynamicRange": ("Mode", "Level"),
    "WhiteBalance": ("Mode", "CrGain", "CbGain"),
}
# Device-setting key -> (imaging element, sub-element or None).
SETTING_PATHS = {
    "exposure_mode": ("Exposure", "Mode"),
    "wb_mode": ("WhiteBalance", "Mode"),
    "focus_mode": ("Focus", "AutoFocusMode"),
    "ir_cut_filter": ("IrCutFilter", None),
    "backlight_compensation": ("BacklightCompensation", "Mode"),
    "wide_dynamic_range": ("WideDynamicRange", "Mode"),
    "brightness": ("Brightness", None),
    "contrast": ("Contrast", None),
    "color_saturation": ("ColorSaturation", None),
    "sharpness": ("Sharpness", None),
}
LEVEL_SETTINGS = ("brightness", "contrast", "color_saturation", "sharpness")

_CHILD_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


class OnvifFault(Exception):
    """A SOAP fault the device answered with, carrying its subcode and reason.

    ``code`` is the innermost ``ter:`` subcode without its prefix
    (``NotAuthorized``, ``NoProfile``, ``InvalidPosition`` ...), or the HTTP
    status when the device answered with no fault body at all.
    """

    def __init__(self, code: str, reason: str, *, http_status: int = 0):
        self.code = code
        self.reason = reason
        self.http_status = http_status
        text = reason or code or f"HTTP {http_status}"
        super().__init__(text)

    @property
    def not_authorized(self) -> bool:
        return self.code == "NotAuthorized" or self.http_status in (401, 403)


class OnvifCommandError(Exception):
    """A command the camera refused, worded for the person who pressed it."""


# ── XML helpers (namespace-agnostic: firmware prefix choices vary) ──


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _children(elem, name: str) -> list:
    return [c for c in elem if _local(c.tag) == name]


def _child(elem, *path: str):
    """First child matched step by step through ``path`` by local name."""
    node = elem
    for step in path:
        if node is None:
            return None
        node = next((c for c in node if _local(c.tag) == step), None)
    return node


def _descendant(elem, name: str):
    """First descendant (any depth) with the local name."""
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


def _float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _bool_text(value: str) -> bool:
    return str(value).strip().lower() in ("true", "1")


def _parse_xs_datetime(text: str) -> datetime | None:
    """xs:dateTime (``2026-09-08T12:34:56.789Z`` or with an offset) -> aware UTC."""
    text = (text or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_xs_duration_seconds(text: str) -> float | None:
    """A relative xs:duration (``PT10S``, ``PT1M30S``, ``PT0.5S``) in seconds.

    Day and larger units are accepted; a duration with a year or month part is
    not exact and answers None.
    """
    m = re.fullmatch(
        r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?",
        (text or "").strip(),
    )
    if not m or m.group(0) in ("P", "PT"):
        return None
    days, hours, minutes, seconds = m.groups()
    return (
        float(days or 0) * 86400
        + float(hours or 0) * 3600
        + float(minutes or 0) * 60
        + float(seconds or 0)
    )


def _format_datetime(value: datetime) -> str:
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _child_id_for(token: str) -> str:
    """A device token as a child local id: the platform's string ids allow
    letters, digits, '_' and '-' only, so anything else becomes '_'. The
    original token stays in the child's state and in the driver's maps."""
    cleaned = _CHILD_ID_RE.sub("_", token.strip())
    return cleaned or "_"


def _with_credentials(url: str, username: str, password: str) -> str:
    """Embed a login in a URL's authority (``rtsp://user:pass@host/...``)."""
    if not url or not username:
        return url
    parts = urlsplit(url)
    if "@" in parts.netloc:
        return url
    cred = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    return urlunsplit(parts._replace(netloc=cred + parts.netloc))


def _wsse_header(username: str, password: str, device_now: datetime) -> str:
    """A WS-Security UsernameToken with PasswordDigest (WSS UsernameToken
    Profile 1.1 §3.1): Base64(SHA-1(nonce + created + password)), the nonce
    hashed as raw octets and carried Base64-encoded."""
    nonce = os.urandom(16)
    created = _format_datetime(device_now)
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode("utf-8") + password.encode("utf-8")).digest()
    ).decode("ascii")
    return (
        f'<wsse:Security xmlns:wsse="{NS_WSSE}" xmlns:wsu="{NS_WSU}">'
        "<wsse:UsernameToken>"
        f"<wsse:Username>{_xml_escape(username)}</wsse:Username>"
        f'<wsse:Password Type="{PASSWORD_DIGEST_TYPE}">{digest}</wsse:Password>'
        f'<wsse:Nonce EncodingType="{NONCE_ENCODING_TYPE}">'
        f"{base64.b64encode(nonce).decode('ascii')}</wsse:Nonce>"
        f"<wsu:Created>{created}</wsu:Created>"
        "</wsse:UsernameToken></wsse:Security>"
    )


def _envelope(body: str, header: str = "") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{NS_SOAP}">'
        f"<s:Header>{header}</s:Header>"
        f"<s:Body>{body}</s:Body></s:Envelope>"
    ).encode("utf-8")


def _parse_fault(root) -> tuple[str, str] | None:
    """(innermost subcode without prefix, reason) for a SOAP 1.2 fault body,
    or None when the envelope carries no Fault."""
    fault = _descendant(root, "Fault")
    if fault is None:
        return None
    code_value = ""
    node = _child(fault, "Code")
    while node is not None:
        value = _text(node, "Value")
        if value:
            code_value = value
        node = _child(node, "Subcode")
    reason = _text(fault, "Reason", "Text")
    # Some firmware puts the useful sentence in Detail instead of Reason.
    if not reason:
        detail = _child(fault, "Detail")
        if detail is not None:
            reason = " ".join(t.strip() for t in detail.itertext() if t.strip())
    code = code_value.split(":", 1)[-1] if code_value else ""
    return code, reason


def _vector_xml(pan: float | None, tilt: float | None, zoom: float | None) -> str:
    """A tt:PTZVector / tt:PTZSpeed body: PanTilt then Zoom, each optional."""
    out = ""
    if pan is not None and tilt is not None:
        out += f'<tt:PanTilt xmlns:tt="{NS_TT}" x="{pan:.4f}" y="{tilt:.4f}"/>'
    if zoom is not None:
        out += f'<tt:Zoom xmlns:tt="{NS_TT}" x="{zoom:.4f}"/>'
    return out


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class _PtzDirection:
    """The eight joystick directions as (pan, tilt) unit vectors."""

    TABLE = {
        "pt_up": (0.0, 1.0),
        "pt_down": (0.0, -1.0),
        "pt_left": (-1.0, 0.0),
        "pt_right": (1.0, 0.0),
        "pt_up_left": (-1.0, 1.0),
        "pt_up_right": (1.0, 1.0),
        "pt_down_left": (-1.0, -1.0),
        "pt_down_right": (1.0, -1.0),
    }


def _speed_param(params: dict[str, Any], default: float = 0.5) -> float:
    return _clamp(_float(params.get("speed"), default) or default, 0.01, 1.0)



class OnvifCameraDriver(BaseDriver):
    """Generic ONVIF camera: SOAP control with pull-point events."""

    DRIVER_INFO = {
        "id": "onvif_camera",
        "name": "ONVIF Camera",
        "manufacturer": "ONVIF",
        "category": "camera",
        "version": "2.0.4",
        "author": "OpenAVC",
        "description": (
            "Controls any ONVIF Profile S or Profile T camera or video encoder: "
            "pan, tilt, zoom and focus, presets and home, image settings, relay "
            "outputs and digital inputs, plus the camera's RTSP stream and "
            "snapshot addresses for the Video Panel. Motion, tamper, signal-loss "
            "and input changes arrive as events. Use a brand's own driver when "
            "the catalog has one; this one covers everything else."
        ),
        "source_url": "https://www.onvif.org/specs/core/ONVIF-Core-Specification.pdf",
        "tags": ["onvif", "ptz", "camera", "rtsp", "profile-s", "profile-t"],
        "verified": False,
        "simulated": True,
        "protocols": ["onvif"],
        "ports": [80],
        "transport": "http",
        "discovery": {
            # The sibling onvif_camera_discovery.py answers the WS-Discovery
            # multicast probe. cross_vendor: a brand driver that matches the
            # camera by manufacturer or OUI is offered first and this generic
            # one second.
            "python": {
                "file": "./onvif_camera_discovery.py",
                "cross_vendor": True,
            },
        },
        "compatible_models": [
            {
                "manufacturer": "Axis",
                "models": ["P3265-V"],
                "confidence": "partial",
                "notes": (
                    "Bench-tested on AXIS OS 10.12: identity, three media profiles "
                    "with stream and snapshot addresses, imaging levels and wide "
                    "dynamic range, the relay output and the digital input, and the "
                    "relay and input events. The camera exposes no PTZ over ONVIF and "
                    "reports no focus move options, so its remote zoom and focus are "
                    "not reachable through this driver. Create an ONVIF account on "
                    "the camera first; Axis keeps them separate from web accounts."
                ),
            },
            {
                "manufacturer": "Any",
                "models": [
                    "Any ONVIF Profile S or Profile T conformant camera or encoder",
                ],
                "confidence": "untested",
                "notes": (
                    "Built from the ONVIF Core, Media, Media2, PTZ, Imaging and "
                    "Device IO specifications and verified against the "
                    "simulator. Cameras differ in which services they offer; the "
                    "driver reads the device's own service list and shows only "
                    "what it has."
                ),
            },
        ],
        "help": {
            "overview": (
                "ONVIF is the cross-vendor camera control standard. Turn ONVIF on "
                "in the camera's web interface, give it an ONVIF user, and add "
                "the camera here with that login. The driver reads the camera's "
                "make and model, its media profiles (each with a stream address "
                "the Video Panel can show), and whatever PTZ, focus, imaging and "
                "relay control the camera offers."
            ),
            "setup": (
                "1. In the camera's web interface enable ONVIF and create an "
                "ONVIF user with administrator or operator rights. Some brands "
                "keep ONVIF users separate from web users.\n"
                "2. Check the camera's date and time. A clock more than a few "
                "minutes wrong makes the camera refuse every login; the driver "
                "compensates, but only once it can read the clock.\n"
                "3. Add the camera with its IP address, the ONVIF port (80 on "
                "most cameras, 8080 or 8000 on some) and the ONVIF login.\n"
                "4. Under Advanced, pick a media profile if the camera has more "
                "than one and you want PTZ bound to a specific one; the default "
                "is the first profile with PTZ.\n"
                "5. To let the Video Panel play the camera's stream without "
                "adding it by hand, turn on 'Login in stream address' under "
                "Advanced and read the note beside it first."
            ),
            "connection": (
                "Enable ONVIF on the camera and use an ONVIF user, not the web "
                "login, if the camera keeps them separate. If the login is "
                "right and the camera still refuses, set the camera's clock."
            ),
        },
        "default_config": {
            "host": "",
            "port": 80,
            "ssl": False,
            "verify_ssl": False,
            "username": "",
            "password": "",
            "service_path": "/onvif/device_service",
            "profile_token": "",
            "stream_transport": "rtsp",
            "events": True,
            "credentials_in_stream_url": False,
            "poll_interval": 5,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {
                "type": "integer",
                "required": True,
                "default": 80,
                "label": "ONVIF Port",
                "help": "The port the camera's ONVIF service listens on. 80 on most cameras.",
            },
            "username": {
                "type": "string",
                "label": "Username",
                "help": "An ONVIF user on the camera. Leave blank only if the camera allows unauthenticated control.",
            },
            "password": {"type": "string", "label": "Password", "secret": True},
            "ssl": {
                "type": "boolean",
                "label": "Use HTTPS",
                "default": False,
                "advanced": True,
                "help": "Talk to the ONVIF service over HTTPS. Most cameras serve it over plain HTTP.",
            },
            "verify_ssl": {
                "type": "boolean",
                "label": "Verify Certificate",
                "default": False,
                "advanced": True,
                "help": "Only for HTTPS. Off for the self-signed certificate almost every camera ships with.",
            },
            "service_path": {
                "type": "string",
                "label": "Device Service Path",
                "default": "/onvif/device_service",
                "advanced": True,
                "help": "The path of the ONVIF device service. Change it only if the camera's documentation gives a different one.",
            },
            "profile_token": {
                "type": "string",
                "label": "Control Profile",
                "default": "",
                "advanced": True,
                "help": "The media profile PTZ, focus and imaging control is bound to. Blank picks the first profile with PTZ, else the first profile. The profile tokens are listed under Media Profiles once connected.",
            },
            "stream_transport": {
                "type": "enum",
                "label": "Stream Transport",
                "default": "rtsp",
                "advanced": True,
                "values": [
                    {"value": "rtsp", "label": "RTSP over TCP"},
                    {"value": "udp", "label": "RTP unicast over UDP"},
                    {"value": "http", "label": "RTSP tunnelled over HTTP"},
                ],
                "help": "How the stream address the camera hands out should be set up. RTSP over TCP is right for almost every network.",
            },
            "events": {
                "type": "boolean",
                "label": "Subscribe to Events",
                "default": True,
                "advanced": True,
                "help": "Keep a pull-point subscription open so motion, tamper, input and relay changes arrive at once. Turn off only for a camera whose event service misbehaves.",
            },
            "credentials_in_stream_url": {
                "type": "boolean",
                "label": "Login in Stream Address",
                "default": False,
                "advanced": True,
                "help": "Embed the ONVIF login in the published stream and snapshot addresses so the Video Panel can play them without adding the stream by hand. The address, login included, is then visible in Live State and to a paired cloud account. Off keeps the login out of state; add the stream under Video Streams with its login instead.",
            },
            "poll_interval": {
                "type": "integer",
                "label": "Poll Interval (s)",
                "default": 5,
                "min": 1,
                "max": 300,
                "advanced": True,
                "help": "How often PTZ position and focus are read back. Events do not depend on it.",
            },
        },
        "state_variables": {
            "manufacturer": {"type": "string", "label": "Manufacturer"},
            "model": {"type": "string", "label": "Model"},
            "firmware_version": {"type": "string", "label": "Firmware Version"},
            "serial_number": {"type": "string", "label": "Serial Number"},
            "hardware_id": {"type": "string", "label": "Hardware ID"},
            "profile_token": {"type": "string", "label": "Control Profile Token",
                              "help": "The media profile PTZ, focus and imaging act on."},
            "profile_name": {"type": "string", "label": "Control Profile Name"},
            "preview_url": {"type": "string", "label": "Stream URL",
                            "help": "The control profile's stream address. The Video Panel lists it as a source."},
            "preview_format": {"type": "string", "label": "Stream Format"},
            "snapshot_url": {"type": "string", "label": "Snapshot URL",
                             "help": "A JPEG of the current picture, fetched with an HTTP GET."},
            "ptz_supported": {"type": "boolean", "label": "PTZ Supported"},
            "focus_supported": {"type": "boolean", "label": "Focus Control Supported",
                                "help": "True when the camera offers remote focus over its imaging service."},
            "pan_position": {"type": "number", "label": "Pan Position", "min": -1.0, "max": 1.0,
                             "step": 0.01, "control": True,
                             "help": "-1 (full left) to 1 (full right) in the camera's generic space."},
            "tilt_position": {"type": "number", "label": "Tilt Position", "min": -1.0, "max": 1.0,
                              "step": 0.01, "control": True,
                              "help": "-1 (full down) to 1 (full up) in the camera's generic space."},
            "zoom_position": {"type": "number", "label": "Zoom Position", "min": 0.0, "max": 1.0,
                              "step": 0.01, "control": True,
                              "help": "0 (wide) to 1 (tele) in the camera's generic space."},
            "move_status": {"type": "enum", "label": "Move Status",
                            "values": ["idle", "moving", "unknown"]},
            "home_supported": {"type": "boolean", "label": "Home Position Supported"},
            "preset_count": {"type": "integer", "label": "Preset Count"},
            "preset_options": {"type": "string", "label": "Preset List",
                               "help": "The camera's PTZ presets, for the preset pickers."},
            "preset_status": {"type": "enum", "label": "Preset Status",
                              "values": ["invoked", "reached", "aborted", "left", "none"],
                              "help": "Reported by the camera as a preset recall progresses."},
            "preset_last": {"type": "string", "label": "Last Preset Token"},
            "aux_command_options": {"type": "string", "label": "Auxiliary Command List"},
            "focus_position": {"type": "number", "label": "Focus Position", "control": True},
            "focus_move_status": {"type": "enum", "label": "Focus Move Status",
                                  "values": ["idle", "moving", "unknown"]},
            "focus_mode": {"type": "enum", "label": "Focus Mode", "values": ["auto", "manual"],
                           "control": True},
            "exposure_mode": {"type": "enum", "label": "Exposure Mode",
                              "values": ["auto", "manual"], "control": True},
            "wb_mode": {"type": "enum", "label": "White Balance Mode",
                        "values": ["auto", "manual"], "control": True},
            "ir_cut_filter": {"type": "enum", "label": "IR Cut Filter",
                              "values": ["on", "off", "auto"], "control": True,
                              "help": "on is day mode, off is night mode, auto lets the camera decide."},
            "backlight_compensation": {"type": "boolean", "label": "Backlight Compensation",
                                       "control": True},
            "wide_dynamic_range": {"type": "boolean", "label": "Wide Dynamic Range",
                                   "control": True},
            "brightness": {"type": "number", "label": "Brightness", "control": True,
                           "help": "In the camera's own range; see brightness_range."},
            "contrast": {"type": "number", "label": "Contrast", "control": True},
            "color_saturation": {"type": "number", "label": "Color Saturation", "control": True},
            "sharpness": {"type": "number", "label": "Sharpness", "control": True},
            "brightness_range": {"type": "string", "label": "Brightness Range",
                                 "help": "The camera's own min..max for brightness."},
            "contrast_range": {"type": "string", "label": "Contrast Range"},
            "color_saturation_range": {"type": "string", "label": "Color Saturation Range"},
            "sharpness_range": {"type": "string", "label": "Sharpness Range"},
            "imaging_preset": {"type": "string", "label": "Imaging Preset",
                               "help": "The manufacturer scene preset in effect, when the camera offers them."},
            "imaging_preset_options": {"type": "string", "label": "Imaging Preset List"},
            "motion": {"type": "boolean", "label": "Motion Detected"},
            "tamper": {"type": "boolean", "label": "Tamper Alarm",
                       "help": "The camera reports its picture too blurry, dark or bright, or the scene changed."},
            "tamper_reason": {"type": "string", "label": "Tamper Reason"},
            "signal_loss": {"type": "boolean", "label": "Video Signal Loss"},
            "events_active": {"type": "boolean", "label": "Event Subscription Active",
                              "help": "True while the pull-point subscription is open and delivering."},
            "clock_offset_s": {"type": "number", "label": "Camera Clock Offset", "unit": "s",
                               "help": "Camera clock minus this system's clock. A large value is why an otherwise-correct login gets refused."},
            "last_error": {"type": "string", "label": "Last Error"},
        },
        "child_entity_types": {
            "profile": {
                "label": "Media Profile",
                "label_plural": "Media Profiles",
                "id_format": {"type": "string"},
                "label_field": "name",
                "summary_fields": ["name", "encoding", "resolution", "has_ptz"],
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "token": {"type": "string", "label": "Token"},
                    "encoding": {"type": "string", "label": "Encoding"},
                    "resolution": {"type": "string", "label": "Resolution"},
                    "framerate": {"type": "number", "label": "Frame Rate", "unit": "fps"},
                    "has_ptz": {"type": "boolean", "label": "PTZ"},
                    "video_source": {"type": "string", "label": "Video Source Token"},
                    "preview_url": {"type": "string", "label": "Stream URL"},
                    "preview_format": {"type": "string", "label": "Stream Format"},
                    "snapshot_url": {"type": "string", "label": "Snapshot URL"},
                },
            },
            "relay": {
                "label": "Relay Output",
                "label_plural": "Relay Outputs",
                "id_format": {"type": "string"},
                "summary_fields": ["active", "mode", "idle_state"],
                "state_variables": {
                    "token": {"type": "string", "label": "Token"},
                    "active": {"type": "boolean", "label": "Active", "control": True,
                               "help": "Reported by the camera's relay event. Unknown until the camera has sent one."},
                    "mode": {"type": "enum", "label": "Mode", "values": ["bistable", "monostable"]},
                    "idle_state": {"type": "enum", "label": "Idle State", "values": ["open", "closed"]},
                    "delay_time": {"type": "string", "label": "Monostable Delay"},
                },
            },
            "input": {
                "label": "Digital Input",
                "label_plural": "Digital Inputs",
                "id_format": {"type": "string"},
                "summary_fields": ["active"],
                "state_variables": {
                    "token": {"type": "string", "label": "Token"},
                    "active": {"type": "boolean", "label": "Active",
                               "help": "Reported by the camera's digital-input event."},
                },
            },
        },
        "device_settings": {
            "exposure_mode": {
                "type": "enum", "label": "Exposure Mode",
                "values": [{"value": "auto", "label": "Auto"}, {"value": "manual", "label": "Manual"}],
                "state_key": "exposure_mode", "default": "auto", "setup": False,
                "help": "Auto lets the camera set exposure time, gain and iris.",
            },
            "wb_mode": {
                "type": "enum", "label": "White Balance Mode",
                "values": [{"value": "auto", "label": "Auto"}, {"value": "manual", "label": "Manual"}],
                "state_key": "wb_mode", "default": "auto", "setup": False,
            },
            "focus_mode": {
                "type": "enum", "label": "Focus Mode",
                "values": [{"value": "auto", "label": "Auto"}, {"value": "manual", "label": "Manual"}],
                "state_key": "focus_mode", "default": "auto", "setup": False,
                "help": "Manual focus moves only when told to; the focus commands switch to manual by themselves.",
            },
            "ir_cut_filter": {
                "type": "enum", "label": "IR Cut Filter",
                "values": [{"value": "auto", "label": "Auto"}, {"value": "on", "label": "On (day)"},
                           {"value": "off", "label": "Off (night)"}],
                "state_key": "ir_cut_filter", "default": "auto", "setup": False,
            },
            "backlight_compensation": {
                "type": "boolean", "label": "Backlight Compensation",
                "state_key": "backlight_compensation", "default": False, "setup": False,
                "help": "Brightens a subject lit from behind.",
            },
            "wide_dynamic_range": {
                "type": "boolean", "label": "Wide Dynamic Range",
                "state_key": "wide_dynamic_range", "default": False, "setup": False,
            },
            "brightness": {
                "type": "number", "label": "Brightness",
                "state_key": "brightness", "default": 50, "setup": False,
                "help": "In the camera's own range, shown in Brightness Range once connected.",
            },
            "contrast": {
                "type": "number", "label": "Contrast",
                "state_key": "contrast", "default": 50, "setup": False,
            },
            "color_saturation": {
                "type": "number", "label": "Color Saturation",
                "state_key": "color_saturation", "default": 50, "setup": False,
            },
            "sharpness": {
                "type": "number", "label": "Sharpness",
                "state_key": "sharpness", "default": 50, "setup": False,
            },
        },
        # The quick actions hide on a camera that lacks the capability: a fixed
        # dome has no home position to go to and no focus to hand back.
        "actions": [
            {
                "id": "pt_home",
                "kind": "command",
                "command": "pt_home",
                "label": "Go to Home",
                "icon": "house",
                "visible_when": {"key": "device.$id.ptz_supported", "operator": "truthy"},
            },
            {
                "id": "pt_stop",
                "kind": "command",
                "command": "pt_stop",
                "label": "Stop Pan/Tilt/Zoom",
                "icon": "octagon-x",
                "visible_when": {"key": "device.$id.ptz_supported", "operator": "truthy"},
            },
            {
                "id": "focus_auto",
                "kind": "command",
                "command": "focus_auto",
                "label": "Auto Focus",
                "icon": "focus",
                "visible_when": {"key": "device.$id.focus_supported", "operator": "truthy"},
            },
            {
                "id": "reboot",
                "kind": "command",
                "command": "reboot",
                "label": "Reboot Camera",
                "icon": "power",
                "confirm": "Reboot the camera? It will be unreachable for a minute or two.",
            },
        ],
        "commands": {
            # Pan / tilt drive (continuous, generic velocity space)
            "pt_up": {"label": "Tilt Up", "params": {
                "speed": {"type": "number", "label": "Speed", "min": 0.01, "max": 1.0, "default": 0.5,
                          "help": "Fraction of the camera's full speed, 0.01 to 1."},
            },
                      "help": "Start tilting up. Send pt_stop to halt."},
            "pt_down": {"label": "Tilt Down", "params": {
                "speed": {"type": "number", "label": "Speed", "min": 0.01, "max": 1.0, "default": 0.5,
                          "help": "Fraction of the camera's full speed, 0.01 to 1."},
            },
                        "help": "Start tilting down. Send pt_stop to halt."},
            "pt_left": {"label": "Pan Left", "params": {
                "speed": {"type": "number", "label": "Speed", "min": 0.01, "max": 1.0, "default": 0.5,
                          "help": "Fraction of the camera's full speed, 0.01 to 1."},
            },
                        "help": "Start panning left. Send pt_stop to halt."},
            "pt_right": {"label": "Pan Right", "params": {
                "speed": {"type": "number", "label": "Speed", "min": 0.01, "max": 1.0, "default": 0.5,
                          "help": "Fraction of the camera's full speed, 0.01 to 1."},
            },
                         "help": "Start panning right. Send pt_stop to halt."},
            "pt_up_left": {"label": "Pan/Tilt Up-Left", "params": {
                "speed": {"type": "number", "label": "Speed", "min": 0.01, "max": 1.0, "default": 0.5,
                          "help": "Fraction of the camera's full speed, 0.01 to 1."},
            }},
            "pt_up_right": {"label": "Pan/Tilt Up-Right", "params": {
                "speed": {"type": "number", "label": "Speed", "min": 0.01, "max": 1.0, "default": 0.5,
                          "help": "Fraction of the camera's full speed, 0.01 to 1."},
            }},
            "pt_down_left": {"label": "Pan/Tilt Down-Left", "params": {
                "speed": {"type": "number", "label": "Speed", "min": 0.01, "max": 1.0, "default": 0.5,
                          "help": "Fraction of the camera's full speed, 0.01 to 1."},
            }},
            "pt_down_right": {"label": "Pan/Tilt Down-Right", "params": {
                "speed": {"type": "number", "label": "Speed", "min": 0.01, "max": 1.0, "default": 0.5,
                          "help": "Fraction of the camera's full speed, 0.01 to 1."},
            }},
            "pt_drive": {
                "label": "Drive Pan/Tilt/Zoom",
                "help": "Continuous move with a signed velocity per axis, for a joystick. 0 on an axis stops that axis.",
                "params": {
                    "pan": {"type": "number", "label": "Pan Velocity", "min": -1.0, "max": 1.0,
                            "required": True, "help": "-1 full left to 1 full right."},
                    "tilt": {"type": "number", "label": "Tilt Velocity", "min": -1.0, "max": 1.0,
                             "required": True, "help": "-1 full down to 1 full up."},
                    "zoom": {"type": "number", "label": "Zoom Velocity", "min": -1.0, "max": 1.0,
                             "default": 0.0, "help": "-1 wide to 1 tele. Leave 0 to leave zoom alone."},
                },
            },
            "pt_stop": {"label": "Stop Pan/Tilt/Zoom", "params": {},
                        "help": "Stop every ongoing pan, tilt and zoom movement."},
            "pt_absolute": {
                "label": "Go to Pan/Tilt Position",
                "params": {
                    "pan": {"type": "number", "label": "Pan", "min": -1.0, "max": 1.0, "required": True},
                    "tilt": {"type": "number", "label": "Tilt", "min": -1.0, "max": 1.0, "required": True},
                    "speed": {"type": "number", "label": "Speed", "min": 0.01, "max": 1.0,
                              "help": "Leave blank for the camera's default speed."},
                },
                "help": "Absolute position in the generic space: -1..1 on each axis.",
            },
            "pt_relative": {
                "label": "Nudge Pan/Tilt",
                "params": {
                    "pan": {"type": "number", "label": "Pan Step", "min": -1.0, "max": 1.0, "required": True},
                    "tilt": {"type": "number", "label": "Tilt Step", "min": -1.0, "max": 1.0, "required": True},
                    "speed": {"type": "number", "label": "Speed", "min": 0.01, "max": 1.0},
                },
                "help": "Move by a fraction of the full range from where the camera is now.",
            },
            # Zoom
            "zoom_in": {"label": "Zoom In", "params": {
                "speed": {"type": "number", "label": "Speed", "min": 0.01, "max": 1.0, "default": 0.5,
                          "help": "Fraction of the camera's full speed, 0.01 to 1."},
            },
                        "help": "Start zooming toward tele. Send zoom_stop to halt."},
            "zoom_out": {"label": "Zoom Out", "params": {
                "speed": {"type": "number", "label": "Speed", "min": 0.01, "max": 1.0, "default": 0.5,
                          "help": "Fraction of the camera's full speed, 0.01 to 1."},
            },
                         "help": "Start zooming toward wide. Send zoom_stop to halt."},
            "zoom_stop": {"label": "Stop Zoom", "params": {}},
            "zoom_absolute": {
                "label": "Go to Zoom Position",
                "params": {
                    "zoom": {"type": "number", "label": "Zoom", "min": 0.0, "max": 1.0, "required": True,
                             "help": "0 wide to 1 tele."},
                    "speed": {"type": "number", "label": "Speed", "min": 0.01, "max": 1.0},
                },
            },
            "zoom_relative": {
                "label": "Nudge Zoom",
                "params": {
                    "zoom": {"type": "number", "label": "Zoom Step", "min": -1.0, "max": 1.0, "required": True},
                    "speed": {"type": "number", "label": "Speed", "min": 0.01, "max": 1.0},
                },
            },
            # Home + presets
            "pt_home": {"label": "Go to Home", "params": {},
                        "help": "Move to the camera's home position."},
            "set_home": {"label": "Set Home Here", "params": {},
                         "help": "Save the current position as home. Some cameras have a fixed home and refuse."},
            "preset_recall": {
                "label": "Recall Preset",
                "params": {
                    "preset": {"type": "string", "label": "Preset", "required": True,
                               "options_state": "preset_options"},
                    "speed": {"type": "number", "label": "Speed", "min": 0.01, "max": 1.0},
                },
            },
            "preset_save": {
                "label": "Save Preset",
                "params": {
                    "name": {"type": "string", "label": "Name",
                             "help": "A name for the preset. Blank lets the camera name it."},
                    "preset": {"type": "string", "label": "Overwrite Preset",
                               "options_state": "preset_options",
                               "help": "Pick an existing preset to overwrite, or leave blank to create a new one."},
                },
                "help": "Save the current position. Fails while the camera is moving.",
            },
            "preset_delete": {
                "label": "Delete Preset",
                "params": {
                    "preset": {"type": "string", "label": "Preset", "required": True,
                               "options_state": "preset_options"},
                },
            },
            # Focus (imaging service)
            "focus_near": {"label": "Focus Near", "params": {
                "speed": {"type": "number", "label": "Speed", "min": 0.01, "max": 1.0, "default": 0.5,
                          "help": "Fraction of the camera's full speed, 0.01 to 1."},
            },
                           "help": "Start moving focus nearer. Switches the camera to manual focus. Send focus_stop to halt."},
            "focus_far": {"label": "Focus Far", "params": {
                "speed": {"type": "number", "label": "Speed", "min": 0.01, "max": 1.0, "default": 0.5,
                          "help": "Fraction of the camera's full speed, 0.01 to 1."},
            },
                          "help": "Start moving focus farther. Switches the camera to manual focus. Send focus_stop to halt."},
            "focus_stop": {"label": "Stop Focus", "params": {}},
            "focus_absolute": {
                "label": "Go to Focus Position",
                "params": {
                    "position": {"type": "number", "label": "Position", "required": True,
                                 "help": "In the camera's own focus range."},
                    "speed": {"type": "number", "label": "Speed", "min": 0.01, "max": 1.0},
                },
            },
            "focus_auto": {"label": "Auto Focus", "params": {},
                           "help": "Hand focus back to the camera."},
            "focus_manual": {"label": "Manual Focus", "params": {},
                             "help": "Hold focus where it is until a focus command moves it."},
            "imaging_preset_apply": {
                "label": "Apply Imaging Preset",
                "params": {
                    "preset": {"type": "string", "label": "Imaging Preset", "required": True,
                               "options_state": "imaging_preset_options"},
                },
                "help": "Apply one of the manufacturer's scene presets, on cameras that offer them.",
            },
            # Auxiliary (IR lamp, wiper, heater)
            "aux_command": {
                "label": "Auxiliary Command",
                "params": {
                    "command": {"type": "string", "label": "Command", "required": True,
                                "options_state": "aux_command_options",
                                "help": "One of the auxiliary commands the camera lists, such as an IR lamp or wiper."},
                },
            },
            # Relays
            "relay_on": {
                "label": "Relay On",
                "params": {"relay": {"type": "child_id", "child_type": "relay", "label": "Relay",
                                     "required": True}},
                "help": "Set the relay output active.",
            },
            "relay_off": {
                "label": "Relay Off",
                "params": {"relay": {"type": "child_id", "child_type": "relay", "label": "Relay",
                                     "required": True}},
                "help": "Set the relay output inactive.",
            },
            # System
            "reboot": {"label": "Reboot", "params": {},
                       "help": "Reboot the camera. It drops offline for a minute or two."},
        },
    }

    _client: httpx.AsyncClient | None = None

    def __init__(self, device_id: str, config: dict[str, Any], state, events):
        super().__init__(device_id, config, state, events)
        self._client = None
        self._auth_mode = "wsse"          # "wsse" | "digest" | "none"
        self._clock_offset = timedelta(0)
        self._services: dict[str, str] = {}
        self._profiles: dict[str, dict[str, Any]] = {}   # token -> parsed profile
        self._profile_children: dict[str, str] = {}      # token -> child id
        self._control_token = ""
        self._video_source = ""
        self._ptz_node = ""
        self._ptz_absolute = False
        self._ptz_relative = False
        self._presets: dict[str, str] = {}               # token -> name
        self._relays: dict[str, str] = {}                # token -> child id
        self._relay_tokens: dict[str, str] = {}          # child id -> token
        self._inputs: dict[str, str] = {}                # token -> child id
        self._imaging: dict[str, Any] = {}               # last ImagingSettings
        self._imaging_options: dict[str, Any] = {}
        self._focus_continuous = False
        self._focus_absolute = False
        self._focus_status_supported = False
        self._imaging_presets: dict[str, str] = {}
        self._tamper: dict[str, bool] = {}
        self._poll_count = 0
        self._event_task: asyncio.Task | None = None
        self._subscription_url = ""
        self._subscription_params: list[str] = []
        self._subscription_ends: datetime | None = None
        self._event_warned = False
        password = str(config.get("password", "") or "")
        if password:
            self.redact_in_log(password)

    # ── Config accessors ──

    @property
    def _host(self) -> str:
        return str(self.config.get("host", "")).strip()

    @property
    def _port(self) -> int:
        try:
            return int(self.config.get("port", 80) or 80)
        except (TypeError, ValueError):
            return 80

    @property
    def _scheme(self) -> str:
        return "https" if self.config.get("ssl") else "http"

    @property
    def _username(self) -> str:
        return str(self.config.get("username", "") or "")

    @property
    def _password(self) -> str:
        return str(self.config.get("password", "") or "")

    def _device_now(self) -> datetime:
        return datetime.now(timezone.utc) + self._clock_offset

    def _auth_fault(self, exc: OnvifFault) -> ConnectionFaultError:
        """The typed auth_failed fault, worded for what actually happened: a
        camera that wants a login when none is entered is told what to do,
        a rejected one is told what to check."""
        if not self._username:
            message = (
                "This camera needs an ONVIF login and none is entered. Open its "
                "web interface, create an ONVIF user with administrator or "
                "operator rights, then enter it under Edit Device and press "
                "Reconnect."
            )
        else:
            message = (
                "The camera refused the ONVIF login. Check the username and "
                "password, and the camera's clock."
            )
        return ConnectionFaultError(message, code="auth_failed")

    # ── Connection lifecycle ──

    async def _create_transport(self, transport_type: str) -> None:
        host, port = self._host, self._port
        if not host:
            raise ConnectionFaultError("No IP address configured", code="invalid_config")
        if not await self._verify_reachable(host, port):
            raise ConnectionError(f"{host}:{port} is not responding")
        self._auth_mode = "wsse" if self._username else "none"
        self._client = httpx.AsyncClient(
            base_url=f"{self._scheme}://{host}:{port}",
            verify=bool(self.config.get("verify_ssl", False)),
            timeout=httpx.Timeout(10.0, connect=5.0),
        )

    async def _post_connect(self) -> None:
        try:
            await self._sync_clock()
            await self._read_services()
            await self._read_identity()
        except OnvifFault as exc:
            if exc.not_authorized:
                raise self._auth_fault(exc) from exc
            raise ConnectionError(f"The camera answered with a fault: {exc}") from exc
        except httpx.TransportError as exc:
            raise ConnectionError(f"{self._host} is not responding: {exc}") from exc
        log.info(
            f"[{self.device_id}] Connected to ONVIF device at {self._host}:{self._port} "
            f"({self.get_state('manufacturer')} {self.get_state('model')}), "
            f"auth={self._auth_mode}, services={sorted(self._services)}"
        )

    async def _initial_sync(self) -> None:
        try:
            await self._read_profiles()
            await self._read_ptz()
            await self._read_imaging(initial=True)
            await self._read_io()
        except OnvifFault as exc:
            if exc.not_authorized:
                raise self._auth_fault(exc) from exc
            raise ConnectionError(f"The camera answered with a fault: {exc}") from exc
        except httpx.TransportError as exc:
            raise ConnectionError(f"{self._host} is not responding: {exc}") from exc
        # The pull-point subscription needs the service list, so it starts
        # here rather than in _start_push (which the platform runs first).
        self._start_event_loop()

    def _link_alive(self) -> bool:
        return self._client is not None

    async def _close_session(self) -> None:
        await self._stop_event_loop(unsubscribe=True)
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _liveness_probe(self) -> None:
        """GetSystemDateAndTime is PRE_AUTH and cheap; it also keeps the clock
        offset fresh, which is what the login digest depends on."""
        if self._client is None:
            raise ConnectionError("Not connected")
        await self._sync_clock()

    # ── SOAP plumbing ──

    def _service_url(self, service: str) -> str:
        url = self._services.get(service)
        if url:
            return url
        if service == "device":
            path = str(self.config.get("service_path") or "/onvif/device_service")
            return f"{self._scheme}://{self._host}:{self._port}{path}"
        raise OnvifCommandError(f"The camera does not offer the ONVIF {service} service.")

    def _rewrite_xaddr(self, url: str) -> str:
        """The address the device announces, re-pointed at the address we
        reached it on (a camera behind NAT or on a new DHCP lease reports the
        one it knows). The path and, when the device gave one, the port are
        kept."""
        parts = urlsplit((url or "").strip())
        if not parts.scheme or not parts.netloc:
            return url
        port = parts.port or self._port
        return urlunsplit(
            (self._scheme, f"{self._host}:{port}", parts.path or "/", parts.query, "")
        )

    async def _call(
        self,
        url: str,
        action: str,
        body: str,
        *,
        auth: bool = True,
        timeout: float | None = None,
        wsa_headers: str = "",
    ):
        """POST one SOAP 1.2 request and return the parsed Body's first child.

        Raises OnvifFault for a SOAP fault or a non-2xx answer with no fault
        body, and lets httpx transport errors propagate.
        """
        client = self._client
        if client is None:
            raise ConnectionError("Not connected")
        for attempt in (1, 2):
            header = wsa_headers
            request_auth = None
            if auth and self._username:
                if self._auth_mode == "digest":
                    request_auth = httpx.DigestAuth(self._username, self._password)
                else:
                    header = _wsse_header(self._username, self._password, self._device_now()) + header
            content = _envelope(body, header)
            headers = {
                "Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"',
            }
            kwargs: dict[str, Any] = {"content": content, "headers": headers}
            if request_auth is not None:
                kwargs["auth"] = request_auth
            if timeout is not None:
                kwargs["timeout"] = httpx.Timeout(timeout, connect=5.0)
            resp = await client.post(url, **kwargs)
            if (
                resp.status_code == 401
                and auth
                and self._username
                and self._auth_mode != "digest"
                and attempt == 1
            ):
                # Core 5.9.1: a device that only speaks HTTP Digest answers a
                # request without HTTP credentials with 401. Switch the
                # session and retry once; every later call uses Digest.
                log.info(f"[{self.device_id}] Camera wants HTTP Digest authentication")
                self._auth_mode = "digest"
                continue
            break
        root = None
        text = resp.text
        if text.strip():
            try:
                root = _xml_fromstring(text)
            except (_XMLParseError, ValueError):
                root = None
        if root is not None:
            fault = _parse_fault(root)
            if fault is not None:
                code, reason = fault
                raise OnvifFault(code, reason, http_status=resp.status_code)
        if resp.status_code == 401:
            raise OnvifFault("NotAuthorized", "The camera refused the login", http_status=401)
        if resp.status_code >= 400 or root is None:
            raise OnvifFault(
                f"HTTP{resp.status_code}",
                f"HTTP {resp.status_code} with no usable SOAP answer",
                http_status=resp.status_code,
            )
        soap_body = _child(root, "Body")
        if soap_body is None or len(soap_body) == 0:
            raise OnvifFault("EmptyBody", "The camera answered with an empty SOAP body")
        return soap_body[0]

    async def _service_call(self, service: str, op: str, body_inner: str = "", *, ns: str | None = None,
                            auth: bool = True, prefix: str | None = None):
        """Call ``op`` on a named service. The body element is ``<p:op>`` in the
        service's namespace, with ``body_inner`` as its children."""
        ns = ns or SERVICE_NAMESPACES[service]
        p = prefix or {NS_TDS: "tds", NS_TRT: "trt", NS_TR2: "tr2", NS_TPTZ: "tptz",
                       NS_TIMG: "timg", NS_TEV: "tev", NS_TMD: "tmd"}.get(ns, "ns")
        body = f'<{p}:{op} xmlns:{p}="{ns}">{body_inner}</{p}:{op}>' if body_inner else f'<{p}:{op} xmlns:{p}="{ns}"/>'
        return await self._call(self._service_url(service), f"{ns}/{op}", body, auth=auth)

    # ── Connect-time reads ──

    async def _sync_clock(self) -> None:
        """Read the device clock (PRE_AUTH) and keep the offset. A camera that
        demands authentication even here gets one authenticated retry; the
        offset stays at zero if that fails too."""
        try:
            resp = await self._service_call("device", "GetSystemDateAndTime", auth=False)
        except OnvifFault as exc:
            if not exc.not_authorized or not self._username:
                raise
            resp = await self._service_call("device", "GetSystemDateAndTime", auth=True)
        utc = _child(resp, "SystemDateAndTime", "UTCDateTime")
        if utc is None:
            return
        try:
            device_time = datetime(
                int(_text(utc, "Date", "Year")), int(_text(utc, "Date", "Month")),
                int(_text(utc, "Date", "Day")), int(_text(utc, "Time", "Hour")),
                int(_text(utc, "Time", "Minute")), int(_text(utc, "Time", "Second")),
                tzinfo=timezone.utc,
            )
        except ValueError:
            return
        self._clock_offset = device_time - datetime.now(timezone.utc)
        self.set_state("clock_offset_s", round(self._clock_offset.total_seconds(), 1))

    async def _read_services(self) -> None:
        services: dict[str, str] = {}
        try:
            resp = await self._service_call(
                "device", "GetServices",
                f'<tds:IncludeCapability xmlns:tds="{NS_TDS}">false</tds:IncludeCapability>',
            )
            for svc in _children(resp, "Service"):
                namespace = _text(svc, "Namespace")
                xaddr = _text(svc, "XAddr")
                for key, ns in SERVICE_NAMESPACES.items():
                    if namespace == ns and xaddr:
                        services[key] = self._rewrite_xaddr(xaddr)
        except OnvifFault as exc:
            if exc.not_authorized:
                raise
            log.info(f"[{self.device_id}] GetServices faulted ({exc}); using GetCapabilities")
        if "device" not in services:
            # Pre-GetServices firmware: the capability exchange carries XAddrs
            # for the ver10 services only.
            resp = await self._service_call(
                "device", "GetCapabilities",
                f'<tds:Category xmlns:tds="{NS_TDS}">All</tds:Category>',
            )
            caps = _child(resp, "Capabilities")
            if caps is not None:
                for key, name in (("device", "Device"), ("media", "Media"), ("ptz", "PTZ"),
                                  ("imaging", "Imaging"), ("events", "Events")):
                    xaddr = _text(caps, name, "XAddr")
                    if xaddr:
                        services[key] = self._rewrite_xaddr(xaddr)
        if "device" not in services:
            services["device"] = self._service_url("device")
        self._services = services

    async def _read_identity(self) -> None:
        resp = await self._service_call("device", "GetDeviceInformation")
        self.set_states({
            "manufacturer": _text(resp, "Manufacturer"),
            "model": _text(resp, "Model"),
            "firmware_version": _text(resp, "FirmwareVersion"),
            "serial_number": _text(resp, "SerialNumber"),
            "hardware_id": _text(resp, "HardwareId"),
        })

    # ── Media profiles ──

    def _parse_profile(self, elem, media2: bool) -> dict[str, Any]:
        token = elem.get("token", "")
        name = _text(elem, "Name")
        if media2:
            conf = _child(elem, "Configurations")
            source = _child(conf, "VideoSource") if conf is not None else None
            encoder = _child(conf, "VideoEncoder") if conf is not None else None
            ptz = _child(conf, "PTZ") if conf is not None else None
        else:
            source = _child(elem, "VideoSourceConfiguration")
            encoder = _child(elem, "VideoEncoderConfiguration")
            ptz = _child(elem, "PTZConfiguration")
        width = _text(encoder, "Resolution", "Width") if encoder is not None else ""
        height = _text(encoder, "Resolution", "Height") if encoder is not None else ""
        return {
            "token": token,
            "name": name or token,
            "video_source": _text(source, "SourceToken") if source is not None else "",
            "encoding": _text(encoder, "Encoding") if encoder is not None else "",
            "resolution": f"{width}x{height}" if width and height else "",
            "framerate": _float(_text(encoder, "RateControl", "FrameRateLimit")) if encoder is not None else None,
            "ptz_node": _text(ptz, "NodeToken") if ptz is not None else "",
            "ptz_config": ptz.get("token", "") if ptz is not None else "",
        }

    async def _read_profiles(self) -> None:
        media2 = "media2" in self._services
        profiles: dict[str, dict[str, Any]] = {}
        if media2:
            resp = await self._service_call(
                "media2", "GetProfiles", f'<tr2:Type xmlns:tr2="{NS_TR2}">All</tr2:Type>',
            )
            for elem in _children(resp, "Profiles"):
                prof = self._parse_profile(elem, media2=True)
                if prof["token"]:
                    profiles[prof["token"]] = prof
        elif "media" in self._services:
            resp = await self._service_call("media", "GetProfiles")
            for elem in _children(resp, "Profiles"):
                prof = self._parse_profile(elem, media2=False)
                if prof["token"]:
                    profiles[prof["token"]] = prof
        else:
            log.warning(f"[{self.device_id}] Camera offers no media service; no streams or PTZ")
        for token, prof in profiles.items():
            try:
                prof["stream_url"] = await self._stream_uri(token, media2)
            except OnvifFault as exc:
                log.info(f"[{self.device_id}] No stream URI for profile {token}: {exc}")
                prof["stream_url"] = ""
            try:
                prof["snapshot_url"] = await self._snapshot_uri(token, media2)
            except OnvifFault as exc:
                log.debug(f"[{self.device_id}] No snapshot URI for profile {token}: {exc}")
                prof["snapshot_url"] = ""
        # Reconcile the profile roster.
        wanted = {token: _child_id_for(token) for token in profiles}
        for token, child_id in list(self._profile_children.items()):
            if token not in wanted:
                self.deregister_child("profile", child_id)
                del self._profile_children[token]
        embed = bool(self.config.get("credentials_in_stream_url"))
        for token, prof in profiles.items():
            child_id = wanted[token]
            stream = prof["stream_url"]
            snapshot = prof["snapshot_url"]
            if embed:
                stream = _with_credentials(stream, self._username, self._password)
                snapshot = _with_credentials(snapshot, self._username, self._password)
            values = {
                "name": prof["name"],
                "token": token,
                "encoding": prof["encoding"],
                "resolution": prof["resolution"],
                "framerate": prof["framerate"],
                "has_ptz": bool(prof["ptz_node"]),
                "video_source": prof["video_source"],
                "preview_url": stream,
                "preview_format": "rtsp" if stream.lower().startswith("rtsp") else "",
                "snapshot_url": snapshot,
            }
            if token not in self._profile_children:
                self.register_child("profile", child_id, initial_state=values)
                self._profile_children[token] = child_id
            else:
                self.set_child_state_batch("profile", child_id, values)
        self._profiles = profiles
        # The control profile: configured, else first with PTZ, else first.
        configured = str(self.config.get("profile_token", "") or "").strip()
        if configured and configured in profiles:
            control = configured
        elif configured:
            log.warning(
                f"[{self.device_id}] Configured profile {configured!r} is not on the camera; "
                f"available: {sorted(profiles)}"
            )
            control = ""
        else:
            control = ""
        if not control:
            control = next((t for t, p in profiles.items() if p["ptz_node"]), "")
        if not control and profiles:
            control = next(iter(profiles))
        self._control_token = control
        prof = profiles.get(control, {})
        self._video_source = prof.get("video_source", "")
        child_id = self._profile_children.get(control, "")
        child_values = self.get_child_state("profile", child_id) if child_id else {}
        self.set_states({
            "profile_token": control,
            "profile_name": prof.get("name", ""),
            "preview_url": child_values.get("preview_url", ""),
            "preview_format": child_values.get("preview_format", ""),
            "snapshot_url": child_values.get("snapshot_url", ""),
        })

    def _stream_protocol(self) -> tuple[str, str, str]:
        """(Media2 Protocol, Media1 Stream, Media1 Transport Protocol)."""
        choice = str(self.config.get("stream_transport", "rtsp") or "rtsp")
        return {
            "rtsp": ("RTSP", "RTP-Unicast", "RTSP"),
            "udp": ("RtspUnicast", "RTP-Unicast", "UDP"),
            "http": ("RtspOverHttp", "RTP-Unicast", "HTTP"),
        }.get(choice, ("RTSP", "RTP-Unicast", "RTSP"))

    async def _stream_uri(self, token: str, media2: bool) -> str:
        proto2, stream1, transport1 = self._stream_protocol()
        if media2:
            resp = await self._service_call(
                "media2", "GetStreamUri",
                f'<tr2:Protocol xmlns:tr2="{NS_TR2}">{proto2}</tr2:Protocol>'
                f'<tr2:ProfileToken xmlns:tr2="{NS_TR2}">{_xml_escape(token)}</tr2:ProfileToken>',
            )
            return _text(resp, "Uri")
        resp = await self._service_call(
            "media", "GetStreamUri",
            f'<trt:StreamSetup xmlns:trt="{NS_TRT}" xmlns:tt="{NS_TT}">'
            f"<tt:Stream>{stream1}</tt:Stream>"
            f"<tt:Transport><tt:Protocol>{transport1}</tt:Protocol></tt:Transport>"
            f"</trt:StreamSetup>"
            f'<trt:ProfileToken xmlns:trt="{NS_TRT}">{_xml_escape(token)}</trt:ProfileToken>',
        )
        return _text(resp, "MediaUri", "Uri")

    async def _snapshot_uri(self, token: str, media2: bool) -> str:
        if media2:
            resp = await self._service_call(
                "media2", "GetSnapshotUri",
                f'<tr2:ProfileToken xmlns:tr2="{NS_TR2}">{_xml_escape(token)}</tr2:ProfileToken>',
            )
            return _text(resp, "Uri")
        resp = await self._service_call(
            "media", "GetSnapshotUri",
            f'<trt:ProfileToken xmlns:trt="{NS_TRT}">{_xml_escape(token)}</trt:ProfileToken>',
        )
        return _text(resp, "MediaUri", "Uri")

    # ── PTZ ──

    def _profile_body(self, prefix: str, ns: str, inner: str = "") -> str:
        return (
            f'<{prefix}:ProfileToken xmlns:{prefix}="{ns}">'
            f"{_xml_escape(self._control_token)}</{prefix}:ProfileToken>{inner}"
        )

    async def _ptz_call(self, op: str, inner: str = ""):
        if not self._ptz_node:
            raise OnvifCommandError("This camera has no pan/tilt/zoom control.")
        return await self._service_call("ptz", op, self._profile_body("tptz", NS_TPTZ, inner))

    async def _read_ptz(self) -> None:
        prof = self._profiles.get(self._control_token, {})
        node_token = prof.get("ptz_node", "")
        if not node_token or "ptz" not in self._services:
            self._ptz_node = ""
            self.set_states({"ptz_supported": False, "home_supported": False,
                             "preset_count": 0, "preset_options": "[]",
                             "aux_command_options": "[]"})
            return
        self._ptz_node = node_token
        resp = await self._service_call(
            "ptz", "GetNode",
            f'<tptz:NodeToken xmlns:tptz="{NS_TPTZ}">{_xml_escape(node_token)}</tptz:NodeToken>',
        )
        node = _child(resp, "PTZNode")
        spaces = _child(node, "SupportedPTZSpaces") if node is not None else None
        self._ptz_absolute = spaces is not None and (
            _child(spaces, "AbsolutePanTiltPositionSpace") is not None
            or _child(spaces, "AbsoluteZoomPositionSpace") is not None
        )
        self._ptz_relative = spaces is not None and (
            _child(spaces, "RelativePanTiltTranslationSpace") is not None
            or _child(spaces, "RelativeZoomTranslationSpace") is not None
        )
        aux = [
            a.text.strip() for a in _children(node, "AuxiliaryCommands")
            if node is not None and a.text and a.text.strip()
        ]
        home = _bool_text(_text(node, "HomeSupported")) if node is not None else False
        self.set_states({
            "ptz_supported": True,
            "home_supported": home,
            "aux_command_options": json.dumps(aux),
        })
        await self._refresh_presets()
        await self._poll_ptz_status()

    async def _refresh_presets(self) -> None:
        if not self._ptz_node:
            return
        resp = await self._ptz_call("GetPresets")
        presets: dict[str, str] = {}
        for elem in _children(resp, "Preset"):
            token = elem.get("token", "")
            if token:
                presets[token] = _text(elem, "Name") or token
        self._presets = presets
        self.set_states({
            "preset_count": len(presets),
            "preset_options": json.dumps(
                [{"value": t, "label": n if n == t else f"{n} ({t})"} for t, n in presets.items()]
            ),
        })

    async def _poll_ptz_status(self) -> None:
        if not self._ptz_node:
            return
        resp = await self._ptz_call("GetStatus")
        status = _child(resp, "PTZStatus")
        if status is None:
            return
        updates: dict[str, Any] = {}
        position = _child(status, "Position")
        if position is not None:
            pantilt = _child(position, "PanTilt")
            if pantilt is not None:
                updates["pan_position"] = _float(pantilt.get("x"))
                updates["tilt_position"] = _float(pantilt.get("y"))
            zoom = _child(position, "Zoom")
            if zoom is not None:
                updates["zoom_position"] = _float(zoom.get("x"))
        move = _child(status, "MoveStatus")
        if move is not None:
            states = {_text(move, "PanTilt").upper(), _text(move, "Zoom").upper()} - {""}
            if "MOVING" in states:
                updates["move_status"] = "moving"
            elif "UNKNOWN" in states or not states:
                updates["move_status"] = "unknown"
            else:
                updates["move_status"] = "idle"
        if updates:
            self.set_states(updates)

    async def _continuous_move(self, pan: float | None, tilt: float | None, zoom: float | None) -> None:
        await self._ptz_call(
            "ContinuousMove",
            f'<tptz:Velocity xmlns:tptz="{NS_TPTZ}">{_vector_xml(pan, tilt, zoom)}</tptz:Velocity>',
        )

    async def _stop(self, pan_tilt: bool, zoom: bool) -> None:
        await self._ptz_call(
            "Stop",
            f'<tptz:PanTilt xmlns:tptz="{NS_TPTZ}">{str(pan_tilt).lower()}</tptz:PanTilt>'
            f'<tptz:Zoom xmlns:tptz="{NS_TPTZ}">{str(zoom).lower()}</tptz:Zoom>',
        )

    @staticmethod
    def _speed_xml(speed: float | None, *, pan_tilt: bool, zoom: bool) -> str:
        if speed is None:
            return ""
        s = _clamp(speed, 0.01, 1.0)
        return (
            f'<tptz:Speed xmlns:tptz="{NS_TPTZ}">'
            f"{_vector_xml(s if pan_tilt else None, s if pan_tilt else None, s if zoom else None)}"
            "</tptz:Speed>"
        )

    # ── Imaging ──

    async def _imaging_call(self, op: str, inner: str = ""):
        if "imaging" not in self._services or not self._video_source:
            raise OnvifCommandError("This camera has no imaging control.")
        return await self._service_call(
            "imaging", op,
            f'<timg:VideoSourceToken xmlns:timg="{NS_TIMG}">'
            f"{_xml_escape(self._video_source)}</timg:VideoSourceToken>{inner}",
        )

    async def _read_imaging(self, *, initial: bool = False) -> None:
        if "imaging" not in self._services or not self._video_source:
            if initial:
                self.set_state("focus_supported", False)
            return
        if initial:
            try:
                resp = await self._imaging_call("GetOptions")
                self._imaging_options = self._parse_imaging_options(_child(resp, "ImagingOptions"))
                ranges: dict[str, Any] = {}
                for key in LEVEL_SETTINGS:
                    rng = self._imaging_options.get(SETTING_PATHS[key][0])
                    if rng:
                        ranges[f"{key}_range"] = f"{rng[0]:g}..{rng[1]:g}"
                if ranges:
                    self.set_states(ranges)
            except OnvifFault as exc:
                log.info(f"[{self.device_id}] Imaging GetOptions faulted: {exc}")
            try:
                resp = await self._imaging_call("GetMoveOptions")
                opts = _child(resp, "MoveOptions")
                self._focus_continuous = opts is not None and _child(opts, "Continuous") is not None
                self._focus_absolute = opts is not None and _child(opts, "Absolute") is not None
                self._focus_status_supported = self._focus_continuous or self._focus_absolute or (
                    opts is not None and _child(opts, "Relative") is not None
                )
            except OnvifFault as exc:
                log.info(f"[{self.device_id}] Imaging GetMoveOptions faulted: {exc}")
            self.set_state("focus_supported", self._focus_status_supported)
            try:
                resp = await self._imaging_call("GetPresets")
                self._imaging_presets = {
                    p.get("token", ""): _text(p, "Name") or p.get("token", "")
                    for p in _children(resp, "Preset") if p.get("token")
                }
                self.set_state("imaging_preset_options", json.dumps(
                    [{"value": t, "label": n} for t, n in self._imaging_presets.items()]
                ))
            except OnvifFault:
                self._imaging_presets = {}
                self.set_state("imaging_preset_options", "[]")
        resp = await self._imaging_call("GetImagingSettings")
        settings = _child(resp, "ImagingSettings")
        if settings is not None:
            self._imaging = self._parse_imaging(settings)
            self._publish_imaging()
        if self._imaging_presets:
            try:
                resp = await self._imaging_call("GetCurrentPreset")
                current = _child(resp, "CurrentPreset")
                self.set_state("imaging_preset", current.get("token", "") if current is not None else "")
            except OnvifFault:
                pass
        if self._focus_status_supported:
            await self._poll_focus_status()

    @staticmethod
    def _parse_imaging(settings) -> dict[str, Any]:
        """ImagingSettings20 -> {element: text | {child: text}} keeping only
        the elements the camera reported, so a write sends back exactly what
        it holds plus the one change."""
        out: dict[str, Any] = {}
        for elem in settings:
            name = _local(elem.tag)
            if name in IMAGING_CHILD_ORDER:
                out[name] = {
                    _local(c.tag): (c.text or "").strip()
                    for c in elem if _local(c.tag) in IMAGING_CHILD_ORDER[name]
                }
            elif name in IMAGING_ORDER:
                out[name] = (elem.text or "").strip()
        return out

    @staticmethod
    def _parse_imaging_options(options) -> dict[str, Any]:
        """{element: (min, max)} for the level settings, {element: [modes]} for
        the enumerated ones."""
        out: dict[str, Any] = {}
        if options is None:
            return out
        for name in ("Brightness", "ColorSaturation", "Contrast", "Sharpness"):
            rng = _child(options, name)
            if rng is not None:
                low, high = _float(_text(rng, "Min")), _float(_text(rng, "Max"))
                if low is not None and high is not None:
                    out[name] = (low, high)
        modes = [m.text.strip() for m in _children(options, "IrCutFilterModes") if m.text]
        if modes:
            out["IrCutFilter"] = modes
        for name in ("Exposure", "WhiteBalance", "BacklightCompensation", "WideDynamicRange"):
            block = _child(options, name)
            if block is not None:
                found = [m.text.strip() for m in _children(block, "Mode") if m.text]
                if found:
                    out[name] = found
        focus = _child(options, "Focus")
        if focus is not None:
            found = [m.text.strip() for m in _children(focus, "AutoFocusModes") if m.text]
            if found:
                out["Focus"] = found
        return out

    def _publish_imaging(self) -> None:
        img = self._imaging
        updates: dict[str, Any] = {}
        exposure = img.get("Exposure")
        if isinstance(exposure, dict) and exposure.get("Mode"):
            updates["exposure_mode"] = exposure["Mode"].lower()
        wb = img.get("WhiteBalance")
        if isinstance(wb, dict) and wb.get("Mode"):
            updates["wb_mode"] = wb["Mode"].lower()
        focus = img.get("Focus")
        if isinstance(focus, dict) and focus.get("AutoFocusMode"):
            updates["focus_mode"] = focus["AutoFocusMode"].lower()
        if img.get("IrCutFilter"):
            updates["ir_cut_filter"] = str(img["IrCutFilter"]).lower()
        blc = img.get("BacklightCompensation")
        if isinstance(blc, dict) and blc.get("Mode"):
            updates["backlight_compensation"] = blc["Mode"].upper() == "ON"
        wdr = img.get("WideDynamicRange")
        if isinstance(wdr, dict) and wdr.get("Mode"):
            updates["wide_dynamic_range"] = wdr["Mode"].upper() == "ON"
        for key in LEVEL_SETTINGS:
            value = img.get(SETTING_PATHS[key][0])
            if isinstance(value, str) and value:
                updates[key] = _float(value)
        if updates:
            self.set_states(updates)

    def _imaging_xml(self) -> str:
        parts = []
        for name in IMAGING_ORDER:
            value = self._imaging.get(name)
            if value is None:
                continue
            if isinstance(value, dict):
                inner = "".join(
                    f"<tt:{c}>{_xml_escape(str(value[c]))}</tt:{c}>"
                    for c in IMAGING_CHILD_ORDER[name] if value.get(c) not in (None, "")
                )
                parts.append(f"<tt:{name}>{inner}</tt:{name}>")
            else:
                parts.append(f"<tt:{name}>{_xml_escape(str(value))}</tt:{name}>")
        return (
            f'<timg:ImagingSettings xmlns:timg="{NS_TIMG}" xmlns:tt="{NS_TT}">'
            + "".join(parts)
            + "</timg:ImagingSettings>"
        )

    async def _write_imaging(self, key: str, value: Any) -> None:
        """Read-modify-write one imaging setting, then read back what the
        camera holds so state reports the device, not the request."""
        element, sub = SETTING_PATHS[key]
        resp = await self._imaging_call("GetImagingSettings")
        settings = _child(resp, "ImagingSettings")
        self._imaging = self._parse_imaging(settings) if settings is not None else {}
        if key in LEVEL_SETTINGS:
            number = _float(value)
            if number is None:
                raise DeviceSettingValueError(f"{key} needs a number")
            rng = self._imaging_options.get(element)
            if rng and not (rng[0] <= number <= rng[1]):
                raise DeviceSettingValueError(
                    f"{key} must be between {rng[0]:g} and {rng[1]:g} on this camera"
                )
            if element not in self._imaging:
                raise DeviceSettingValueError(f"This camera does not report {key}, so it cannot be set")
            self._imaging[element] = f"{number:g}"
        elif key in ("backlight_compensation", "wide_dynamic_range"):
            on = value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "on", "yes")
            block = self._imaging.get(element)
            if not isinstance(block, dict):
                raise DeviceSettingValueError(f"This camera does not report {key}, so it cannot be set")
            block["Mode"] = "ON" if on else "OFF"
        elif key == "ir_cut_filter":
            wire = str(value).strip().upper()
            if wire not in ("ON", "OFF", "AUTO"):
                raise DeviceSettingValueError("IR cut filter is on, off or auto")
            allowed = self._imaging_options.get("IrCutFilter")
            if allowed and wire not in [a.upper() for a in allowed]:
                raise DeviceSettingValueError(
                    f"This camera's IR cut filter offers {', '.join(a.lower() for a in allowed)}"
                )
            if "IrCutFilter" not in self._imaging:
                raise DeviceSettingValueError("This camera does not report an IR cut filter")
            self._imaging["IrCutFilter"] = wire
        else:
            wire = str(value).strip().upper()
            if wire not in ("AUTO", "MANUAL"):
                raise DeviceSettingValueError(f"{key} is auto or manual")
            block = self._imaging.get(element)
            if not isinstance(block, dict):
                raise DeviceSettingValueError(f"This camera does not report {key}, so it cannot be set")
            block[sub] = wire
        await self._imaging_call("SetImagingSettings", self._imaging_xml())
        resp = await self._imaging_call("GetImagingSettings")
        settings = _child(resp, "ImagingSettings")
        if settings is not None:
            self._imaging = self._parse_imaging(settings)
            self._publish_imaging()

    async def _poll_focus_status(self) -> None:
        resp = await self._imaging_call("GetStatus")
        focus = _child(resp, "Status", "FocusStatus20")
        if focus is None:
            return
        updates: dict[str, Any] = {}
        position = _float(_text(focus, "Position"))
        if position is not None:
            updates["focus_position"] = position
        move = _text(focus, "MoveStatus").upper()
        if move:
            updates["focus_move_status"] = {"MOVING": "moving", "IDLE": "idle"}.get(move, "unknown")
        if updates:
            self.set_states(updates)

    async def _focus_move(self, inner: str) -> None:
        await self._imaging_call(
            "Move", f'<timg:Focus xmlns:timg="{NS_TIMG}" xmlns:tt="{NS_TT}">{inner}</timg:Focus>',
        )

    # ── Relay outputs + digital inputs ──

    async def _read_io(self) -> None:
        relays: dict[str, dict[str, Any]] = {}
        try:
            resp = await self._service_call("device", "GetRelayOutputs")
            for elem in _children(resp, "RelayOutputs"):
                token = elem.get("token", "")
                if not token:
                    continue
                props = _child(elem, "Properties")
                relays[token] = {
                    "token": token,
                    "mode": _text(props, "Mode").lower() if props is not None else None,
                    "idle_state": _text(props, "IdleState").lower() if props is not None else None,
                    "delay_time": _text(props, "DelayTime") if props is not None else "",
                }
        except OnvifFault as exc:
            log.debug(f"[{self.device_id}] GetRelayOutputs faulted: {exc}")
        for token, child_id in list(self._relays.items()):
            if token not in relays:
                self.deregister_child("relay", child_id)
                del self._relays[token]
                self._relay_tokens.pop(child_id, None)
        for token, values in relays.items():
            child_id = _child_id_for(token)
            if values["mode"] not in ("bistable", "monostable"):
                values["mode"] = None
            if values["idle_state"] not in ("open", "closed"):
                values["idle_state"] = None
            if token in self._relays:
                self.set_child_state_batch("relay", child_id, values)
            else:
                self.register_child("relay", child_id, initial_state=values)
                self._relays[token] = child_id
                self._relay_tokens[child_id] = token
        inputs: list[str] = []
        if "deviceio" in self._services:
            try:
                resp = await self._service_call("deviceio", "GetDigitalInputs")
                inputs = [e.get("token", "") for e in _children(resp, "DigitalInputs") if e.get("token")]
            except OnvifFault as exc:
                log.debug(f"[{self.device_id}] GetDigitalInputs faulted: {exc}")
        for token in inputs:
            self._ensure_input(token)

    def _ensure_input(self, token: str) -> str:
        child_id = self._inputs.get(token)
        if child_id is None:
            child_id = _child_id_for(token)
            self.register_child("input", child_id, initial_state={"token": token})
            self._inputs[token] = child_id
        return child_id

    async def refresh_children(self) -> Any:
        await self._read_profiles()
        await self._read_ptz()
        await self._read_io()
        return {
            "profiles": len(self._profiles),
            "relays": len(self._relays),
            "inputs": len(self._inputs),
        }

    # ── Events (pull point) ──

    def _events_wanted(self) -> bool:
        return bool(self.config.get("events", True)) and "events" in self._services

    def _start_event_loop(self) -> None:
        if not self._events_wanted():
            self.set_state("events_active", False)
            return
        if self._event_task is None or self._event_task.done():
            self._event_task = asyncio.create_task(self._event_loop())

    async def _stop_event_loop(self, *, unsubscribe: bool) -> None:
        task, self._event_task = self._event_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if unsubscribe and self._subscription_url and self._client is not None:
            try:
                await asyncio.wait_for(self._subscription_call("Unsubscribe", ACTION_UNSUBSCRIBE), 5.0)
            except Exception as exc:  # noqa: BLE001 - best effort on the way out
                log.debug(f"[{self.device_id}] Unsubscribe failed: {exc}")
        self._subscription_url = ""
        self._subscription_params = []
        self._subscription_ends = None
        self.set_state("events_active", False)

    async def _stop_push(self) -> None:
        await super()._stop_push()
        await self._stop_event_loop(unsubscribe=True)

    def _wsa_headers(self, action: str, to: str, extra: list[str] | None = None) -> str:
        head = (
            f'<wsa:Action xmlns:wsa="{NS_WSA}">{action}</wsa:Action>'
            f'<wsa:To xmlns:wsa="{NS_WSA}">{_xml_escape(to)}</wsa:To>'
        )
        # A subscription's ReferenceParameters ride along as header blocks
        # (WS-Addressing 3.2); some cameras key the pull point on them.
        return head + "".join(extra or [])

    async def _subscribe(self) -> None:
        events_url = self._service_url("events")
        filter_xml = (
            f'<tev:Filter xmlns:tev="{NS_TEV}">'
            f'<wsnt:TopicExpression xmlns:wsnt="{NS_WSNT}" xmlns:tns1="{NS_TNS1}" '
            f'Dialect="{TOPIC_DIALECT_CONCRETE_SET}">'
            "tns1:Device/Trigger//.|tns1:VideoSource//.|tns1:PTZController//."
            "|tns1:RuleEngine/CellMotionDetector//."
            "</wsnt:TopicExpression></tev:Filter>"
        )
        term = (
            f'<tev:InitialTerminationTime xmlns:tev="{NS_TEV}">'
            f"PT{SUBSCRIPTION_TERM_S}S</tev:InitialTerminationTime>"
        )
        body_filtered = f'<tev:CreatePullPointSubscription xmlns:tev="{NS_TEV}">{filter_xml}{term}</tev:CreatePullPointSubscription>'
        body_plain = f'<tev:CreatePullPointSubscription xmlns:tev="{NS_TEV}">{term}</tev:CreatePullPointSubscription>'
        headers = self._wsa_headers(ACTION_CREATE_PULLPOINT, events_url)
        try:
            resp = await self._call(events_url, ACTION_CREATE_PULLPOINT, body_filtered, wsa_headers=headers)
        except OnvifFault as exc:
            if exc.not_authorized:
                raise
            # A device that rejects the topic filter still has to accept an
            # unfiltered subscription (Core 9.1.1).
            log.info(f"[{self.device_id}] Filtered subscription refused ({exc}); subscribing to all events")
            resp = await self._call(events_url, ACTION_CREATE_PULLPOINT, body_plain, wsa_headers=headers)
        ref = _child(resp, "SubscriptionReference")
        address = _text(ref, "Address") if ref is not None else ""
        if not address:
            raise OnvifFault("NoSubscription", "The camera returned no pull-point address")
        params = _child(ref, "ReferenceParameters")
        self._subscription_params = []
        if params is not None:
            from xml.etree.ElementTree import tostring
            for child in params:
                child.set(f"{{{NS_WSA}}}IsReferenceParameter", "true")
                self._subscription_params.append(tostring(child, encoding="unicode"))
        self._subscription_url = self._rewrite_xaddr(address)
        self._note_subscription_times(resp)
        self.set_state("events_active", True)
        self._event_warned = False
        log.info(f"[{self.device_id}] Event subscription open at {self._subscription_url}")

    def _note_subscription_times(self, resp) -> None:
        current = _parse_xs_datetime(_text(resp, "CurrentTime"))
        ends = _parse_xs_datetime(_text(resp, "TerminationTime"))
        if current is not None:
            self._clock_offset = current - datetime.now(timezone.utc)
            self.set_state("clock_offset_s", round(self._clock_offset.total_seconds(), 1))
        self._subscription_ends = ends

    async def _subscription_call(self, op: str, action: str, inner: str = "", *, timeout: float | None = None):
        prefix, ns = ("tev", NS_TEV) if op == "PullMessages" else ("wsnt", NS_WSNT)
        body = f'<{prefix}:{op} xmlns:{prefix}="{ns}">{inner}</{prefix}:{op}>' if inner else f'<{prefix}:{op} xmlns:{prefix}="{ns}"/>'
        headers = self._wsa_headers(action, self._subscription_url, self._subscription_params)
        return await self._call(self._subscription_url, action, body, wsa_headers=headers, timeout=timeout)

    async def _pull_once(self) -> None:
        resp = await self._subscription_call(
            "PullMessages", ACTION_PULL_MESSAGES,
            f'<tev:Timeout xmlns:tev="{NS_TEV}">PT{PULL_TIMEOUT_S}S</tev:Timeout>'
            f'<tev:MessageLimit xmlns:tev="{NS_TEV}">{PULL_MESSAGE_LIMIT}</tev:MessageLimit>',
            timeout=PULL_TIMEOUT_S + 15.0,
        )
        self._note_subscription_times(resp)
        for message in _children(resp, "NotificationMessage"):
            try:
                self._handle_notification(message)
            except Exception:  # noqa: BLE001 - one bad message must not stop the loop
                log.debug(f"[{self.device_id}] Could not read a notification", exc_info=True)
        if self._subscription_ends is not None:
            remaining = (self._subscription_ends - self._device_now()).total_seconds()
            if remaining < RENEW_BELOW_S:
                renew = await self._subscription_call(
                    "Renew", ACTION_RENEW,
                    f'<wsnt:TerminationTime xmlns:wsnt="{NS_WSNT}">PT{SUBSCRIPTION_TERM_S}S</wsnt:TerminationTime>',
                )
                self._note_subscription_times(renew)

    async def _event_loop(self) -> None:
        backoff = EVENT_RETRY_MIN_S
        while True:
            try:
                if not self._subscription_url:
                    await self._subscribe()
                    backoff = EVENT_RETRY_MIN_S
                await self._pull_once()
            except asyncio.CancelledError:
                raise
            except (httpx.TransportError, OnvifFault, ConnectionError, OSError) as exc:
                self._subscription_url = ""
                self._subscription_params = []
                self.set_state("events_active", False)
                if isinstance(exc, OnvifFault) and exc.not_authorized:
                    log.warning(f"[{self.device_id}] Event subscription refused: {exc}")
                    return
                level = log.debug if self._event_warned else log.warning
                level(f"[{self.device_id}] Event subscription dropped ({exc}); retrying in {backoff:.0f}s")
                self._event_warned = True
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, EVENT_RETRY_MAX_S)
            except Exception:  # noqa: BLE001 - keep the loop alive, say so once
                self._subscription_url = ""
                self.set_state("events_active", False)
                log.warning(f"[{self.device_id}] Event loop error; retrying", exc_info=not self._event_warned)
                self._event_warned = True
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, EVENT_RETRY_MAX_S)

    @staticmethod
    def _items(container) -> dict[str, str]:
        if container is None:
            return {}
        return {
            item.get("Name", ""): item.get("Value", "")
            for item in _children(container, "SimpleItem") if item.get("Name")
        }

    def _handle_notification(self, message) -> None:
        topic = _text(message, "Topic")
        topic = topic.split(":", 1)[-1] if ":" in topic.split("/", 1)[0] else topic
        holder = _child(message, "Message")
        inner = _child(holder, "Message") if holder is not None else None
        if inner is None:
            return
        operation = inner.get("PropertyOperation", "")
        source = self._items(_child(inner, "Source"))
        data = self._items(_child(inner, "Data"))
        if topic == TOPIC_DIGITAL_INPUT:
            token = source.get("InputToken", "")
            if token and "LogicalState" in data:
                child_id = self._ensure_input(token)
                self.set_child_state("input", child_id, "active", _bool_text(data["LogicalState"]))
        elif topic == TOPIC_RELAY:
            token = source.get("RelayToken", "")
            child_id = self._relays.get(token)
            if child_id and "LogicalState" in data:
                self.set_child_state("relay", child_id, "active", data["LogicalState"].lower() == "active")
        elif topic == TOPIC_MOTION:
            if "State" in data:
                self.set_state("motion", _bool_text(data["State"]))
        elif topic == TOPIC_CELL_MOTION:
            if "IsMotion" in data:
                self.set_state("motion", _bool_text(data["IsMotion"]))
        elif topic == TOPIC_SIGNAL_LOSS:
            if "State" in data:
                self.set_state("signal_loss", _bool_text(data["State"]))
        elif topic.startswith("VideoSource/"):
            kind = topic.split("/")[1] if "/" in topic else ""
            if kind in TAMPER_KINDS and "State" in data:
                if operation == "Deleted":
                    self._tamper.pop(kind, None)
                else:
                    self._tamper[kind] = _bool_text(data["State"])
                active = [TAMPER_KINDS[k] for k, v in self._tamper.items() if v]
                self.set_states({"tamper": bool(active), "tamper_reason": ", ".join(active)})
        elif topic.startswith(TOPIC_PRESET_PREFIX):
            status = topic[len(TOPIC_PRESET_PREFIX):].lower()
            if status in ("invoked", "reached", "aborted", "left"):
                self.set_states({
                    "preset_status": status,
                    "preset_last": data.get("PresetToken", self.get_state("preset_last")),
                })
        else:
            log.debug(f"[{self.device_id}] Ignoring event topic {topic!r}")

    # ── Polling ──

    async def poll(self) -> None:
        if self._client is None:
            return
        self._poll_count += 1
        try:
            if self._ptz_node:
                await self._poll_ptz_status()
            if self._focus_status_supported:
                await self._poll_focus_status()
            if self._poll_count % SLOW_POLL_EVERY == 1:
                if "imaging" in self._services and self._video_source:
                    await self._read_imaging()
                if self._ptz_node:
                    await self._refresh_presets()
        except OnvifFault as exc:
            if exc.not_authorized:
                raise ConnectionFaultError(
                    "The camera stopped accepting the ONVIF login.", code="auth_failed",
                ) from exc
            self.set_state("last_error", f"{exc.code}: {exc.reason}" if exc.reason else exc.code)
        except httpx.TransportError as exc:
            raise ConnectionError(f"{self._host} is not responding: {exc}") from exc

    # ── Commands ──

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        params = params or {}
        handler = self._DISPATCH.get(command)
        if handler is None:
            raise OnvifCommandError(f"Unknown command: {command}")
        try:
            return await handler(self, params)
        except OnvifFault as exc:
            if exc.not_authorized:
                raise ConnectionFaultError(
                    "The camera stopped accepting the ONVIF login.", code="auth_failed",
                ) from exc
            raise OnvifCommandError(self._explain_fault(command, exc)) from exc
        except httpx.TransportError as exc:
            raise ConnectionError(f"{self._host} is not responding: {exc}") from exc

    @staticmethod
    def _explain_fault(command: str, exc: OnvifFault) -> str:
        code = exc.code
        sentence = {
            "NoProfile": "The control profile is not on the camera any more. Refresh from Device.",
            "NoPTZProfile": "The control profile has no PTZ configuration. Pick a profile with PTZ under Advanced.",
            "NoToken": "That preset is not on the camera. Refresh from Device to reload the list.",
            "InvalidPosition": "That position is outside the camera's range.",
            "InvalidTranslation": "That step is outside the camera's range.",
            "InvalidSpeed": "That speed is outside the camera's range.",
            "InvalidVelocity": "That velocity is outside the camera's range.",
            "MovingPTZ": "The camera is still moving. Wait for it to stop, then save the preset.",
            "TooManyPresets": "The camera has no free preset slots.",
            "PresetExist": "A preset with that name already exists.",
            "InvalidPresetName": "The camera refused that preset name.",
            "NoHomePosition": "No home position is set on the camera.",
            "CannotOverwriteHome": "This camera's home position is fixed.",
            "SettingsInvalid": "The camera refused those image settings.",
            "NoImagingForSource": "This camera's video source has no imaging control.",
            "SpaceNotSupported": "The camera does not support that movement type.",
            "ActionNotSupported": "The camera does not support that operation.",
        }.get(code)
        if sentence:
            return sentence
        detail = exc.reason or code
        return f"The camera refused {command}: {detail}"

    async def _cmd_pt_direction(self, params: dict[str, Any], command: str) -> None:
        dx, dy = _PtzDirection.TABLE[command]
        speed = _speed_param(params)
        await self._continuous_move(dx * speed, dy * speed, None)

    async def _cmd_pt_drive(self, params: dict[str, Any]) -> None:
        pan = _clamp(_float(params.get("pan"), 0.0) or 0.0, -1.0, 1.0)
        tilt = _clamp(_float(params.get("tilt"), 0.0) or 0.0, -1.0, 1.0)
        zoom = _clamp(_float(params.get("zoom"), 0.0) or 0.0, -1.0, 1.0)
        await self._continuous_move(pan, tilt, zoom)

    async def _cmd_pt_stop(self, params: dict[str, Any]) -> None:
        await self._stop(True, True)

    async def _cmd_pt_absolute(self, params: dict[str, Any]) -> None:
        if not self._ptz_absolute:
            raise OnvifCommandError("This camera does not support absolute pan/tilt positioning.")
        pan = _clamp(_float(params.get("pan"), 0.0) or 0.0, -1.0, 1.0)
        tilt = _clamp(_float(params.get("tilt"), 0.0) or 0.0, -1.0, 1.0)
        await self._ptz_call(
            "AbsoluteMove",
            f'<tptz:Position xmlns:tptz="{NS_TPTZ}">{_vector_xml(pan, tilt, None)}</tptz:Position>'
            + self._speed_xml(_float(params.get("speed")), pan_tilt=True, zoom=False),
        )

    async def _cmd_pt_relative(self, params: dict[str, Any]) -> None:
        if not self._ptz_relative:
            raise OnvifCommandError("This camera does not support relative pan/tilt steps.")
        pan = _clamp(_float(params.get("pan"), 0.0) or 0.0, -1.0, 1.0)
        tilt = _clamp(_float(params.get("tilt"), 0.0) or 0.0, -1.0, 1.0)
        await self._ptz_call(
            "RelativeMove",
            f'<tptz:Translation xmlns:tptz="{NS_TPTZ}">{_vector_xml(pan, tilt, None)}</tptz:Translation>'
            + self._speed_xml(_float(params.get("speed")), pan_tilt=True, zoom=False),
        )

    async def _cmd_zoom_in(self, params: dict[str, Any]) -> None:
        await self._continuous_move(None, None, _speed_param(params))

    async def _cmd_zoom_out(self, params: dict[str, Any]) -> None:
        await self._continuous_move(None, None, -_speed_param(params))

    async def _cmd_zoom_stop(self, params: dict[str, Any]) -> None:
        await self._stop(False, True)

    async def _cmd_zoom_absolute(self, params: dict[str, Any]) -> None:
        if not self._ptz_absolute:
            raise OnvifCommandError("This camera does not support absolute zoom positioning.")
        zoom = _clamp(_float(params.get("zoom"), 0.0) or 0.0, 0.0, 1.0)
        await self._ptz_call(
            "AbsoluteMove",
            f'<tptz:Position xmlns:tptz="{NS_TPTZ}">{_vector_xml(None, None, zoom)}</tptz:Position>'
            + self._speed_xml(_float(params.get("speed")), pan_tilt=False, zoom=True),
        )

    async def _cmd_zoom_relative(self, params: dict[str, Any]) -> None:
        if not self._ptz_relative:
            raise OnvifCommandError("This camera does not support relative zoom steps.")
        zoom = _clamp(_float(params.get("zoom"), 0.0) or 0.0, -1.0, 1.0)
        await self._ptz_call(
            "RelativeMove",
            f'<tptz:Translation xmlns:tptz="{NS_TPTZ}">{_vector_xml(None, None, zoom)}</tptz:Translation>'
            + self._speed_xml(_float(params.get("speed")), pan_tilt=False, zoom=True),
        )

    async def _cmd_pt_home(self, params: dict[str, Any]) -> None:
        await self._ptz_call("GotoHomePosition")

    async def _cmd_set_home(self, params: dict[str, Any]) -> None:
        await self._ptz_call("SetHomePosition")

    def _preset_token(self, value: Any) -> str:
        """Accept a preset token or its name; the picker sends the token."""
        text = str(value or "").strip()
        if not text:
            raise OnvifCommandError("Pick a preset.")
        if text in self._presets:
            return text
        for token, name in self._presets.items():
            if name == text:
                return token
        return text

    async def _cmd_preset_recall(self, params: dict[str, Any]) -> None:
        token = self._preset_token(params.get("preset"))
        await self._ptz_call(
            "GotoPreset",
            f'<tptz:PresetToken xmlns:tptz="{NS_TPTZ}">{_xml_escape(token)}</tptz:PresetToken>'
            + self._speed_xml(_float(params.get("speed")), pan_tilt=True, zoom=True),
        )

    async def _cmd_preset_save(self, params: dict[str, Any]) -> str:
        name = str(params.get("name") or "").strip()
        existing = str(params.get("preset") or "").strip()
        inner = ""
        if name:
            inner += f'<tptz:PresetName xmlns:tptz="{NS_TPTZ}">{_xml_escape(name)}</tptz:PresetName>'
        if existing:
            inner += (
                f'<tptz:PresetToken xmlns:tptz="{NS_TPTZ}">'
                f"{_xml_escape(self._preset_token(existing))}</tptz:PresetToken>"
            )
        resp = await self._ptz_call("SetPreset", inner)
        token = _text(resp, "PresetToken")
        await self._refresh_presets()
        return token

    async def _cmd_preset_delete(self, params: dict[str, Any]) -> None:
        token = self._preset_token(params.get("preset"))
        await self._ptz_call(
            "RemovePreset",
            f'<tptz:PresetToken xmlns:tptz="{NS_TPTZ}">{_xml_escape(token)}</tptz:PresetToken>',
        )
        await self._refresh_presets()

    async def _cmd_focus_near(self, params: dict[str, Any]) -> None:
        if not self._focus_continuous:
            raise OnvifCommandError("This camera does not support continuous focus control.")
        await self._focus_move(f"<tt:Continuous><tt:Speed>{-_speed_param(params):.4f}</tt:Speed></tt:Continuous>")

    async def _cmd_focus_far(self, params: dict[str, Any]) -> None:
        if not self._focus_continuous:
            raise OnvifCommandError("This camera does not support continuous focus control.")
        await self._focus_move(f"<tt:Continuous><tt:Speed>{_speed_param(params):.4f}</tt:Speed></tt:Continuous>")

    async def _cmd_focus_stop(self, params: dict[str, Any]) -> None:
        await self._imaging_call("Stop")

    async def _cmd_focus_absolute(self, params: dict[str, Any]) -> None:
        if not self._focus_absolute:
            raise OnvifCommandError("This camera does not support absolute focus positioning.")
        position = _float(params.get("position"))
        if position is None:
            raise OnvifCommandError("Give a focus position.")
        speed = _float(params.get("speed"))
        inner = f"<tt:Absolute><tt:Position>{position:.4f}</tt:Position>"
        if speed is not None:
            inner += f"<tt:Speed>{_clamp(speed, 0.01, 1.0):.4f}</tt:Speed>"
        inner += "</tt:Absolute>"
        await self._focus_move(inner)

    async def _cmd_focus_auto(self, params: dict[str, Any]) -> None:
        await self._write_imaging("focus_mode", "auto")

    async def _cmd_focus_manual(self, params: dict[str, Any]) -> None:
        await self._write_imaging("focus_mode", "manual")

    async def _cmd_imaging_preset_apply(self, params: dict[str, Any]) -> None:
        token = str(params.get("preset") or "").strip()
        if not token:
            raise OnvifCommandError("Pick an imaging preset.")
        for t, name in self._imaging_presets.items():
            if name == token:
                token = t
                break
        await self._imaging_call(
            "SetCurrentPreset",
            f'<timg:PresetToken xmlns:timg="{NS_TIMG}">{_xml_escape(token)}</timg:PresetToken>',
        )
        await self._read_imaging()

    async def _cmd_aux_command(self, params: dict[str, Any]) -> str:
        data = str(params.get("command") or "").strip()
        if not data:
            raise OnvifCommandError("Pick an auxiliary command.")
        resp = await self._ptz_call(
            "SendAuxiliaryCommand",
            f'<tptz:AuxiliaryData xmlns:tptz="{NS_TPTZ}">{_xml_escape(data)}</tptz:AuxiliaryData>',
        )
        return _text(resp, "AuxiliaryResponse")

    async def _set_relay(self, params: dict[str, Any], active: bool) -> None:
        child_id = str(params.get("relay") or "").strip()
        token = self._relay_tokens.get(child_id)
        if token is None:
            raise OnvifCommandError("Pick a relay output.")
        await self._service_call(
            "device", "SetRelayOutputState",
            f'<tds:RelayOutputToken xmlns:tds="{NS_TDS}">{_xml_escape(token)}</tds:RelayOutputToken>'
            f'<tds:LogicalState xmlns:tds="{NS_TDS}">{"active" if active else "inactive"}</tds:LogicalState>',
        )

    async def _cmd_relay_on(self, params: dict[str, Any]) -> None:
        await self._set_relay(params, True)

    async def _cmd_relay_off(self, params: dict[str, Any]) -> None:
        await self._set_relay(params, False)

    async def _cmd_reboot(self, params: dict[str, Any]) -> str:
        resp = await self._service_call("device", "SystemReboot")
        return _text(resp, "Message")

    _DISPATCH: dict[str, Any] = {
        **{
            name: (lambda self, p, _n=name: self._cmd_pt_direction(p, _n))
            for name in _PtzDirection.TABLE
        },
        "pt_drive": _cmd_pt_drive,
        "pt_stop": _cmd_pt_stop,
        "pt_absolute": _cmd_pt_absolute,
        "pt_relative": _cmd_pt_relative,
        "zoom_in": _cmd_zoom_in,
        "zoom_out": _cmd_zoom_out,
        "zoom_stop": _cmd_zoom_stop,
        "zoom_absolute": _cmd_zoom_absolute,
        "zoom_relative": _cmd_zoom_relative,
        "pt_home": _cmd_pt_home,
        "set_home": _cmd_set_home,
        "preset_recall": _cmd_preset_recall,
        "preset_save": _cmd_preset_save,
        "preset_delete": _cmd_preset_delete,
        "focus_near": _cmd_focus_near,
        "focus_far": _cmd_focus_far,
        "focus_stop": _cmd_focus_stop,
        "focus_absolute": _cmd_focus_absolute,
        "focus_auto": _cmd_focus_auto,
        "focus_manual": _cmd_focus_manual,
        "imaging_preset_apply": _cmd_imaging_preset_apply,
        "aux_command": _cmd_aux_command,
        "relay_on": _cmd_relay_on,
        "relay_off": _cmd_relay_off,
        "reboot": _cmd_reboot,
    }

    # ── Device settings ──

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        if key not in SETTING_PATHS:
            raise ValueError(f"Unknown device setting: {key}")
        try:
            await self._write_imaging(key, value)
        except OnvifFault as exc:
            if exc.not_authorized:
                raise ConnectionFaultError(
                    "The camera stopped accepting the ONVIF login.", code="auth_failed",
                ) from exc
            raise DeviceSettingValueError(self._explain_fault(key, exc)) from exc
        except httpx.TransportError as exc:
            raise ConnectionError(f"{self._host} is not responding: {exc}") from exc
