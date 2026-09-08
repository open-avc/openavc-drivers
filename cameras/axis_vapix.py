"""
OpenAVC Axis Camera Driver (VAPIX).

Controls Axis network cameras through VAPIX, Axis's own HTTP API: remote
zoom and focus on fixed cameras (the optics control API), the IR cut filter
and the day/night switch, image settings (brightness, contrast, wide dynamic
range, exposure, white balance, defog and the rest of the sensor group),
rotation and mirroring, text and image overlays, the I/O ports, IR
illuminators, audio, view areas with their stream and snapshot addresses,
and pan, tilt, zoom, focus, presets and guard tours on PTZ models and on
fixed cameras with digital PTZ turned on. Events
(port changes, day/night mode, motion from the camera's analytics, tampering,
system ready, hardware faults) arrive over the VAPIX event WebSocket.

Why Python
----------
VAPIX is a family of interfaces rather than one protocol: JSON methods over
POST on a dozen CGIs, ``key=value`` text from ``param.cgi``, plain GET
arguments for ``ptz.cgi`` and ``port.cgi``, HTTP Digest on every request, and
an event stream that is a JSON-RPC WebSocket authenticated by a Digest
handshake. The camera also says what it has (the API discovery service, the
optics capabilities, the port roster, the light roster) and the driver shapes
itself to that at connect. None of it fits the declarative ``.avcdriver``
request/response model or the four ``push:`` shapes, so this is a Python
driver that owns an ``httpx`` session and a ``websockets`` connection.

Push vs poll
------------
Hybrid. The event WebSocket (``/vapix/ws-data-stream?sources=events``, AXIS
OS 10.11 and later) is the push channel: the driver configures a topic filter
and reacts to ``events:notify`` frames. Everything stateful is replayed when
the stream opens, so day/night mode and the port states are known at once.
Lens position, PTZ position and the port states are also polled
(``poll_interval``, default 10 s), so a camera without the WebSocket API
still reports its ports; image settings, overlays, lights and audio are
re-read on a slower cadence inside the same loop.

Authentication
--------------
VAPIX authenticates against the camera's own user list (root, or an account
created under System > Accounts) with HTTP Digest by default. An ONVIF
account is a separate thing on Axis and is not accepted here. A camera whose
authentication policy is Basic only answers the Digest attempt with a Basic
challenge and the driver switches, but only over HTTPS: Basic over plain
HTTP sends the password in the clear and the driver refuses it. A rejected
login is a typed ``auth_failed`` fault so the platform waits for new
credentials instead of retrying into a lockout.

Stream credentials
------------------
The RTSP, MJPEG and snapshot addresses the driver publishes carry no login by
default, because state is shown in the IDE and relayed to a paired cloud
account. ``credentials_in_stream_url`` embeds them for a room that wants the
Video Panel to play the stream directly.

Sources (all public, from Axis Communications, developer.axis.com/vapix):
  Authentication                  https://developer.axis.com/vapix/authentication/
  Basic device information        https://developer.axis.com/vapix/network-video/basic-device-information/
  API Discovery service           https://developer.axis.com/vapix/network-video/api-discovery-service/
  Parameter management            https://developer.axis.com/vapix/network-video/parameter-management/
  Parameters for video channels   https://developer.axis.com/vapix/network-video/parameter-management/image-api/
  Imaging API (sensor group)      https://developer.axis.com/vapix/network-video/imaging-api/
  Image source rotation           https://developer.axis.com/vapix/network-video/image-source-rotation/
  Optics control                  https://developer.axis.com/vapix/network-video/optics-control/
  DayNight API                    https://developer.axis.com/vapix/network-video/daynight-api/
  Pan/tilt/zoom API               https://developer.axis.com/vapix/network-video/pantiltzoom-api/
  Guard tour API                  https://developer.axis.com/vapix/network-video/guard-tour-api/
  I/O port management             https://developer.axis.com/vapix/network-video/io-port-management/
  Input and outputs               https://developer.axis.com/vapix/network-video/input-and-outputs/
  Light control                   https://developer.axis.com/vapix/network-video/light-control/
  Overlay API                     https://developer.axis.com/vapix/network-video/overlay-api/
  View Area API                   https://developer.axis.com/vapix/network-video/view-area-api/
  Video streaming                 https://developer.axis.com/vapix/network-video/video-streaming/
  Stream profiles                 https://developer.axis.com/vapix/network-video/stream-profiles/
  Audio API                       https://developer.axis.com/vapix/audio-systems/audio-api/
  Time API                        https://developer.axis.com/vapix/network-video/time-api/
  Firmware management (reboot)    https://developer.axis.com/vapix/network-video/firmware-management-api/
  Event streaming over WebSocket  https://developer.axis.com/vapix/network-video/event-streaming-over-websocket/
  Event and action services       https://developer.axis.com/vapix/network-video/event-and-action-services/
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
import websockets

from openavc.core.connection_fault import CHILD_NOT_FITTED
from openavc.drivers.base import (
    BaseDriver,
    ConnectionFaultError,
    DeviceSettingValueError,
)
from openavc.utils.logger import get_logger

log = get_logger(__name__)


# ── CGI paths ──

CGI_DEVICE_INFO = "/axis-cgi/basicdeviceinfo.cgi"
CGI_API_DISCOVERY = "/axis-cgi/apidiscovery.cgi"
CGI_PARAM = "/axis-cgi/param.cgi"
CGI_OPTICS = "/axis-cgi/opticscontrol.cgi"
CGI_DAYNIGHT = "/axis-cgi/daynight.cgi"
CGI_PTZ = "/axis-cgi/com/ptz.cgi"
CGI_PTZ_CONFIG = "/axis-cgi/com/ptzconfig.cgi"
CGI_PORT_MGMT = "/axis-cgi/io/portmanagement.cgi"
CGI_PORT = "/axis-cgi/io/port.cgi"
CGI_VIRTUAL_INPUT = "/axis-cgi/io/virtualinput.cgi"
CGI_LIGHT = "/axis-cgi/lightcontrol.cgi"
CGI_OVERLAY = "/axis-cgi/dynamicoverlay/dynamicoverlay.cgi"
CGI_VIEW_AREA_INFO = "/axis-cgi/viewarea/info.cgi"
CGI_STREAM_PROFILE = "/axis-cgi/streamprofile.cgi"
CGI_TIME = "/axis-cgi/time.cgi"
CGI_FIRMWARE = "/axis-cgi/firmwaremanagement.cgi"
CGI_RESTART = "/axis-cgi/restart.cgi"
CGI_WSSESSION = "/axis-cgi/wssession.cgi"
CGI_SNAPSHOT = "/axis-cgi/jpg/image.cgi"
CGI_MJPEG = "/axis-cgi/mjpg/video.cgi"
RTSP_PATH = "/axis-media/media.amp"
WS_EVENTS_PATH = "/vapix/ws-data-stream"

# API Discovery ids (the "Identification" line of each API's page).
API_OPTICS = "optics-control"
API_DAYNIGHT = "daynight"
API_PTZ = "ptz-control"
API_IO = "io-port-management"
API_LIGHT = "light-control"
API_VIEW_AREA = "view-area"
API_STREAM_PROFILES = "stream-profiles"
API_TIME = "time-service"
API_FIRMWARE = "fwmgr"
API_EVENT_WS = "event-streaming-over-websocket"
API_GUARD_TOUR = "guard-tour"

# Event topic families the driver subscribes to (event declaration
# namespaces: tns1 = ONVIF, tnsaxis = Axis).
EVENT_TOPIC_FILTERS = (
    "tns1:Device/tnsaxis:IO//.",
    "tns1:Device/tnsaxis:Status//.",
    "tns1:Device/tnsaxis:HardwareFailure//.",
    "tns1:Device/tnsaxis:Casing//.",
    "tns1:Device/tnsaxis:Sensor//.",
    "tns1:Device/tnsaxis:Tampering//.",
    "tns1:VideoSource//.",
    "tns1:VideoAnalytics//.",
    "tns1:PTZController//.",
    "tns1:AudioSource//.",
    "tns1:CameraApplicationPlatform//.",
    "tns1:RuleEngine//.",
)

EVENT_RETRY_MIN_S = 5.0
EVENT_RETRY_MAX_S = 60.0
EVENT_OPEN_TIMEOUT_S = 10.0

# Slow-cadence refresh inside poll(): image settings, overlays, lights,
# audio, guard tours, stream profiles.
SLOW_POLL_EVERY = 6

# Sensor parameters (ImageSource.I#.Sensor.*) read as state and written as
# device settings: setting key -> (parameter name, kind).
SENSOR_SETTINGS: dict[str, tuple[str, str]] = {
    "brightness": ("Brightness", "int"),
    "contrast": ("Contrast", "int"),
    "color_level": ("ColorLevel", "int"),
    "sharpness": ("Sharpness", "int"),
    "wdr": ("WDR", "onoff"),
    "wdr_level": ("WDRLevel", "int"),
    "local_contrast": ("LocalContrast", "int"),
    "exposure_mode": ("Exposure", "enum"),
    "exposure_value": ("ExposureValue", "int"),
    "exposure_window": ("ExposureWindow", "enum"),
    "exposure_priority": ("ExposurePriority", "int"),
    "max_gain": ("MaxGain", "int"),
    "white_balance": ("WhiteBalance", "enum"),
    "backlight_compensation": ("BacklightCompensation", "yesno"),
    "defog": ("Defog", "enum"),
    "defog_effect": ("DefogEffect", "int"),
}

DAYNIGHT_SETTINGS: dict[str, str] = {
    "day_night_shift_level": "DayNightShiftLevel",
    "day_night_dwell_time": "DayNightDwellTime",
    "night_day_dwell_time": "NightDayDwellTime",
    "night_day_shift_level": "NightDayShiftLevel",
    "day_night_autotune": "Autotune",
    "night_filter": "NightFilter",
}

OVERLAY_POSITIONS = ("topLeft", "top", "topRight", "bottomLeft", "bottom", "bottomRight")
OVERLAY_COLORS = ("black", "white", "red", "transparent", "semiTransparent")
OPTICS_STEP_TYPES = {"big": "bigStep", "small": "smallStep"}

_CHILD_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


class VapixError(Exception):
    """An answer the camera gave that is not the one asked for: a JSON error
    object, a ``# Error:`` line from param.cgi, an ``Error:`` line from
    ptz.cgi, or an HTTP status with no usable body."""

    def __init__(self, message: str, *, code: int | str = "", http_status: int = 0):
        self.code = code
        self.http_status = http_status
        super().__init__(message or f"HTTP {http_status}" or str(code))

    @property
    def not_authorized(self) -> bool:
        return self.http_status in (401, 403)


class VapixCommandError(Exception):
    """A command the camera refused, worded for the person who pressed it."""


# ── Helpers ──


def _bool_text(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on", "high", "active")


def _yesno(value: Any) -> str:
    return "yes" if (value if isinstance(value, bool) else _bool_text(value)) else "no"


def _onoff(value: Any) -> str:
    return "on" if (value if isinstance(value, bool) else _bool_text(value)) else "off"


def _float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _child_id_for(token: str) -> str:
    """A camera id as a child local id: letters, digits, '_' and '-' only."""
    cleaned = _CHILD_ID_RE.sub("_", str(token).strip())
    return cleaned or "_"


def _parse_param_list(text: str) -> dict[str, str]:
    """``root.Group.Name=value`` lines -> ``{"Group.Name": "value"}``.

    param.cgi answers a bad group with a ``# Error:`` line and HTTP 200, so
    that is raised here rather than read as an empty group.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("# Error"):
            raise VapixError(line.lstrip("# ").strip())
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("root."):
            key = key[5:]
        out[key] = value.strip()
    return out


def _with_credentials(url: str, username: str, password: str) -> str:
    """Embed a login in a URL's authority (``rtsp://user:pass@host/...``)."""
    if not url or not username:
        return url
    parts = urlsplit(url)
    if "@" in parts.netloc:
        return url
    cred = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    return urlunsplit(parts._replace(netloc=cred + parts.netloc))


def _parse_challenge(header: str) -> dict[str, str]:
    """The fields of a ``WWW-Authenticate: Digest ...`` header."""
    fields: dict[str, str] = {}
    for key, value in re.findall(r'(\w+)=("[^"]*"|[^,\s]+)', header):
        fields[key.lower()] = value.strip('"')
    return fields


def _digest_authorization(
    challenge: str, method: str, uri: str, username: str, password: str,
) -> str:
    """An ``Authorization: Digest`` value answering ``challenge`` (RFC 2617,
    MD5, qop=auth), for the WebSocket handshake that httpx does not make."""
    fields = _parse_challenge(challenge)
    realm = fields.get("realm", "")
    nonce = fields.get("nonce", "")
    opaque = fields.get("opaque")
    qop = "auth" if "auth" in fields.get("qop", "auth").split(",") else ""
    cnonce = os.urandom(8).hex()
    nc = "00000001"
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode("utf-8")).hexdigest()
    if fields.get("algorithm", "MD5").upper() == "MD5-SESS":
        ha1 = hashlib.md5(f"{ha1}:{nonce}:{cnonce}".encode("utf-8")).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode("utf-8")).hexdigest()
    if qop:
        response = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode("utf-8")).hexdigest()
    else:
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode("utf-8")).hexdigest()
    parts = [
        f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"',
        f'uri="{uri}"', f'response="{response}"', "algorithm=MD5",
    ]
    if qop:
        parts += [f"qop={qop}", f"nc={nc}", f'cnonce="{cnonce}"']
    if opaque:
        parts.append(f'opaque="{opaque}"')
    return "Digest " + ", ".join(parts)


def _topic_name(topic: str) -> str:
    """``tns1:Device/tnsaxis:IO/tnsaxis:Port`` -> ``Device/IO/Port``."""
    return "/".join(part.split(":", 1)[-1] for part in str(topic).split("/"))


class AxisVapixDriver(BaseDriver):
    """Axis camera over VAPIX: HTTP Digest, JSON and text CGIs, event WebSocket."""

    DRIVER_INFO = {
        "id": "axis_vapix",
        "name": "Axis Camera (VAPIX)",
        "manufacturer": "Axis",
        "category": "camera",
        "version": "1.1.1",
        "author": "OpenAVC",
        "description": (
            "Controls Axis network cameras through VAPIX, Axis's own API: remote "
            "zoom and focus on fixed cameras, the IR cut filter and day/night "
            "switching, image settings, rotation, text and image overlays, the "
            "I/O ports, IR illuminators, audio, and the stream and snapshot "
            "addresses for the Video Panel. PTZ models add pan, tilt, zoom, "
            "presets and guard tours. Port changes, day/night mode, motion, "
            "tampering and hardware faults arrive as events."
        ),
        "source_url": "https://developer.axis.com/vapix/",
        "tags": ["axis", "vapix", "camera", "ptz", "rtsp", "day-night", "io"],
        "verified": False,
        "simulated": True,
        "protocols": ["vapix", "http"],
        "ports": [80, 443],
        "transport": "http",
        "discovery": {
            # The four Axis OUIs, the SSDP root description every AXIS OS device
            # serves (manufacturer AXIS on the UPnP Basic device), Bonjour, the
            # factory hostname, and the one VAPIX answer a camera gives without
            # a login: basic device information's unrestricted properties.
            "oui": ["00:40:8c", "ac:cc:8e", "b8:a4:4f", "e8:27:25"],
            "manufacturer_alias": ["AXIS", "Axis Communications"],
            "hostname": ["^axis-"],
            "mdns": "_axis-video._tcp.local.",
            "ssdp": {
                "device_type": "urn:schemas-upnp-org:device:Basic:1",
                "manufacturer": "AXIS",
            },
            "tcp_probe": {
                "port": 80,
                "send_ascii": (
                    "POST /axis-cgi/basicdeviceinfo.cgi HTTP/1.1\r\n"
                    "Host: axis\r\n"
                    "Content-Type: application/json\r\n"
                    "Content-Length: 66\r\n"
                    "Connection: close\r\n"
                    "\r\n"
                    '{"apiVersion":"1.0","method":"getAllUnrestrictedProperties"}      '
                ),
                "expect_regex": r'"Brand"\s*:\s*"AXIS"',
                "extract_manufacturer": "Axis",
                "extract": {
                    "model": {"regex": r'"ProdNbr"\s*:\s*"([^"]+)"', "group": 1},
                    "serial_number": {"regex": r'"SerialNumber"\s*:\s*"([^"]+)"', "group": 1},
                    "firmware": {"regex": r'"Version"\s*:\s*"([^"]+)"', "group": 1},
                },
                "timeout_ms": 3000,
            },
        },
        "compatible_models": [
            {
                "manufacturer": "Axis",
                "models": ["P3265-V"],
                "confidence": "partial",
                "notes": (
                    "Bench-tested on AXIS OS 10.12: identity, remote zoom and "
                    "focus, IR cut filter and day/night, image settings, "
                    "overlays, the I/O ports and their events, view areas with "
                    "stream and snapshot addresses. It is a fixed dome, so the "
                    "PTZ, guard tour and IR illuminator control were verified "
                    "in the simulator only."
                ),
            },
            {
                "manufacturer": "Axis",
                "models": [
                    "Any Axis camera on AXIS OS 9.x or later",
                    "PTZ models (P55, P56, Q60, Q61, Q62, Q63 series and M55)",
                ],
                "confidence": "untested",
                "notes": (
                    "Built from the VAPIX documentation. The driver reads what "
                    "the camera says it has (API discovery, optics "
                    "capabilities, the port and light rosters) and offers only "
                    "that. Events need AXIS OS 10.11 or later; older cameras "
                    "are polled."
                ),
            },
        ],
        "help": {
            "overview": (
                "Controls an Axis camera through VAPIX, the camera's own API. "
                "Fixed cameras get remote zoom and focus, the IR cut filter and "
                "day/night switch, image settings, overlays, the I/O ports and "
                "any IR illuminator. PTZ cameras add pan, tilt, zoom, presets "
                "and guard tours. Every view area is listed with its stream "
                "address so the Video Panel can show it."
            ),
            "setup": (
                "1. Find the camera. Axis cameras take a DHCP address and fall "
                "back to 192.168.0.90 when no DHCP server answers. The hostname "
                "is axis- followed by the serial number, for example "
                "axis-b8a44fc359b2.\n"
                "2. A brand-new camera asks for a root password the first time "
                "its web interface opens. Set it before anything else; the "
                "camera answers nothing until it is set.\n"
                "3. Create an account for the driver under System > Accounts "
                "with operator or administrator rights, or use root. ONVIF "
                "accounts are separate on Axis and do not work here.\n"
                "4. Check the camera's date and time under System > Date and "
                "time. Event timestamps and the camera's own certificate "
                "depend on it.\n"
                "5. Add the camera with its IP address and that login. Under "
                "Advanced, pick the view area to control if the camera has more "
                "than one, and a stream profile if the Video Panel should use "
                "one.\n"
                "6. To let the Video Panel play the camera's stream without "
                "adding it by hand, turn on 'Login in stream address' under "
                "Advanced and read the note beside it first."
            ),
            "connection": (
                "Use a camera account with operator or administrator rights, "
                "not an ONVIF account. A new camera needs its root password set "
                "in its web interface first. No DHCP? The camera is at "
                "192.168.0.90."
            ),
        },
        "default_config": {
            "host": "",
            "port": 80,
            "ssl": False,
            "verify_ssl": False,
            "username": "",
            "password": "",
            "camera": 1,
            "stream_profile": "",
            "rtsp_port": 554,
            "events": True,
            "credentials_in_stream_url": False,
            "poll_interval": 10,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {
                "type": "integer",
                "required": True,
                "default": 80,
                "label": "HTTP Port",
                "help": "80 for HTTP, 443 when Use HTTPS is on.",
            },
            "username": {
                "type": "string",
                "required": True,
                "label": "Username",
                "help": "A camera account with operator or administrator rights (System > Accounts), or root. ONVIF accounts do not work here.",
            },
            "password": {"type": "string", "required": True, "label": "Password", "secret": True},
            "ssl": {
                "type": "boolean",
                "label": "Use HTTPS",
                "default": False,
                "advanced": True,
                "help": "Talk to the camera over HTTPS. Set the port to 443 as well.",
            },
            "verify_ssl": {
                "type": "boolean",
                "label": "Verify Certificate",
                "default": False,
                "advanced": True,
                "help": "Only for HTTPS. Off for the self-signed certificate the camera ships with.",
            },
            "camera": {
                "type": "integer",
                "label": "View Area",
                "default": 1,
                "min": 1,
                "max": 32,
                "advanced": True,
                "help": "The view area (video channel) this device controls and previews. 1 on a camera with one view area.",
            },
            "stream_profile": {
                "type": "string",
                "label": "Stream Profile",
                "default": "",
                "advanced": True,
                "help": "The name of a stream profile on the camera for the published stream address. Blank uses the camera's default stream. The profiles are listed under Stream Profile List once connected.",
            },
            "rtsp_port": {
                "type": "integer",
                "label": "RTSP Port",
                "default": 554,
                "min": 1,
                "max": 65535,
                "advanced": True,
                "help": "The camera's RTSP port, for the published stream address.",
            },
            "events": {
                "type": "boolean",
                "label": "Subscribe to Events",
                "default": True,
                "advanced": True,
                "help": "Keep the camera's event stream open so port changes, day/night mode, motion and tampering arrive at once. Turn off only for a camera whose event stream misbehaves.",
            },
            "credentials_in_stream_url": {
                "type": "boolean",
                "label": "Login in Stream Address",
                "default": False,
                "advanced": True,
                "help": "Embed the camera login in the published stream and snapshot addresses so the Video Panel can play them without adding the stream by hand. The address, login included, is then visible in Live State and to a paired cloud account. Off keeps the login out of state; add the stream under Video Streams with its login instead.",
            },
            "poll_interval": {
                "type": "integer",
                "label": "Poll Interval (s)",
                "default": 10,
                "min": 1,
                "max": 300,
                "advanced": True,
                "help": "How often lens position, PTZ position and the ports are read back. Events do not depend on it.",
            },
        },
        "state_variables": {
            # Identity
            "model": {"type": "string", "label": "Model"},
            "product_name": {"type": "string", "label": "Product Name"},
            "firmware_version": {"type": "string", "label": "AXIS OS Version"},
            "serial_number": {"type": "string", "label": "Serial Number"},
            "hardware_id": {"type": "string", "label": "Hardware ID"},
            "api_list": {"type": "string", "label": "VAPIX APIs",
                         "help": "The APIs the camera announces, comma separated."},
            # Streams
            "preview_url": {"type": "string", "label": "Stream URL",
                            "help": "The RTSP address of the controlled view area. The Video Panel lists it as a source."},
            "preview_format": {"type": "string", "label": "Stream Format"},
            "snapshot_url": {"type": "string", "label": "Snapshot URL",
                             "help": "A JPEG of the current picture, fetched with an HTTP GET."},
            "mjpeg_url": {"type": "string", "label": "MJPEG URL",
                          "help": "A Motion JPEG stream over HTTP for a viewer that cannot play RTSP."},
            "stream_profile_options": {"type": "string", "label": "Stream Profile List",
                                       "help": "The stream profiles on the camera, for the Stream Profile setting."},
            "view_area_count": {"type": "integer", "label": "View Area Count"},
            # Optics (fixed cameras)
            "zoom_supported": {"type": "boolean", "label": "Remote Zoom Supported"},
            "focus_supported": {"type": "boolean", "label": "Remote Focus Supported"},
            "ir_cut_supported": {"type": "boolean", "label": "IR Cut Filter Control Supported"},
            "magnification": {"type": "number", "label": "Magnification", "min": 1.0, "step": 0.1,
                              "unit": "x", "control": True,
                              "help": "1 is wide; the top of the range is Max Magnification."},
            "max_magnification": {"type": "number", "label": "Max Magnification", "unit": "x"},
            "zoom_moving": {"type": "boolean", "label": "Zoom Moving"},
            "focus_position": {"type": "number", "label": "Focus Position", "min": 0.0, "max": 1.0,
                               "step": 0.01, "control": True, "help": "0 near to 1 far."},
            "focus_moving": {"type": "boolean", "label": "Focus Moving"},
            "ir_cut_filter": {"type": "enum", "label": "IR Cut Filter", "values": ["on", "off", "auto"],
                              "control": True,
                              "help": "on is day mode (color), off is night mode (black and white, IR sensitive), auto lets the camera switch."},
            "temperature_compensation": {"type": "boolean", "label": "Focus Temperature Compensation"},
            "ir_compensation": {"type": "boolean", "label": "Focus IR Compensation"},
            # Day / night
            "day_mode": {"type": "boolean", "label": "Day Mode",
                         "help": "True while the IR cut filter is on. Reported by the camera's day/night event."},
            "day_night_shift_level": {"type": "integer", "label": "Day to Night Level", "min": 0, "max": 100,
                                      "control": True, "help": "Higher switches to night mode when it is darker."},
            "day_night_dwell_time": {"type": "number", "label": "Day to Night Dwell", "unit": "s",
                                     "min": 1, "max": 600},
            "night_day_dwell_time": {"type": "number", "label": "Night to Day Dwell", "unit": "s",
                                     "min": 1, "max": 600},
            "night_day_shift_level": {"type": "integer", "label": "Night to Day Level", "min": 0, "max": 100},
            "day_night_autotune": {"type": "boolean", "label": "Night to Day Autotune"},
            "night_filter": {"type": "enum", "label": "Night Filter", "values": ["clear", "irpass"]},
            # Image (ImageSource.I#.Sensor)
            "brightness": {"type": "integer", "label": "Brightness", "min": 0, "max": 100, "control": True},
            "contrast": {"type": "integer", "label": "Contrast", "min": 0, "max": 100, "control": True},
            "color_level": {"type": "integer", "label": "Saturation", "min": 0, "max": 100, "control": True},
            "sharpness": {"type": "integer", "label": "Sharpness", "min": 0, "max": 100, "control": True},
            "wdr": {"type": "boolean", "label": "Wide Dynamic Range", "control": True},
            "wdr_level": {"type": "integer", "label": "WDR Level", "min": 0, "max": 100, "control": True},
            "local_contrast": {"type": "integer", "label": "Local Contrast", "min": 0, "max": 100},
            "exposure_mode": {"type": "enum", "label": "Exposure Mode",
                              "values": ["auto", "flickerfree50", "flickerfree60", "hold"], "control": True},
            "exposure_value": {"type": "integer", "label": "Exposure Value", "min": 0, "max": 100, "control": True},
            "exposure_window": {"type": "string", "label": "Exposure Window"},
            "exposure_priority": {"type": "integer", "label": "Exposure Priority", "min": 0, "max": 100,
                                  "help": "0 prioritizes low noise, 100 prioritizes motion, 50 is neither."},
            "max_gain": {"type": "integer", "label": "Max Gain", "min": 0, "max": 100},
            "white_balance": {"type": "string", "label": "White Balance", "control": True},
            "backlight_compensation": {"type": "boolean", "label": "Backlight Compensation", "control": True},
            "defog": {"type": "enum", "label": "Defog", "values": ["off", "on", "auto"]},
            "defog_effect": {"type": "integer", "label": "Defog Effect", "min": 0, "max": 100},
            "rotation": {"type": "integer", "label": "Rotation", "unit": "deg"},
            "mirror": {"type": "boolean", "label": "Mirror"},
            "overlays_shown": {"type": "string", "label": "Overlays Shown",
                               "help": "Which overlay kinds the camera draws into its streams."},
            # PTZ
            "ptz_supported": {"type": "boolean", "label": "PTZ Supported",
                              "help": "True while the camera's pan, tilt and zoom (mechanical, or digital on a fixed camera) is turned on."},
            "ptz_digital": {"type": "boolean", "label": "Digital PTZ Available",
                            "help": "The camera can pan, tilt and zoom digitally within its picture."},
            "ptz_enabled": {"type": "boolean", "label": "PTZ Enabled",
                            "help": "The camera's PTZ is turned on for the controlled view area."},
            "ptz_driver": {"type": "string", "label": "PTZ Driver"},
            "pan_position": {"type": "number", "label": "Pan", "min": -180.0, "max": 180.0, "step": 0.1,
                             "unit": "deg", "control": True},
            "tilt_position": {"type": "number", "label": "Tilt", "min": -180.0, "max": 180.0, "step": 0.1,
                              "unit": "deg", "control": True},
            "zoom_level": {"type": "integer", "label": "PTZ Zoom", "min": 1, "max": 9999, "control": True,
                           "help": "1 wide to 9999 tele in the camera's zoom steps."},
            "ptz_moving": {"type": "boolean", "label": "PTZ Moving"},
            "ptz_ready": {"type": "boolean", "label": "PTZ Ready"},
            "autofocus": {"type": "boolean", "label": "PTZ Autofocus", "control": True},
            "preset_count": {"type": "integer", "label": "Preset Count"},
            "preset_options": {"type": "string", "label": "Preset List",
                               "help": "The camera's PTZ presets, for the preset pickers."},
            "preset_last": {"type": "string", "label": "Last Preset"},
            "guard_tour_options": {"type": "string", "label": "Guard Tour List"},
            "guard_tour_running": {"type": "string", "label": "Guard Tour Running",
                                   "help": "The name of the running guard tour, blank when none."},
            # I/O
            "port_count": {"type": "integer", "label": "I/O Port Count"},
            "manual_trigger": {"type": "boolean", "label": "Manual Trigger",
                               "help": "The camera's manual trigger, from its Live View button or a virtual input."},
            # Lights
            "light_count": {"type": "integer", "label": "Illuminator Count"},
            # Audio
            "audio_supported": {"type": "boolean", "label": "Audio Supported"},
            "audio_enabled": {"type": "boolean", "label": "Audio Enabled", "control": True},
            "audio_input_gain": {"type": "number", "label": "Audio Input Gain", "unit": "dB", "control": True},
            "audio_input_muted": {"type": "boolean", "label": "Audio Input Muted"},
            "audio_output_gain": {"type": "number", "label": "Audio Output Gain", "unit": "dB", "control": True},
            "audio_alarm": {"type": "boolean", "label": "Audio Level Alarm",
                            "help": "The sound level is above the camera's alarm level."},
            # Overlays
            "overlay_image_options": {"type": "string", "label": "Overlay Image List",
                                      "help": "The image files uploaded to the camera, for the image overlay picker."},
            # Events
            "events_active": {"type": "boolean", "label": "Event Stream Active",
                              "help": "True while the camera's event stream is open and delivering."},
            "motion": {"type": "boolean", "label": "Motion Detected",
                       "help": "From the camera's analytics (Object Analytics or Video Motion Detection). A camera with no analytics rule never reports it."},
            "motion_source": {"type": "string", "label": "Motion Source",
                              "help": "The event topic that last reported motion."},
            "tamper_count": {"type": "integer", "label": "Tamper Count",
                             "help": "How many tampering events (camera covered, moved or defocused) since connect."},
            "tamper_last": {"type": "string", "label": "Last Tamper"},
            "stream_accessed": {"type": "boolean", "label": "Live Stream Accessed",
                                "help": "Someone is watching the camera's live stream."},
            "system_ready": {"type": "boolean", "label": "System Ready"},
            "casing_open": {"type": "boolean", "label": "Casing Open"},
            "hardware_fault": {"type": "boolean", "label": "Hardware Fault",
                               "help": "A fan, power supply or temperature failure the camera reports."},
            "hardware_fault_reason": {"type": "string", "label": "Hardware Fault Reason"},
            "storage_fault": {"type": "boolean", "label": "Storage Disrupted",
                              "help": "The camera cannot use its SD card or network share. A camera with no card fitted reports this too."},
            "storage_fault_detail": {"type": "string", "label": "Storage Disrupted On"},
            "scene_change": {"type": "boolean", "label": "Scene Changed",
                             "help": "The camera reports its whole picture changed, as when it is covered or turned."},
            "temperature_alarm": {"type": "boolean", "label": "Temperature Outside Range"},
            "pir": {"type": "boolean", "label": "PIR Sensor"},
            "shock_count": {"type": "integer", "label": "Shock Count"},
            # System
            "clock_offset_s": {"type": "number", "label": "Camera Clock Offset", "unit": "s",
                               "help": "Camera clock minus this system's clock."},
            "time_zone": {"type": "string", "label": "Time Zone"},
            "last_error": {"type": "string", "label": "Last Error"},
        },
        "child_entity_types": {
            "view": {
                "label": "View Area",
                "label_plural": "View Areas",
                "id_format": {"type": "integer", "min": 1, "max": 32},
                "summary_fields": ["enabled", "source", "resolution"],
                "state_variables": {
                    "enabled": {"type": "boolean", "label": "Enabled",
                                "help": "Turned on in the camera. A disabled view area has no stream."},
                    "source": {"type": "integer", "label": "Image Source"},
                    "configurable": {"type": "boolean", "label": "Configurable"},
                    "resolution": {"type": "string", "label": "Geometry",
                                   "help": "The cropped area's size and offset on the sensor canvas."},
                    "preview_url": {"type": "string", "label": "Stream URL"},
                    "preview_format": {"type": "string", "label": "Stream Format"},
                    "snapshot_url": {"type": "string", "label": "Snapshot URL"},
                    "mjpeg_url": {"type": "string", "label": "MJPEG URL"},
                },
            },
            "port": {
                "label": "I/O Port",
                "label_plural": "I/O Ports",
                "id_format": {"type": "string"},
                "label_field": "name",
                "summary_fields": ["name", "direction", "active"],
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "direction": {"type": "enum", "label": "Direction", "values": ["input", "output"]},
                    "state": {"type": "enum", "label": "Circuit", "values": ["open", "closed"]},
                    "normal_state": {"type": "enum", "label": "Normal State", "values": ["open", "closed"]},
                    "active": {"type": "boolean", "label": "Active", "control": True,
                               "help": "True when the circuit is in the opposite of its normal state."},
                    "usage": {"type": "string", "label": "Usage"},
                    "configurable": {"type": "boolean", "label": "Configurable"},
                },
            },
            "light": {
                "label": "Illuminator",
                "label_plural": "Illuminators",
                "id_format": {"type": "string"},
                "summary_fields": ["light_type", "on", "intensity"],
                "state_variables": {
                    "light_type": {"type": "string", "label": "Type"},
                    "enabled": {"type": "boolean", "label": "Enabled"},
                    "on": {"type": "boolean", "label": "On", "control": True},
                    "auto_intensity": {"type": "boolean", "label": "Automatic Intensity", "control": True},
                    "intensity": {"type": "integer", "label": "Intensity", "min": 0, "max": 100,
                                  "unit": "%", "control": True},
                    "sync_day_night": {"type": "boolean", "label": "Follows Day/Night"},
                    "led_count": {"type": "integer", "label": "LED Count"},
                    "error": {"type": "string", "label": "Error"},
                },
            },
            "overlay": {
                "label": "Overlay",
                "label_plural": "Overlays",
                "id_format": {"type": "string"},
                "label_field": "text",
                "summary_fields": ["kind", "text", "position"],
                "state_variables": {
                    "kind": {"type": "enum", "label": "Kind", "values": ["text", "image"]},
                    "camera": {"type": "integer", "label": "View Area"},
                    "text": {"type": "string", "label": "Text", "control": True},
                    "position": {"type": "string", "label": "Position"},
                    "font_size": {"type": "integer", "label": "Font Size"},
                    "text_color": {"type": "string", "label": "Text Color"},
                    "background_color": {"type": "string", "label": "Background Color"},
                    "image_path": {"type": "string", "label": "Image"},
                    "visible": {"type": "boolean", "label": "Visible"},
                },
            },
        },
        "device_settings": {
            "digital_ptz": {
                "type": "boolean", "label": "Digital PTZ",
                "state_key": "ptz_enabled", "default": False, "setup": False,
                "help": "Turn the camera's digital pan, tilt and zoom on for the controlled view area. The PTZ commands work while it is on.",
            },
            "ir_cut_filter": {
                "type": "enum", "label": "IR Cut Filter",
                "values": [{"value": "auto", "label": "Auto"}, {"value": "on", "label": "On (day)"},
                           {"value": "off", "label": "Off (night)"}],
                "state_key": "ir_cut_filter", "default": "auto", "setup": False,
                "help": "On keeps the camera in color day mode, off in black and white night mode, auto switches on light level.",
            },
            "brightness": {"type": "integer", "label": "Brightness", "min": 0, "max": 100,
                           "state_key": "brightness", "default": 50, "setup": False},
            "contrast": {"type": "integer", "label": "Contrast", "min": 0, "max": 100,
                         "state_key": "contrast", "default": 50, "setup": False},
            "color_level": {"type": "integer", "label": "Saturation", "min": 0, "max": 100,
                            "state_key": "color_level", "default": 50, "setup": False},
            "sharpness": {"type": "integer", "label": "Sharpness", "min": 0, "max": 100,
                          "state_key": "sharpness", "default": 50, "setup": False},
            "wdr": {"type": "boolean", "label": "Wide Dynamic Range",
                    "state_key": "wdr", "default": True, "setup": False,
                    "help": "Balances bright and dark parts of the picture."},
            "wdr_level": {"type": "integer", "label": "WDR Level", "min": 0, "max": 100,
                          "state_key": "wdr_level", "default": 50, "setup": False},
            "local_contrast": {"type": "integer", "label": "Local Contrast", "min": 0, "max": 100,
                               "state_key": "local_contrast", "default": 50, "setup": False},
            "exposure_mode": {
                "type": "enum", "label": "Exposure Mode",
                "values": [{"value": "auto", "label": "Auto"},
                           {"value": "flickerfree50", "label": "Flicker-free 50 Hz"},
                           {"value": "flickerfree60", "label": "Flicker-free 60 Hz"},
                           {"value": "hold", "label": "Hold current"}],
                "state_key": "exposure_mode", "default": "auto", "setup": False,
                "help": "Flicker-free modes match the mains frequency of the room's lighting.",
            },
            "exposure_value": {"type": "integer", "label": "Exposure Value", "min": 0, "max": 100,
                               "state_key": "exposure_value", "default": 50, "setup": False},
            "exposure_window": {
                "type": "enum", "label": "Exposure Window",
                "values": ["auto", "right", "left", "upper", "lower", "spot", "custom"],
                "state_key": "exposure_window", "default": "auto", "setup": False,
                "help": "The part of the picture that drives the auto exposure.",
            },
            "exposure_priority": {"type": "integer", "label": "Exposure Priority", "min": 0, "max": 100,
                                  "state_key": "exposure_priority", "default": 50, "setup": False,
                                  "help": "0 low noise, 100 motion, 50 neither."},
            "max_gain": {"type": "integer", "label": "Max Gain", "min": 0, "max": 100,
                         "state_key": "max_gain", "default": 100, "setup": False},
            "white_balance": {
                "type": "enum", "label": "White Balance",
                "values": [{"value": "auto", "label": "Auto"}, {"value": "auto_indoor", "label": "Auto indoor"},
                           {"value": "auto_outdoor", "label": "Auto outdoor"}, {"value": "hold", "label": "Hold current"},
                           {"value": "manual", "label": "Manual"},
                           {"value": "fixed_outdoor1", "label": "Fixed sunny (5500 K)"},
                           {"value": "fixed_outdoor2", "label": "Fixed cloudy (6500 K)"},
                           {"value": "fixed_indoor", "label": "Fixed incandescent (3000 K)"},
                           {"value": "fixed_fluor1", "label": "Fixed fluorescent (4000 K)"},
                           {"value": "fixed_fluor2", "label": "Fixed fluorescent (3000 K)"}],
                "state_key": "white_balance", "default": "auto", "setup": False,
            },
            "backlight_compensation": {"type": "boolean", "label": "Backlight Compensation",
                                       "state_key": "backlight_compensation", "default": False, "setup": False,
                                       "help": "Brightens a subject lit from behind."},
            "defog": {
                "type": "enum", "label": "Defog",
                "values": [{"value": "off", "label": "Off"}, {"value": "auto", "label": "Auto"},
                           {"value": "on", "label": "On"}],
                "state_key": "defog", "default": "off", "setup": False,
            },
            "defog_effect": {"type": "integer", "label": "Defog Effect", "min": 0, "max": 100,
                             "state_key": "defog_effect", "default": 0, "setup": False},
            "rotation": {
                "type": "enum", "label": "Rotation",
                "values": [{"value": "0", "label": "0"}, {"value": "90", "label": "90"},
                           {"value": "180", "label": "180"}, {"value": "270", "label": "270"}],
                "state_key": "rotation", "default": "0", "setup": False,
                "help": "Clockwise, in degrees. Open streams restart when it changes.",
            },
            "mirror": {"type": "boolean", "label": "Mirror",
                       "state_key": "mirror", "default": False, "setup": False},
            "overlays_shown": {
                "type": "enum", "label": "Overlays Shown",
                "values": [{"value": "all", "label": "All"}, {"value": "text", "label": "Text only"},
                           {"value": "image", "label": "Images only"},
                           {"value": "application", "label": "Application only"},
                           {"value": "off", "label": "None"}],
                "state_key": "overlays_shown", "default": "all", "setup": False,
                "help": "Which overlay kinds the camera draws into every stream.",
            },
            "day_night_shift_level": {"type": "integer", "label": "Day to Night Level", "min": 0, "max": 100,
                                      "state_key": "day_night_shift_level", "default": 50, "setup": False,
                                      "help": "Higher switches to night mode when it is darker."},
            "day_night_dwell_time": {"type": "number", "label": "Day to Night Dwell (s)", "min": 1, "max": 600,
                                     "state_key": "day_night_dwell_time", "default": 3, "setup": False,
                                     "help": "Seconds it must stay dark before the camera switches to night mode."},
            "night_day_dwell_time": {"type": "number", "label": "Night to Day Dwell (s)", "min": 1, "max": 600,
                                     "state_key": "night_day_dwell_time", "default": 3, "setup": False,
                                     "help": "Seconds it must stay bright before the camera switches to day mode. Raise it where headlights pass at night."},
            "night_day_shift_level": {"type": "integer", "label": "Night to Day Level", "min": 0, "max": 100,
                                      "state_key": "night_day_shift_level", "default": 50, "setup": False,
                                      "help": "Only while Night to Day Autotune is off."},
            "day_night_autotune": {"type": "boolean", "label": "Night to Day Autotune",
                                   "state_key": "day_night_autotune", "default": True, "setup": False,
                                   "help": "Let the camera tune the night to day level itself."},
            "night_filter": {
                "type": "enum", "label": "Night Filter",
                "values": [{"value": "clear", "label": "Clear glass"}, {"value": "irpass", "label": "IR pass"}],
                "state_key": "night_filter", "default": "clear", "setup": False,
                "help": "Which filter sits in front of the sensor in night mode, on cameras that have both.",
            },
            "audio_enabled": {"type": "boolean", "label": "Audio Enabled",
                              "state_key": "audio_enabled", "default": False, "setup": False,
                              "help": "Include audio in the camera's streams."},
            "audio_input_gain": {"type": "number", "label": "Audio Input Gain (dB)",
                                 "state_key": "audio_input_gain", "default": 0, "setup": False},
            "audio_output_gain": {"type": "number", "label": "Audio Output Gain (dB)",
                                  "state_key": "audio_output_gain", "default": 0, "setup": False},
        },
        # The quick actions hide on a camera that lacks the capability: a fixed
        # dome without remote focus has nothing to focus, and only a PTZ model
        # has a home position.
        "actions": [
            {"id": "ir_cut_auto", "kind": "command", "command": "ir_cut_auto", "label": "Day/Night Auto",
             "icon": "sun-moon",
             "visible_when": {"key": "device.$id.ir_cut_supported", "operator": "truthy"}},
            {"id": "ir_cut_on", "kind": "command", "command": "ir_cut_on", "label": "Day Mode", "icon": "sun",
             "visible_when": {"key": "device.$id.ir_cut_supported", "operator": "truthy"}},
            {"id": "ir_cut_off", "kind": "command", "command": "ir_cut_off", "label": "Night Mode", "icon": "moon",
             "visible_when": {"key": "device.$id.ir_cut_supported", "operator": "truthy"}},
            {"id": "autofocus", "kind": "command", "command": "autofocus", "label": "Autofocus", "icon": "focus",
             "visible_when": {"key": "device.$id.focus_supported", "operator": "truthy"}},
            {"id": "pt_home", "kind": "command", "command": "pt_home", "label": "Go to Home", "icon": "house",
             "visible_when": {"key": "device.$id.ptz_supported", "operator": "truthy"}},
            {"id": "pt_stop", "kind": "command", "command": "pt_stop", "label": "Stop Pan/Tilt/Zoom",
             "icon": "octagon-x",
             "visible_when": {"key": "device.$id.ptz_supported", "operator": "truthy"}},
            {"id": "reboot", "kind": "command", "command": "reboot", "label": "Reboot Camera", "icon": "power",
             "confirm": "Reboot the camera? It will be unreachable for a minute or two."},
        ],
        "commands": {
            # Optics (fixed cameras)
            "zoom_set": {
                "label": "Set Magnification",
                "params": {"magnification": {"type": "number", "label": "Magnification", "required": True,
                                             "min": 1.0, "max": 100.0, "unit": "x",
                                             "help": "1 is wide. The top of the range is Max Magnification."}},
                "help": "Move the lens to a magnification. Run Autofocus afterwards.",
            },
            "zoom_in": {
                "label": "Zoom In (lens step)",
                "params": {
                    "step": {"type": "enum", "label": "Step", "values": ["big", "small"], "default": "big"},
                    "amount": {"type": "number", "label": "Amount", "min": 0.0, "max": 100.0,
                               "help": "A magnification amount instead of a step. Leave blank to use the step."},
                },
                "help": "Nudge the lens toward tele.",
            },
            "zoom_out": {
                "label": "Zoom Out (lens step)",
                "params": {
                    "step": {"type": "enum", "label": "Step", "values": ["big", "small"], "default": "big"},
                    "amount": {"type": "number", "label": "Amount", "min": 0.0, "max": 100.0,
                               "help": "A magnification amount instead of a step. Leave blank to use the step."},
                },
                "help": "Nudge the lens toward wide.",
            },
            "focus_set": {
                "label": "Set Focus Position",
                "params": {"position": {"type": "number", "label": "Position", "required": True,
                                        "min": 0.0, "max": 1.0, "help": "0 near to 1 far."}},
            },
            "focus_near": {
                "label": "Focus Nearer (step)",
                "params": {
                    "step": {"type": "enum", "label": "Step", "values": ["big", "small"], "default": "small"},
                    "amount": {"type": "number", "label": "Amount", "min": 0.0, "max": 1.0,
                               "help": "A focus amount instead of a step. Leave blank to use the step."},
                },
            },
            "focus_far": {
                "label": "Focus Farther (step)",
                "params": {
                    "step": {"type": "enum", "label": "Step", "values": ["big", "small"], "default": "small"},
                    "amount": {"type": "number", "label": "Amount", "min": 0.0, "max": 1.0,
                               "help": "A focus amount instead of a step. Leave blank to use the step."},
                },
            },
            "autofocus": {"label": "Autofocus", "params": {},
                          "help": "Run a focus search. Set the magnification first."},
            "focus_window": {
                "label": "Set Focus Window",
                "params": {
                    "x": {"type": "number", "label": "Left", "required": True, "min": 0.0, "max": 1.0,
                          "help": "Fraction of the picture width from the left edge."},
                    "y": {"type": "number", "label": "Top", "required": True, "min": 0.0, "max": 1.0,
                          "help": "Fraction of the picture height from the top edge."},
                    "width": {"type": "number", "label": "Width", "required": True, "min": 0.0, "max": 1.0},
                    "height": {"type": "number", "label": "Height", "required": True, "min": 0.0, "max": 1.0},
                },
                "help": "The part of the picture Autofocus optimizes for. Run Autofocus afterwards.",
            },
            "optics_reset": {
                "label": "Reset Optics",
                "params": {
                    "zoom": {"type": "boolean", "label": "Reset Zoom", "default": True},
                    "focus": {"type": "boolean", "label": "Reset Focus", "default": True},
                },
                "help": "Return the lens to its default position, after a lens change. Run Autofocus afterwards.",
            },
            "optics_calibrate": {"label": "Calibrate Optics", "params": {},
                                 "help": "Re-calibrate zoom and focus when the lens has lost its position."},
            # IR cut filter (fixed cameras through the optics or sensor parameter, PTZ models through ptz.cgi)
            "ir_cut_auto": {"label": "Day/Night Auto", "params": {},
                            "help": "Let the camera switch the IR cut filter on light level."},
            "ir_cut_on": {"label": "Day Mode", "params": {},
                          "help": "Force the IR cut filter on: color picture."},
            "ir_cut_off": {"label": "Night Mode", "params": {},
                           "help": "Force the IR cut filter off: black and white, IR sensitive."},
            # PTZ (PTZ models)
            "pt_up": {"label": "Tilt Up", "params": {"speed": {"type": "integer", "label": "Speed", "min": 1, "max": 100, "default": 50}},
                      "help": "Start tilting up. Send pt_stop to halt."},
            "pt_down": {"label": "Tilt Down", "params": {"speed": {"type": "integer", "label": "Speed", "min": 1, "max": 100, "default": 50}},
                        "help": "Start tilting down. Send pt_stop to halt."},
            "pt_left": {"label": "Pan Left", "params": {"speed": {"type": "integer", "label": "Speed", "min": 1, "max": 100, "default": 50}},
                        "help": "Start panning left. Send pt_stop to halt."},
            "pt_right": {"label": "Pan Right", "params": {"speed": {"type": "integer", "label": "Speed", "min": 1, "max": 100, "default": 50}},
                         "help": "Start panning right. Send pt_stop to halt."},
            "pt_drive": {
                "label": "Drive Pan/Tilt",
                "params": {
                    "pan": {"type": "integer", "label": "Pan Speed", "min": -100, "max": 100, "required": True,
                            "help": "-100 full left to 100 full right. 0 stops."},
                    "tilt": {"type": "integer", "label": "Tilt Speed", "min": -100, "max": 100, "required": True,
                             "help": "-100 full down to 100 full up. 0 stops."},
                },
                "help": "Continuous pan and tilt with a signed speed per axis, for a joystick.",
            },
            "pt_stop": {"label": "Stop Pan/Tilt/Zoom", "params": {},
                        "help": "Stop every ongoing pan, tilt, zoom and focus movement."},
            "pt_home": {"label": "Go to Home", "params": {}},
            "pt_absolute": {
                "label": "Go to Pan/Tilt Position",
                "params": {
                    "pan": {"type": "number", "label": "Pan", "min": -180.0, "max": 180.0, "required": True, "unit": "deg"},
                    "tilt": {"type": "number", "label": "Tilt", "min": -180.0, "max": 180.0, "required": True, "unit": "deg"},
                    "speed": {"type": "integer", "label": "Speed", "min": 1, "max": 100},
                },
            },
            "pt_relative": {
                "label": "Nudge Pan/Tilt",
                "params": {
                    "pan": {"type": "number", "label": "Pan Step", "min": -360.0, "max": 360.0, "default": 0, "unit": "deg"},
                    "tilt": {"type": "number", "label": "Tilt Step", "min": -360.0, "max": 360.0, "default": 0, "unit": "deg"},
                    "speed": {"type": "integer", "label": "Speed", "min": 1, "max": 100},
                },
                "help": "Move by degrees from where the camera is now.",
            },
            "ptz_zoom_in": {"label": "PTZ Zoom In", "params": {"speed": {"type": "integer", "label": "Speed", "min": 1, "max": 100, "default": 50}},
                            "help": "Start zooming toward tele. Send ptz_zoom_stop to halt."},
            "ptz_zoom_out": {"label": "PTZ Zoom Out", "params": {"speed": {"type": "integer", "label": "Speed", "min": 1, "max": 100, "default": 50}},
                             "help": "Start zooming toward wide. Send ptz_zoom_stop to halt."},
            "ptz_zoom_stop": {"label": "PTZ Zoom Stop", "params": {}},
            "ptz_zoom_absolute": {
                "label": "Go to PTZ Zoom",
                "params": {"zoom": {"type": "integer", "label": "Zoom", "min": 1, "max": 19999, "required": True,
                                    "help": "1 wide to 9999 tele; digital zoom continues to 19999."}},
            },
            "ptz_zoom_relative": {
                "label": "Nudge PTZ Zoom",
                "params": {"zoom": {"type": "integer", "label": "Zoom Step", "min": -19999, "max": 19999, "required": True}},
            },
            "ptz_focus_near": {"label": "PTZ Focus Near", "params": {"speed": {"type": "integer", "label": "Speed", "min": 1, "max": 100, "default": 50}},
                               "help": "Start moving focus nearer. Send ptz_focus_stop to halt."},
            "ptz_focus_far": {"label": "PTZ Focus Far", "params": {"speed": {"type": "integer", "label": "Speed", "min": 1, "max": 100, "default": 50}},
                              "help": "Start moving focus farther. Send ptz_focus_stop to halt."},
            "ptz_focus_stop": {"label": "PTZ Focus Stop", "params": {}},
            "ptz_autofocus_on": {"label": "PTZ Autofocus On", "params": {}},
            "ptz_autofocus_off": {"label": "PTZ Autofocus Off", "params": {}},
            "center": {
                "label": "Center on Point",
                "params": {
                    "x": {"type": "integer", "label": "X", "required": True, "min": 0, "max": 9999,
                          "help": "Pixel column in the stream picture."},
                    "y": {"type": "integer", "label": "Y", "required": True, "min": 0, "max": 9999,
                          "help": "Pixel row in the stream picture."},
                    "width": {"type": "integer", "label": "Picture Width", "min": 1, "max": 9999,
                              "help": "The width of the picture the point was clicked in, when it differs from the camera's default."},
                    "height": {"type": "integer", "label": "Picture Height", "min": 1, "max": 9999},
                },
                "help": "Click-to-center: move so the clicked point is in the middle of the picture.",
            },
            "area_zoom": {
                "label": "Zoom to Area",
                "params": {
                    "x": {"type": "integer", "label": "X", "required": True, "min": 0, "max": 9999},
                    "y": {"type": "integer", "label": "Y", "required": True, "min": 0, "max": 9999},
                    "zoom": {"type": "integer", "label": "Zoom Factor", "required": True, "min": 1, "max": 9999,
                             "help": "In percent of the current field of view: 300 zooms in three times, 50 zooms out to twice."},
                    "width": {"type": "integer", "label": "Picture Width", "min": 1, "max": 9999},
                    "height": {"type": "integer", "label": "Picture Height", "min": 1, "max": 9999},
                },
            },
            "preset_recall": {
                "label": "Recall Preset",
                "params": {"preset": {"type": "string", "label": "Preset", "required": True,
                                      "options_state": "preset_options"}},
            },
            "preset_save": {
                "label": "Save Preset",
                "params": {"name": {"type": "string", "label": "Name", "required": True,
                                    "help": "Saving under an existing name moves that preset here."}},
                "help": "Save the current position under a name.",
            },
            "preset_delete": {
                "label": "Delete Preset",
                "params": {"preset": {"type": "string", "label": "Preset", "required": True,
                                      "options_state": "preset_options"}},
            },
            "set_home": {"label": "Set Home Here", "params": {},
                         "help": "Save the current position as the home position."},
            "guard_tour_start": {
                "label": "Start Guard Tour",
                "params": {"tour": {"type": "string", "label": "Guard Tour", "required": True,
                                    "options_state": "guard_tour_options"}},
            },
            "guard_tour_stop": {
                "label": "Stop Guard Tour",
                "params": {"tour": {"type": "string", "label": "Guard Tour",
                                    "options_state": "guard_tour_options",
                                    "help": "Leave blank to stop every running tour."}},
            },
            "aux_command": {
                "label": "Auxiliary Command",
                "params": {"function": {"type": "string", "label": "Function", "required": True,
                                        "help": "A device-specific function name, such as a wiper or a light, as the camera's PTZ driver names it."}},
            },
            # I/O ports
            "port_on": {
                "label": "Port Active",
                "params": {"port": {"type": "child_id", "child_type": "port", "label": "Port", "required": True}},
                "help": "Put an output port in its active state (the opposite of its normal state).",
            },
            "port_off": {
                "label": "Port Inactive",
                "params": {"port": {"type": "child_id", "child_type": "port", "label": "Port", "required": True}},
                "help": "Return an output port to its normal state.",
            },
            "port_pulse": {
                "label": "Pulse Port",
                "params": {
                    "port": {"type": "child_id", "child_type": "port", "label": "Port", "required": True},
                    "duration": {"type": "integer", "label": "Duration", "min": 1, "max": 65535, "default": 500,
                                 "unit": "ms"},
                },
                "help": "Activate an output port for a time, then return it to its normal state.",
            },
            "virtual_input_on": {
                "label": "Virtual Input On",
                "params": {"input": {"type": "integer", "label": "Virtual Input", "required": True, "min": 1, "max": 64,
                                     "help": "The virtual input number an event rule on the camera watches."}},
                "help": "Activate one of the camera's virtual inputs, for a rule set up on the camera.",
            },
            "virtual_input_off": {
                "label": "Virtual Input Off",
                "params": {"input": {"type": "integer", "label": "Virtual Input", "required": True, "min": 1, "max": 64}},
            },
            # Illuminators
            "light_on": {
                "label": "Illuminator On",
                "params": {"light": {"type": "child_id", "child_type": "light", "label": "Illuminator", "required": True}},
            },
            "light_off": {
                "label": "Illuminator Off",
                "params": {"light": {"type": "child_id", "child_type": "light", "label": "Illuminator", "required": True}},
            },
            "light_intensity": {
                "label": "Set Illuminator Intensity",
                "params": {
                    "light": {"type": "child_id", "child_type": "light", "label": "Illuminator", "required": True},
                    "intensity": {"type": "integer", "label": "Intensity", "required": True, "min": 0, "max": 100, "unit": "%"},
                },
                "help": "Turns automatic intensity off.",
            },
            "light_auto_intensity": {
                "label": "Illuminator Automatic Intensity",
                "params": {
                    "light": {"type": "child_id", "child_type": "light", "label": "Illuminator", "required": True},
                    "enabled": {"type": "boolean", "label": "Automatic", "required": True},
                },
            },
            # Overlays
            "overlay_add_text": {
                "label": "Add Text Overlay",
                "params": {
                    "text": {"type": "string", "label": "Text", "required": True,
                             "help": "Up to 512 characters. Modifiers such as %c (date and time) and %F (frame rate) expand on the camera."},
                    "position": {"type": "enum", "label": "Position", "values": ["topLeft", "top", "topRight", "bottomLeft", "bottom", "bottomRight"],
                                 "default": "topLeft"},
                    "font_size": {"type": "integer", "label": "Font Size", "min": 0, "max": 200,
                                  "help": "Blank lets the camera pick a size for the resolution."},
                    "text_color": {"type": "enum", "label": "Text Color", "values": ["black", "white", "red", "transparent", "semiTransparent"],
                                   "default": "white"},
                    "background_color": {"type": "enum", "label": "Background", "values": ["black", "white", "red", "transparent", "semiTransparent"],
                                         "default": "transparent"},
                },
                "help": "Draw text into the camera's streams. The new overlay appears under Overlays.",
            },
            "overlay_set_text": {
                "label": "Change Overlay Text",
                "params": {
                    "overlay": {"type": "child_id", "child_type": "overlay", "label": "Overlay", "required": True},
                    "text": {"type": "string", "label": "Text", "required": True},
                },
            },
            "overlay_set_position": {
                "label": "Move Overlay",
                "params": {
                    "overlay": {"type": "child_id", "child_type": "overlay", "label": "Overlay", "required": True},
                    "position": {"type": "enum", "label": "Position", "values": ["topLeft", "top", "topRight", "bottomLeft", "bottom", "bottomRight"], "required": True},
                },
            },
            "overlay_add_image": {
                "label": "Add Image Overlay",
                "params": {
                    "image": {"type": "string", "label": "Image", "required": True,
                              "options_state": "overlay_image_options",
                              "help": "An image uploaded to the camera under Overlays."},
                    "position": {"type": "enum", "label": "Position", "values": ["topLeft", "top", "topRight", "bottomLeft", "bottom", "bottomRight"],
                                 "default": "bottomLeft"},
                },
            },
            "overlay_remove": {
                "label": "Remove Overlay",
                "params": {"overlay": {"type": "child_id", "child_type": "overlay", "label": "Overlay", "required": True}},
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
        self._auth: Any = None
        self._apis: set[str] = set()
        self._properties: dict[str, str] = {}
        self._optics_id = ""
        self._optics_caps: set[str] = set()
        self._ptz = False
        self._ptz_available = False
        self._ptz_digital = False
        self._ptz_support: dict[str, str] = {}
        self._lights_absent = False
        self._storage_faults: dict[str, bool] = {}
        self._presets: dict[str, str] = {}          # number -> name
        self._tours: dict[str, dict[str, str]] = {}  # G# -> params
        self._ports: dict[str, dict[str, Any]] = {}  # port id -> last item
        self._port_children: dict[str, str] = {}     # port id -> child id
        self._port_mgmt = False
        self._lights: dict[str, str] = {}            # lightID -> child id
        self._light_ranges: dict[str, tuple[int, int]] = {}
        self._overlays: dict[str, str] = {}          # identity -> child id
        self._overlay_kinds: dict[str, str] = {}
        self._dynamic_overlay = False
        self._views: dict[int, dict[str, Any]] = {}
        self._source_rotation = False
        self._audio = False
        self._daynight_caps: dict[str, bool] = {}
        self._daynight_config: dict[str, Any] = {}
        self._hardware_faults: dict[str, bool] = {}
        self._poll_count = 0
        self._event_task: asyncio.Task | None = None
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
        return _int(self.config.get("port", 80), 80) or 80

    @property
    def _scheme(self) -> str:
        return "https" if self.config.get("ssl") else "http"

    @property
    def _username(self) -> str:
        return str(self.config.get("username", "") or "")

    @property
    def _password(self) -> str:
        return str(self.config.get("password", "") or "")

    @property
    def _cam(self) -> int:
        """The controlled view area, 1-based (ptz.cgi, overlays, streams)."""
        return max(1, _int(self.config.get("camera", 1), 1) or 1)

    @property
    def _src(self) -> int:
        """The matching 0-based index (ImageSource.I#, Image.I#, daynight channel)."""
        return self._cam - 1

    def _base_url(self) -> str:
        return f"{self._scheme}://{self._host}:{self._port}"

    def _auth_fault(self) -> ConnectionFaultError:
        if not self._username:
            message = (
                "This camera needs a login and none is entered. Create an "
                "account with operator or administrator rights under System > "
                "Accounts in the camera's web interface (or use root), enter it "
                "under Edit Device and press Reconnect."
            )
        else:
            message = (
                "The camera refused the login. Check the username and password. "
                "An ONVIF account does not work here; use a camera account from "
                "System > Accounts, or root."
            )
        return ConnectionFaultError(message, code="auth_failed")

    # ── Connection lifecycle ──

    async def _create_transport(self, transport_type: str) -> None:
        host, port = self._host, self._port
        if not host:
            raise ConnectionFaultError("No IP address configured", code="invalid_config")
        if not self._username or not self._password:
            # Never send a login that cannot succeed.
            raise self._auth_fault()
        if not await self._verify_reachable(host, port):
            raise ConnectionError(f"{host}:{port} is not responding")
        self._auth = httpx.DigestAuth(self._username, self._password)
        self._client = httpx.AsyncClient(
            base_url=self._base_url(),
            verify=bool(self.config.get("verify_ssl", False)),
            timeout=httpx.Timeout(10.0, connect=5.0),
        )

    async def _post_connect(self) -> None:
        try:
            await self._read_identity()
            await self._read_api_list()
            await self._read_properties()
        except VapixError as exc:
            if exc.not_authorized:
                raise self._auth_fault() from exc
            raise ConnectionError(f"The camera answered with an error: {exc}") from exc
        except httpx.TransportError as exc:
            raise ConnectionError(f"{self._host} is not responding: {exc}") from exc
        log.info(
            f"[{self.device_id}] Connected to {self.get_state('product_name') or 'Axis camera'} "
            f"at {self._host}:{self._port} (AXIS OS {self.get_state('firmware_version')}), "
            f"apis={sorted(self._apis)}"
        )

    async def _initial_sync(self) -> None:
        try:
            await self._read_views()
            await self._read_stream_profiles()
            await self._read_optics(initial=True)
            await self._read_daynight(initial=True)
            await self._read_image()
            await self._read_ptz(initial=True)
            await self._read_ports()
            await self._read_lights()
            await self._read_overlays()
            await self._read_audio(initial=True)
            await self._read_time()
        except VapixError as exc:
            if exc.not_authorized:
                raise self._auth_fault() from exc
            raise ConnectionError(f"The camera answered with an error: {exc}") from exc
        except httpx.TransportError as exc:
            raise ConnectionError(f"{self._host} is not responding: {exc}") from exc
        self._start_event_loop()

    def _link_alive(self) -> bool:
        return self._client is not None

    async def _close_session(self) -> None:
        await self._stop_event_loop()
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _stop_push(self) -> None:
        await super()._stop_push()
        await self._stop_event_loop()

    async def _liveness_probe(self) -> None:
        """The time API is cheap and keeps the clock offset fresh; a camera
        without it answers basic device information instead."""
        if self._client is None:
            raise ConnectionError("Not connected")
        if API_TIME in self._apis:
            await self._read_time()
        else:
            await self._json(CGI_DEVICE_INFO, "getProperties", {"propertyList": ["Version"]})

    # ── HTTP plumbing ──

    async def _request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None, timeout: float | None = None,
    ) -> httpx.Response:
        """One authenticated request. A Basic-only camera is followed over
        HTTPS only; a 401 after that is the typed auth failure."""
        client = self._client
        if client is None:
            raise ConnectionError("Not connected")
        kwargs: dict[str, Any] = {"params": params, "auth": self._auth}
        if json_body is not None:
            kwargs["json"] = json_body
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(timeout, connect=5.0)
        resp = await client.request(method, path, **kwargs)
        if resp.status_code == 401 and isinstance(self._auth, httpx.DigestAuth):
            challenge = resp.headers.get("WWW-Authenticate", "")
            if "digest" not in challenge.lower() and "basic" in challenge.lower():
                if self._scheme != "https":
                    raise ConnectionFaultError(
                        "The camera only accepts Basic authentication, which sends the "
                        "password in the clear. Turn on Use HTTPS, or set the camera's "
                        "authentication policy to Digest.",
                        code="invalid_config",
                    )
                log.info(f"[{self.device_id}] Camera wants Basic authentication over HTTPS")
                self._auth = httpx.BasicAuth(self._username, self._password)
                kwargs["auth"] = self._auth
                resp = await client.request(method, path, **kwargs)
        if resp.status_code in (401, 403):
            raise VapixError("The camera refused the login", http_status=resp.status_code)
        return resp

    async def _json(
        self, path: str, method: str, params: dict[str, Any] | None = None, *,
        api_version: str = "1.0", timeout: float | None = None,
    ) -> Any:
        """POST one VAPIX JSON method and return its ``data``. A JSON error
        object, whatever HTTP status carries it, is a VapixError."""
        body: dict[str, Any] = {"apiVersion": api_version, "context": "openavc", "method": method}
        if params is not None:
            body["params"] = params
        resp = await self._request("POST", path, json_body=body, timeout=timeout)
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict) and "error" in payload:
            err = payload.get("error") or {}
            raise VapixError(
                str(err.get("message") or ""), code=err.get("code", ""), http_status=resp.status_code,
            )
        if resp.status_code >= 400 or not isinstance(payload, dict):
            raise VapixError(f"HTTP {resp.status_code} from {path}", http_status=resp.status_code)
        return payload.get("data", {})

    async def _param_list(self, group: str) -> dict[str, str]:
        resp = await self._request("GET", CGI_PARAM, params={"action": "list", "group": group})
        if resp.status_code >= 400:
            raise VapixError(f"HTTP {resp.status_code} listing {group}", http_status=resp.status_code)
        return _parse_param_list(resp.text)

    async def _param_update(self, **pairs: str) -> None:
        params = {"action": "update", **pairs}
        resp = await self._request("GET", CGI_PARAM, params=params)
        text = resp.text.strip()
        if resp.status_code >= 400 or text.startswith("# Error"):
            raise VapixError(text.lstrip("# ").strip() or f"HTTP {resp.status_code}", http_status=resp.status_code)

    async def _text_get(self, path: str, params: dict[str, Any]) -> str:
        """ptz.cgi / ptzconfig.cgi / port.cgi: 204 or 200 on success, an
        ``Error:`` body on refusal."""
        resp = await self._request("GET", path, params=params)
        text = resp.text.strip()
        if resp.status_code >= 400:
            raise VapixError(text or f"HTTP {resp.status_code}", http_status=resp.status_code)
        if text.startswith("Error") or text.startswith("# Error"):
            raise VapixError(text.split(":", 1)[-1].strip() or text)
        return text

    # ── Connect-time reads ──

    async def _read_identity(self) -> None:
        try:
            data = await self._json(CGI_DEVICE_INFO, "getAllProperties")
            props = data.get("propertyList") or {}
        except VapixError as exc:
            if exc.not_authorized:
                raise
            # Pre-8.40 firmware: the Brand group carries the same identity.
            log.info(f"[{self.device_id}] basicdeviceinfo unavailable ({exc}); reading the Brand group")
            brand = await self._param_list("Brand")
            props = {
                "ProdNbr": brand.get("Brand.ProdNbr", ""),
                "ProdFullName": brand.get("Brand.ProdFullName", ""),
                "Brand": brand.get("Brand.Brand", ""),
            }
            firmware = await self._param_list("Properties.Firmware.Version")
            props["Version"] = firmware.get("Properties.Firmware.Version", "")
            serial = await self._param_list("Properties.System.SerialNumber")
            props["SerialNumber"] = serial.get("Properties.System.SerialNumber", "")
        self.set_states({
            "model": str(props.get("ProdNbr", "")),
            "product_name": str(props.get("ProdFullName", "")),
            "firmware_version": str(props.get("Version", "")),
            "serial_number": str(props.get("SerialNumber", "")),
            "hardware_id": str(props.get("HardwareID", "")),
        })

    async def _read_api_list(self) -> None:
        apis: set[str] = set()
        try:
            data = await self._json(CGI_API_DISCOVERY, "getApiList")
            for entry in data.get("apiList") or []:
                api_id = str(entry.get("id", "")).strip()
                if api_id:
                    apis.add(api_id)
        except VapixError as exc:
            if exc.not_authorized:
                raise
            log.info(f"[{self.device_id}] API discovery unavailable ({exc}); reading Properties instead")
        self._apis = apis
        self.set_state("api_list", ", ".join(sorted(apis)))

    async def _read_properties(self) -> None:
        self._properties = await self._param_list("Properties")
        props = self._properties
        if props.get("Properties.PTZ.PTZ", "").lower() == "yes":
            self._apis.add(API_PTZ)
        if props.get("Properties.LightControl.LightControl2", "").lower() == "yes":
            self._apis.add(API_LIGHT)
        self._dynamic_overlay = props.get("Properties.DynamicOverlay.DynamicOverlay", "").lower() == "yes"
        self._audio = props.get("Properties.Audio.Audio", "").lower() == "yes"
        self.set_state("audio_supported", self._audio)
        if self._apis:
            self.set_state("api_list", ", ".join(sorted(self._apis)))

    # ── View areas + streams ──

    def _stream_urls(self, camera: int) -> dict[str, str]:
        host = self._host
        rtsp_port = _int(self.config.get("rtsp_port", 554), 554) or 554
        profile = str(self.config.get("stream_profile", "") or "").strip()
        query = f"camera={camera}"
        if profile:
            query += f"&streamprofile={quote(profile, safe='')}"
        port = "" if rtsp_port == 554 else f":{rtsp_port}"
        rtsp = f"rtsp://{host}{port}{RTSP_PATH}?{query}"
        snapshot = f"{self._base_url()}{CGI_SNAPSHOT}?camera={camera}"
        mjpeg = f"{self._base_url()}{CGI_MJPEG}?{query}"
        if self.config.get("credentials_in_stream_url"):
            rtsp = _with_credentials(rtsp, self._username, self._password)
            snapshot = _with_credentials(snapshot, self._username, self._password)
            mjpeg = _with_credentials(mjpeg, self._username, self._password)
        return {"preview_url": rtsp, "preview_format": "rtsp", "snapshot_url": snapshot, "mjpeg_url": mjpeg}

    async def _read_views(self) -> None:
        views: dict[int, dict[str, Any]] = {}
        if API_VIEW_AREA in self._apis:
            try:
                data = await self._json(CGI_VIEW_AREA_INFO, "list")
                for area in data.get("viewAreas") or []:
                    camera = _int(area.get("camera"))
                    if camera is None:
                        continue
                    geometry = area.get("rectangularGeometry") or {}
                    resolution = ""
                    if geometry:
                        resolution = (
                            f"{geometry.get('horizontalSize')}x{geometry.get('verticalSize')}"
                            f"+{geometry.get('horizontalOffset')}+{geometry.get('verticalOffset')}"
                        )
                    views[camera] = {
                        "source": _int(area.get("source"), 0),
                        "configurable": bool(area.get("configurable", False)),
                        "resolution": resolution,
                    }
            except VapixError as exc:
                if exc.not_authorized:
                    raise
                log.info(f"[{self.device_id}] View area list unavailable: {exc}")
        if not views:
            count = _int(self._properties.get("Image.NbrOfConfigs"), None)
            if count is None:
                count = _int((await self._param_list("Image.NbrOfConfigs")).get("Image.NbrOfConfigs"), 1) or 1
            for camera in range(1, max(1, count) + 1):
                views[camera] = {"source": 0, "configurable": False, "resolution": ""}
        enabled_flags: dict[int, bool] = {}
        try:
            for key, value in (await self._param_list("Image.*.Enabled")).items():
                m = re.match(r"Image\.I(\d+)\.Enabled$", key)
                if m:
                    enabled_flags[int(m.group(1)) + 1] = _bool_text(value)
        except VapixError as exc:
            if exc.not_authorized:
                raise
        for camera in list(self._views):
            if camera not in views:
                self.deregister_child("view", camera)
        for camera, values in views.items():
            enabled = enabled_flags.get(camera, True)
            values["enabled"] = enabled
            # A view area the camera lists but has turned off is an unused slot, not a
            # fault: the camera answers for it, there is just no video behind it.
            values.update(self.child_fault() if enabled else self.child_fault(
                CHILD_NOT_FITTED,
                f"View area {camera} is turned off in the camera. Turn it on under "
                "Video > View areas to use it."))
            values.update(self._stream_urls(camera))
            if camera in self._views:
                self.set_child_state_batch("view", camera, values)
            else:
                self.register_child("view", camera, initial_state=values)
        self._views = views
        self.set_state("view_area_count", len(views))
        urls = self._stream_urls(self._cam)
        self.set_states(urls)

    async def _read_stream_profiles(self) -> None:
        names: list[str] = []
        if API_STREAM_PROFILES in self._apis:
            try:
                data = await self._json(CGI_STREAM_PROFILE, "list", {"streamProfileName": []})
                names = [str(p.get("name", "")) for p in data.get("streamProfile") or [] if p.get("name")]
            except VapixError as exc:
                if exc.not_authorized:
                    raise
                log.debug(f"[{self.device_id}] streamprofile.cgi list failed: {exc}")
        if not names:
            try:
                params = await self._param_list("StreamProfile")
                names = [v for k, v in sorted(params.items()) if k.endswith(".Name")]
            except VapixError as exc:
                if exc.not_authorized:
                    raise
        self.set_state("stream_profile_options", json.dumps(names))

    # ── Optics (fixed cameras) ──

    def _has_optics(self) -> bool:
        return bool(self._optics_id)

    async def _read_optics(self, *, initial: bool = False) -> None:
        if initial:
            self._optics_id = ""
            self._optics_caps = set()
            if API_OPTICS in self._apis or not self._apis:
                try:
                    data = await self._json(CGI_OPTICS, "getCapabilities", api_version="1")
                    optics = data.get("optics") or []
                    entry = optics[self._src] if self._src < len(optics) else (optics[0] if optics else None)
                    if entry:
                        self._optics_id = str(entry.get("opticsId", "0"))
                        self._optics_caps = {str(c) for c in entry.get("capabilities") or []}
                        max_mag = _float(entry.get("maxMagnification"))
                        if max_mag is not None:
                            self.set_state("max_magnification", max_mag)
                except VapixError as exc:
                    if exc.not_authorized:
                        raise
                    log.info(f"[{self.device_id}] No optics control: {exc}")
            self.set_states({
                "zoom_supported": "zoom" in self._optics_caps,
                "focus_supported": "focus" in self._optics_caps,
                "ir_cut_supported": (
                    "irCutFilter" in self._optics_caps
                    or self._ptz
                    or API_DAYNIGHT in self._apis
                    or bool(self._properties.get("ImageSource.I0.DayNight.IrCutFilter"))
                ),
            })
        if not self._optics_id:
            return
        data = await self._json(CGI_OPTICS, "getOptics", api_version="1")
        for entry in data.get("optics") or []:
            if str(entry.get("opticsId", "")) != self._optics_id:
                continue
            updates: dict[str, Any] = {}
            if "magnification" in entry:
                updates["magnification"] = _float(entry["magnification"])
            if "zoomMoving" in entry:
                updates["zoom_moving"] = bool(entry["zoomMoving"])
            if "focusPosition" in entry:
                updates["focus_position"] = _float(entry["focusPosition"])
            if "focusMoving" in entry:
                updates["focus_moving"] = bool(entry["focusMoving"])
            if entry.get("irCutFilterState"):
                updates["ir_cut_filter"] = str(entry["irCutFilterState"]).lower()
            if "temperatureCompensation" in entry:
                updates["temperature_compensation"] = bool(entry["temperatureCompensation"])
            if "irCompensation" in entry:
                updates["ir_compensation"] = bool(entry["irCompensation"])
            self.set_states(updates)

    async def _optics(self, method: str, fields: dict[str, Any] | None = None) -> Any:
        if not self._optics_id:
            raise VapixCommandError("This camera has no remote lens control.")
        entry = {"opticsId": self._optics_id, **(fields or {})}
        return await self._json(CGI_OPTICS, method, {"optics": [entry]}, api_version="1")

    # ── Day / night ──

    async def _read_daynight(self, *, initial: bool = False) -> None:
        if API_DAYNIGHT not in self._apis:
            # Older AXIS OS has no daynight.cgi; the day to night threshold is
            # the sensor's DayNight.ShiftLevel parameter, and the IR cut filter
            # of a camera without optics control sits beside it.
            await self._read_ir_cut_param(read_filter=not self._optics_id)
            return
        if initial:
            try:
                data = await self._json(CGI_DAYNIGHT, "getCapabilities", {"channel": self._src}, api_version="1.2")
                for entry in data if isinstance(data, list) else []:
                    if _int(entry.get("channel")) == self._src:
                        self._daynight_caps = {
                            "autotune": bool(entry.get("AutotuneSupport", False)),
                            "irpass": bool(entry.get("IrPassSupport", False)),
                            "night_day_level": bool(entry.get("NightDayShiftLevelSupport", False)),
                        }
            except VapixError as exc:
                if exc.not_authorized:
                    raise
                log.info(f"[{self.device_id}] DayNight capabilities unavailable: {exc}")
        data = await self._json(CGI_DAYNIGHT, "getConfiguration", {"channel": self._src}, api_version="1.2")
        for entry in data if isinstance(data, list) else []:
            if _int(entry.get("channel")) != self._src:
                continue
            self._daynight_config = dict(entry)
            updates: dict[str, Any] = {}
            if "DayNightShiftLevel" in entry:
                updates["day_night_shift_level"] = _int(entry["DayNightShiftLevel"])
            if "DayNightDwellTime" in entry:
                updates["day_night_dwell_time"] = _float(entry["DayNightDwellTime"])
            if "NightDayDwellTime" in entry:
                updates["night_day_dwell_time"] = _float(entry["NightDayDwellTime"])
            if "NightDayShiftLevel" in entry:
                updates["night_day_shift_level"] = _int(entry["NightDayShiftLevel"])
            if "Autotune" in entry:
                updates["day_night_autotune"] = bool(entry["Autotune"])
            if entry.get("NightFilter") in ("clear", "irpass"):
                updates["night_filter"] = entry["NightFilter"]
            self.set_states(updates)
        if initial and not self._optics_id and not self._ptz:
            await self._read_ir_cut_param()

    async def _read_ir_cut_param(self, *, read_filter: bool = True) -> None:
        """The sensor's DayNight parameter group: the day to night threshold,
        and the IR cut filter of a fixed camera without optics control."""
        try:
            params = await self._param_list(f"ImageSource.I{self._src}.DayNight")
        except VapixError as exc:
            if exc.not_authorized:
                raise
            return
        level = _int(params.get(f"ImageSource.I{self._src}.DayNight.ShiftLevel"))
        if level is not None:
            self.set_state("day_night_shift_level", level)
        value = params.get(f"ImageSource.I{self._src}.DayNight.IrCutFilter", "").lower()
        if value and read_filter:
            self.set_states({
                "ir_cut_supported": True,
                "ir_cut_filter": {"yes": "on", "no": "off"}.get(value, value),
            })

    async def _set_ir_cut(self, mode: str) -> None:
        mode = mode.lower()
        if mode not in ("on", "off", "auto"):
            raise VapixCommandError("The IR cut filter is on, off or auto.")
        if self._optics_id and "irCutFilter" in self._optics_caps:
            await self._optics("setIrCutFilterState", {"irCutFilterState": mode})
            await self._read_optics()
            return
        if self._ptz:
            await self._ptz_get({"ircutfilter": mode})
            self.set_state("ir_cut_filter", mode)
            return
        if not self.get_state("ir_cut_supported"):
            raise VapixCommandError("This camera does not report an IR cut filter it can control.")
        wire = {"on": "yes", "off": "no"}.get(mode, mode)
        await self._param_update(**{f"ImageSource.I{self._src}.DayNight.IrCutFilter": wire})
        await self._read_ir_cut_param()

    # ── Image (sensor parameters, appearance) ──

    async def _read_image(self) -> None:
        prefix = f"ImageSource.I{self._src}."
        try:
            params = await self._param_list(f"ImageSource.I{self._src}")
        except VapixError as exc:
            if exc.not_authorized:
                raise
            log.info(f"[{self.device_id}] ImageSource parameters unavailable: {exc}")
            return
        updates: dict[str, Any] = {}
        for key, (name, kind) in SENSOR_SETTINGS.items():
            raw = params.get(f"{prefix}Sensor.{name}")
            if raw is None:
                continue
            if kind == "int":
                updates[key] = _int(raw)
            elif kind in ("onoff", "yesno"):
                updates[key] = _bool_text(raw)
            else:
                updates[key] = raw.lower()
        self._source_rotation = params.get(f"{prefix}SourceRotation", "").lower() == "yes"
        rotation = params.get(f"{prefix}Rotation")
        if rotation is not None:
            updates["rotation"] = _int(rotation)
        try:
            appearance = await self._param_list(f"Image.I{self._src}.Appearance")
        except VapixError as exc:
            if exc.not_authorized:
                raise
            appearance = {}
        aprefix = f"Image.I{self._src}.Appearance."
        if rotation is None and appearance.get(f"{aprefix}Rotation") is not None:
            updates["rotation"] = _int(appearance[f"{aprefix}Rotation"])
        if appearance.get(f"{aprefix}MirrorEnabled") is not None:
            updates["mirror"] = _bool_text(appearance[f"{aprefix}MirrorEnabled"])
        if appearance.get(f"{aprefix}Overlays"):
            updates["overlays_shown"] = appearance[f"{aprefix}Overlays"]
        if updates:
            self.set_states(updates)

    # ── PTZ (PTZ models) ──

    async def _ptz_get(self, params: dict[str, Any]) -> str:
        if not self._ptz:
            raise VapixCommandError("This camera has no pan/tilt/zoom control.")
        return await self._text_get(CGI_PTZ, {"camera": self._cam, **params})

    async def _read_ptz(self, *, initial: bool = False) -> None:
        if initial:
            props = self._properties
            self._ptz_available = API_PTZ in self._apis or props.get("Properties.PTZ.PTZ", "").lower() == "yes"
            self._ptz_digital = props.get("Properties.PTZ.DigitalPTZ", "").lower() == "yes"
            enabled = self._ptz_available
            driver = ""
            if self._ptz_available:
                # A fixed camera's digital PTZ is off until turned on; the
                # enable flag says so, and ptz.cgi answers "PTZ disabled".
                try:
                    flags = await self._param_list(f"PTZ.ImageSource.I{self._src}.PTZEnabled")
                    flag = flags.get(f"PTZ.ImageSource.I{self._src}.PTZEnabled")
                    if flag is not None:
                        enabled = _bool_text(flag)
                except VapixError as exc:
                    if exc.not_authorized:
                        raise
                try:
                    driver = (await self._text_get(CGI_PTZ, {"camera": self._cam, "whoami": 1})).strip()
                except VapixError as exc:
                    if exc.not_authorized:
                        raise
                    driver = str(exc)
                if "disabled" in driver.lower():
                    enabled = False
            self._ptz = self._ptz_available and enabled
            self.set_states({
                "ptz_supported": self._ptz,
                "ptz_digital": self._ptz_digital,
                "ptz_enabled": enabled if self._ptz_available else False,
                "ptz_driver": driver,
            })
            if self._ptz:
                try:
                    self._ptz_support = await self._param_list(f"PTZ.Support.S{self._cam}")
                except VapixError as exc:
                    if exc.not_authorized:
                        raise
                    self._ptz_support = {}
                await self._refresh_presets()
                await self._read_guard_tours()
            else:
                self.set_states({
                    "preset_count": 0, "preset_options": "[]",
                    "guard_tour_options": "[]", "guard_tour_running": "",
                })
        if not self._ptz:
            return
        text = await self._ptz_get({"query": "position"})
        values = _parse_param_list(text)
        updates: dict[str, Any] = {}
        if "pan" in values:
            updates["pan_position"] = _float(values["pan"])
        if "tilt" in values:
            updates["tilt_position"] = _float(values["tilt"])
        if "zoom" in values:
            updates["zoom_level"] = _int(values["zoom"])
        if "autofocus" in values:
            updates["autofocus"] = _bool_text(values["autofocus"])
        if updates:
            self.set_states(updates)

    async def _refresh_presets(self) -> None:
        if not self._ptz:
            return
        text = await self._ptz_get({"query": "presetposcam"})
        presets: dict[str, str] = {}
        for line in text.splitlines():
            m = re.match(r"\s*presetposno(\d+)\s*=\s*(.*)$", line)
            if m:
                presets[m.group(1)] = m.group(2).strip()
        self._presets = presets
        self.set_states({
            "preset_count": len(presets),
            "preset_options": json.dumps(
                [{"value": n, "label": f"{name} ({n})" if name else n} for n, name in presets.items()]
            ),
        })

    async def _read_guard_tours(self) -> None:
        if not self._ptz:
            return
        try:
            params = await self._param_list("GuardTour")
        except VapixError as exc:
            if exc.not_authorized:
                raise
            params = {}
        tours: dict[str, dict[str, str]] = {}
        for key, value in params.items():
            m = re.match(r"GuardTour\.(G\d+)\.(\w+)$", key)
            if m:
                tours.setdefault(m.group(1), {})[m.group(2)] = value
        self._tours = tours
        running = [t.get("Name", g) for g, t in tours.items() if t.get("Running", "").lower() == "yes"]
        self.set_states({
            "guard_tour_options": json.dumps(
                [{"value": g, "label": t.get("Name") or g} for g, t in sorted(tours.items())]
            ),
            "guard_tour_running": running[0] if running else "",
        })

    # ── I/O ports ──

    def _port_active(self, item: dict[str, Any]) -> bool | None:
        state = str(item.get("state", "")).lower()
        normal = str(item.get("normalState", "")).lower()
        if state not in ("open", "closed") or normal not in ("open", "closed"):
            return None
        return state != normal

    async def _read_ports(self) -> None:
        items: list[dict[str, Any]] = []
        if API_IO in self._apis or not self._apis:
            try:
                data = await self._json(CGI_PORT_MGMT, "getPorts")
                items = list(data.get("items") or [])
                self._port_mgmt = True
            except VapixError as exc:
                if exc.not_authorized:
                    raise
                self._port_mgmt = False
                log.info(f"[{self.device_id}] I/O port management unavailable ({exc}); using port.cgi")
        if not self._port_mgmt:
            items = await self._read_ports_legacy()
        wanted: dict[str, dict[str, Any]] = {}
        for item in items:
            port_id = str(item.get("port", "")).strip()
            if port_id:
                wanted[port_id] = item
        for port_id, child_id in list(self._port_children.items()):
            if port_id not in wanted:
                self.deregister_child("port", child_id)
                del self._port_children[port_id]
                self._ports.pop(port_id, None)
        for port_id, item in wanted.items():
            child_id = _child_id_for(port_id)
            direction = str(item.get("direction", "")).lower()
            values = {
                "name": str(item.get("name", "") or f"Port {port_id}"),
                "direction": direction if direction in ("input", "output") else None,
                "state": str(item.get("state", "")).lower() or None,
                "normal_state": str(item.get("normalState", "")).lower() or None,
                "active": self._port_active(item),
                "usage": str(item.get("usage", "") or ""),
                "configurable": bool(item.get("configurable", False)),
            }
            if port_id in self._port_children:
                self.set_child_state_batch("port", child_id, values)
            else:
                self.register_child("port", child_id, initial_state=values)
                self._port_children[port_id] = child_id
            self._ports[port_id] = item
        self.set_state("port_count", len(wanted))

    async def _read_ports_legacy(self) -> list[dict[str, Any]]:
        """Pre-9.70 firmware: the port roster from the IOPort parameter group
        and the states from port.cgi (1-based there, 0-based in IOPort.I#)."""
        try:
            params = await self._param_list("IOPort")
        except VapixError as exc:
            if exc.not_authorized:
                raise
            return []
        indexes = sorted({int(m.group(1)) for k in params if (m := re.match(r"IOPort\.I(\d+)\.", k))})
        if not indexes:
            return []
        numbers = ",".join(str(i + 1) for i in indexes)
        states = _parse_param_list(await self._text_get(CGI_PORT, {"check": numbers}))
        items = []
        for i in indexes:
            prefix = f"IOPort.I{i}."
            direction = params.get(f"{prefix}Direction", "input").lower()
            closed = states.get(f"port{i + 1}") == "1"
            if direction == "output":
                active_closed = params.get(f"{prefix}Output.Active", "closed").lower() == "closed"
                name = params.get(f"{prefix}Output.Name", "")
            else:
                active_closed = params.get(f"{prefix}Input.Trig", "closed").lower() == "closed"
                name = params.get(f"{prefix}Input.Name", "")
            items.append({
                "port": str(i),
                "name": name,
                "direction": direction,
                "state": "closed" if closed else "open",
                "normalState": "open" if active_closed else "closed",
                "configurable": params.get(f"{prefix}Configurable", "no").lower() == "yes",
                "usage": params.get(f"{prefix}Usage", ""),
            })
        return items

    def _port_for(self, child_id: str) -> tuple[str, dict[str, Any]]:
        for port_id, cid in self._port_children.items():
            if cid == str(child_id).strip():
                return port_id, self._ports.get(port_id, {})
        raise VapixCommandError("Pick an I/O port.")

    async def _set_port(self, child_id: str, active: bool) -> None:
        port_id, item = self._port_for(child_id)
        if str(item.get("direction", "")).lower() != "output":
            raise VapixCommandError("That port is an input; only an output port can be set.")
        if self._port_mgmt:
            normal = str(item.get("normalState", "open")).lower()
            wanted = ("closed" if normal == "open" else "open") if active else normal
            await self._json(CGI_PORT_MGMT, "setPorts", {"ports": [{"port": port_id, "state": wanted}]})
        else:
            number = int(port_id) + 1
            await self._text_get(CGI_PORT, {"action": f"{number}:{'/' if active else chr(92)}"})
        await self._read_ports()

    async def _pulse_port(self, child_id: str, duration_ms: int) -> None:
        port_id, item = self._port_for(child_id)
        if str(item.get("direction", "")).lower() != "output":
            raise VapixCommandError("That port is an input; only an output port can be pulsed.")
        duration_ms = max(1, min(65535, int(duration_ms)))
        if self._port_mgmt:
            normal = str(item.get("normalState", "open")).lower()
            active = "closed" if normal == "open" else "open"
            await self._json(CGI_PORT_MGMT, "setStateSequence", {
                "port": port_id,
                "sequence": [{"state": active, "time": duration_ms}, {"state": normal, "time": 0}],
            })
        else:
            number = int(port_id) + 1
            await self._text_get(CGI_PORT, {"action": f"{number}:/{duration_ms}\\"})

    # ── Illuminators ──

    async def _read_lights(self) -> None:
        if API_LIGHT not in self._apis or self._lights_absent:
            self.set_state("light_count", 0)
            return
        try:
            data = await self._json(CGI_LIGHT, "getLightInformation", {})
        except VapixError as exc:
            if exc.not_authorized:
                raise
            # The API is there on cameras with no illuminator; the answer is
            # error 1005 "No light hardware found". Say so once and stop asking.
            self._lights_absent = True
            self.set_state("light_count", 0)
            log.info(f"[{self.device_id}] No illuminator on this camera: {exc}")
            return
        items = {str(i.get("lightID", "")): i for i in data.get("items") or [] if i.get("lightID")}
        for light_id, child_id in list(self._lights.items()):
            if light_id not in items:
                self.deregister_child("light", child_id)
                del self._lights[light_id]
        for light_id, item in items.items():
            child_id = _child_id_for(light_id)
            values: dict[str, Any] = {
                "light_type": str(item.get("lightType", "")),
                "enabled": bool(item.get("enabled", False)),
                "on": bool(item.get("lightState", False)),
                "auto_intensity": bool(item.get("automaticIntensityMode", False)),
                "sync_day_night": bool(item.get("synchronizeDayNightMode", False)),
                "led_count": _int(item.get("nrOfLEDs"), 0),
                "error": str(item.get("errorInfo", "")) if item.get("error") else "",
            }
            new = light_id not in self._lights
            if new:
                self.register_child("light", child_id, initial_state=values)
                self._lights[light_id] = child_id
                try:
                    rng = await self._json(CGI_LIGHT, "getValidIntensity", {"lightID": light_id})
                    ranges = rng.get("ranges") or []
                    if ranges:
                        self._light_ranges[light_id] = (
                            _int(ranges[0].get("low"), 0) or 0, _int(ranges[-1].get("high"), 100) or 100,
                        )
                except VapixError as exc:
                    if exc.not_authorized:
                        raise
            else:
                self.set_child_state_batch("light", child_id, values)
            try:
                cur = await self._json(CGI_LIGHT, "getCurrentIntensity", {"lightID": light_id})
                intensity = _int(cur.get("intensity", cur.get("intesity")))
                if intensity is not None:
                    self.set_child_state("light", child_id, "intensity", intensity)
            except VapixError as exc:
                if exc.not_authorized:
                    raise
        self.set_state("light_count", len(items))

    def _light_for(self, child_id: str) -> str:
        for light_id, cid in self._lights.items():
            if cid == str(child_id).strip():
                return light_id
        raise VapixCommandError("Pick an illuminator.")

    # ── Overlays ──

    async def _read_overlays(self) -> None:
        if not self._dynamic_overlay:
            self.set_state("overlay_image_options", "[]")
            return
        try:
            data = await self._json(CGI_OVERLAY, "list", {})
        except VapixError as exc:
            if exc.not_authorized:
                raise
            log.info(f"[{self.device_id}] Overlay list unavailable: {exc}")
            return
        images = [str(p) for p in data.get("imageFiles") or []]
        self.set_state("overlay_image_options", json.dumps(
            [{"value": p, "label": p.rsplit("/", 1)[-1]} for p in images]
        ))
        wanted: dict[str, dict[str, Any]] = {}
        for entry in data.get("textOverlays") or []:
            identity = str(entry.get("identity", ""))
            position = entry.get("position")
            wanted[identity] = {
                "kind": "text",
                "camera": _int(entry.get("camera"), 1),
                "text": str(entry.get("text", "")),
                "position": position if isinstance(position, str) else json.dumps(position),
                "font_size": _int(entry.get("fontSize")),
                "text_color": str(entry.get("textColor", "")),
                "background_color": str(entry.get("textBGColor", "")),
                "image_path": "",
                "visible": bool(entry.get("visible", True)),
            }
        for entry in data.get("imageOverlays") or []:
            identity = str(entry.get("identity", ""))
            position = entry.get("position")
            path = str(entry.get("overlayPath", ""))
            wanted[identity] = {
                "kind": "image",
                "camera": _int(entry.get("camera"), 1),
                "text": path.rsplit("/", 1)[-1],
                "position": position if isinstance(position, str) else json.dumps(position),
                "font_size": None,
                "text_color": "",
                "background_color": "",
                "image_path": path,
                "visible": bool(entry.get("visible", True)),
            }
        for identity, child_id in list(self._overlays.items()):
            if identity not in wanted:
                self.deregister_child("overlay", child_id)
                del self._overlays[identity]
                self._overlay_kinds.pop(identity, None)
        for identity, values in wanted.items():
            child_id = _child_id_for(identity)
            self._overlay_kinds[identity] = values["kind"]
            if identity in self._overlays:
                self.set_child_state_batch("overlay", child_id, values)
            else:
                self.register_child("overlay", child_id, initial_state=values)
                self._overlays[identity] = child_id

    def _overlay_for(self, child_id: str) -> int:
        for identity, cid in self._overlays.items():
            if cid == str(child_id).strip():
                return int(identity)
        raise VapixCommandError("Pick an overlay. Refresh from Device if it was just added.")

    async def _overlay(self, method: str, params: dict[str, Any]) -> Any:
        if not self._dynamic_overlay:
            raise VapixCommandError("This camera has no overlay control.")
        return await self._json(CGI_OVERLAY, method, params)

    # ── Audio ──

    async def _read_audio(self, *, initial: bool = False) -> None:
        if not self._audio:
            return
        try:
            audio = await self._param_list("Audio.A0")
            source = await self._param_list("AudioSource.A0")
        except VapixError as exc:
            if exc.not_authorized:
                raise
            log.info(f"[{self.device_id}] Audio parameters unavailable: {exc}")
            return
        updates: dict[str, Any] = {}
        if "Audio.A0.Enabled" in audio:
            updates["audio_enabled"] = _bool_text(audio["Audio.A0.Enabled"])
        gain = source.get("AudioSource.A0.InputGain")
        if gain is not None:
            updates["audio_input_muted"] = gain.lower() == "mute"
            number = _float(gain)
            if number is not None:
                updates["audio_input_gain"] = number
        out = source.get("AudioSource.A0.OutputGain")
        if out is not None:
            number = _float(out)
            if number is not None:
                updates["audio_output_gain"] = number
        if updates:
            self.set_states(updates)

    # ── Time ──

    async def _read_time(self) -> None:
        if API_TIME not in self._apis:
            return
        data = await self._json(CGI_TIME, "getDateTimeInfo")
        text = str(data.get("dateTime", "")).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            camera_now = datetime.fromisoformat(text)
        except ValueError:
            camera_now = None
        updates: dict[str, Any] = {}
        if camera_now is not None:
            if camera_now.tzinfo is None:
                camera_now = camera_now.replace(tzinfo=timezone.utc)
            updates["clock_offset_s"] = round((camera_now - datetime.now(timezone.utc)).total_seconds(), 1)
        zone = data.get("timeZone") or data.get("posixTimeZone") or ""
        updates["time_zone"] = str(zone)
        self.set_states(updates)

    async def refresh_children(self) -> Any:
        await self._read_views()
        await self._read_ports()
        await self._read_lights()
        await self._read_overlays()
        if self._ptz:
            await self._refresh_presets()
            await self._read_guard_tours()
        return {
            "views": len(self._views),
            "ports": len(self._port_children),
            "lights": len(self._lights),
            "overlays": len(self._overlays),
        }

    # ── Events (VAPIX event WebSocket) ──

    def _events_wanted(self) -> bool:
        return bool(self.config.get("events", True))

    def _start_event_loop(self) -> None:
        if not self._events_wanted():
            self.set_state("events_active", False)
            return
        if self._event_task is None or self._event_task.done():
            self._event_task = asyncio.create_task(self._event_loop())

    async def _stop_event_loop(self) -> None:
        task, self._event_task = self._event_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self.set_state("events_active", False)

    def _ws_url(self, query: str) -> str:
        scheme = "wss" if self._scheme == "https" else "ws"
        return f"{scheme}://{self._host}:{self._port}{WS_EVENTS_PATH}?{query}"

    def _ws_ssl(self) -> Any:
        if self._scheme != "https":
            return None
        import ssl
        ctx = ssl.create_default_context()
        if not self.config.get("verify_ssl", False):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def _open_event_socket(self) -> Any:
        """Open the event WebSocket with a Digest handshake: the first attempt
        collects the camera's challenge, the second answers it. A camera that
        answers the challenge with something other than 401 Digest gets the
        documented session-token alternative (wssession.cgi)."""
        query = "sources=events"
        url = self._ws_url(query)
        uri = f"{WS_EVENTS_PATH}?{query}"
        kwargs: dict[str, Any] = {"open_timeout": EVENT_OPEN_TIMEOUT_S, "max_size": None}
        ssl_ctx = self._ws_ssl()
        if ssl_ctx is not None:
            kwargs["ssl"] = ssl_ctx
        try:
            return await websockets.connect(url, **kwargs)
        except websockets.exceptions.InvalidStatus as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None) or getattr(exc, "status_code", None)
            headers = getattr(response, "headers", None) or getattr(exc, "headers", None) or {}
            challenge = headers.get("WWW-Authenticate", "") if hasattr(headers, "get") else ""
            if status != 401:
                raise
        if challenge.lower().startswith("digest"):
            authorization = _digest_authorization(challenge, "GET", uri, self._username, self._password)
            try:
                return await websockets.connect(
                    url, additional_headers={"Authorization": authorization}, **kwargs,
                )
            except websockets.exceptions.InvalidStatus as exc:
                response = getattr(exc, "response", None)
                status = getattr(response, "status_code", None) or getattr(exc, "status_code", None)
                if status in (401, 403):
                    raise VapixError("The camera refused the login for the event stream", http_status=401)
                raise
        # Session token: a GET on wssession.cgi with the working HTTP login
        # answers a token that opens the socket for the next 15 seconds.
        resp = await self._request("GET", CGI_WSSESSION)
        token = resp.text.strip().strip('"')
        if resp.status_code >= 400 or not token:
            raise VapixError("The camera gave no event session token", http_status=resp.status_code)
        return await websockets.connect(self._ws_url(f"wssession={quote(token, safe='')}&{query}"), **kwargs)

    async def _event_loop(self) -> None:
        backoff = EVENT_RETRY_MIN_S
        while True:
            ws = None
            try:
                ws = await self._open_event_socket()
                await ws.send(json.dumps({
                    "apiVersion": "1.0",
                    "context": "openavc",
                    "method": "events:configure",
                    "params": {"eventFilterList": [{"topicFilter": t} for t in EVENT_TOPIC_FILTERS]},
                }))
                reply = json.loads(await asyncio.wait_for(ws.recv(), EVENT_OPEN_TIMEOUT_S))
                if isinstance(reply, dict) and reply.get("error"):
                    err = reply["error"]
                    raise VapixError(str(err.get("message", "")), code=err.get("code", ""))
                self.set_state("events_active", True)
                self._event_warned = False
                backoff = EVENT_RETRY_MIN_S
                log.info(f"[{self.device_id}] Event stream open")
                async for raw in ws:
                    try:
                        frame = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(frame, dict) and frame.get("method") == "events:notify":
                        try:
                            self._handle_notification((frame.get("params") or {}).get("notification") or {})
                        except Exception:  # noqa: BLE001 - one bad frame must not stop the stream
                            log.debug(f"[{self.device_id}] Could not read an event", exc_info=True)
                raise ConnectionError("Event stream closed by the camera")
            except asyncio.CancelledError:
                raise
            except VapixError as exc:
                self.set_state("events_active", False)
                if exc.not_authorized:
                    log.warning(f"[{self.device_id}] Event stream refused: {exc}")
                    return
                level = log.debug if self._event_warned else log.warning
                level(f"[{self.device_id}] Event stream unavailable ({exc}); retrying in {backoff:.0f}s")
                self._event_warned = True
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, EVENT_RETRY_MAX_S)
            except Exception as exc:  # noqa: BLE001 - keep the stream alive, say so once
                self.set_state("events_active", False)
                level = log.debug if self._event_warned else log.warning
                level(f"[{self.device_id}] Event stream dropped ({exc}); retrying in {backoff:.0f}s")
                self._event_warned = True
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, EVENT_RETRY_MAX_S)
            finally:
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:  # noqa: BLE001
                        pass

    def _handle_notification(self, notification: dict[str, Any]) -> None:
        topic = _topic_name(notification.get("topic", ""))
        message = notification.get("message") or {}
        source = {str(k): str(v) for k, v in (message.get("source") or {}).items()}
        data = {str(k): str(v) for k, v in (message.get("data") or {}).items()}
        stamp = notification.get("timestamp")
        when = ""
        if isinstance(stamp, (int, float)):
            when = datetime.fromtimestamp(stamp / 1000.0, tz=timezone.utc).isoformat(timespec="seconds")
        if topic == "Device/IO/Port":
            port_id = source.get("port", "")
            child_id = self._port_children.get(port_id)
            if child_id and "state" in data:
                self.set_child_state("port", child_id, "active", _bool_text(data["state"]))
        elif topic == "Device/IO/OutputPort":
            port_id = source.get("port", "")
            child_id = self._port_children.get(port_id)
            if child_id and "state" in data:
                self.set_child_state("port", child_id, "active", _bool_text(data["state"]))
        elif topic == "Device/IO/VirtualPort":
            if "state" in data:
                self.set_state("manual_trigger", _bool_text(data["state"]))
        elif topic == "VideoSource/DayNightVision":
            if "day" in data:
                self.set_state("day_mode", _bool_text(data["day"]))
        elif topic == "VideoSource/LiveStreamAccessed":
            if "accessed" in data:
                self.set_state("stream_accessed", _bool_text(data["accessed"]))
        elif topic == "VideoSource/Tampering":
            self.set_states({
                "tamper_count": (_int(self.get_state("tamper_count"), 0) or 0) + 1,
                "tamper_last": when or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
        elif topic == "Device/Status/SystemReady":
            if "ready" in data:
                self.set_state("system_ready", _bool_text(data["ready"]))
        elif topic == "Device/Status/Temperature/Above_or_below":
            if "sensor_level" in data:
                self.set_state("temperature_alarm", _bool_text(data["sensor_level"]))
        elif topic == "Device/HardwareFailure/StorageFailure":
            # Reported per disk (disk_id SD_DISK / NetworkShare, disruption 0|1);
            # an empty card slot counts as disrupted, so this is its own state.
            disk = source.get("disk_id", "") or "storage"
            self._storage_faults[disk] = _bool_text(data.get("disruption", next(iter(data.values()), "")))
            active = sorted(k for k, v in self._storage_faults.items() if v)
            self.set_states({"storage_fault": bool(active), "storage_fault_detail": ", ".join(active)})
        elif topic.startswith("Device/HardwareFailure/"):
            kind = topic.rsplit("/", 1)[-1]
            value = next(iter(data.values()), "")
            self._hardware_faults[kind] = _bool_text(value)
            active = sorted(k for k, v in self._hardware_faults.items() if v)
            self.set_states({"hardware_fault": bool(active), "hardware_fault_reason": ", ".join(active)})
        elif topic == "Device/Casing/Open":
            value = data.get("Open", data.get("open"))
            if value is not None:
                self.set_state("casing_open", _bool_text(value))
        elif topic == "Device/Sensor/PIR":
            if "state" in data:
                self.set_state("pir", _bool_text(data["state"]))
        elif topic == "Device/Tampering/ShockDetected":
            self.set_state("shock_count", (_int(self.get_state("shock_count"), 0) or 0) + 1)
        elif topic == "PTZController/PTZReady":
            # One per view area (source channel); only the controlled one counts.
            if "ready" in data and source.get("channel", str(self._cam)) == str(self._cam):
                self.set_state("ptz_ready", _bool_text(data["ready"]))
        elif topic.startswith("PTZController/Move/"):
            value = next(iter(data.values()), None)
            if value is not None and source.get("channel", str(self._cam)) == str(self._cam):
                self.set_state("ptz_moving", _bool_text(value))
        elif topic.startswith("VideoSource/GlobalSceneChange"):
            if "State" in data or "state" in data:
                self.set_state("scene_change", _bool_text(data.get("State", data.get("state"))))
        elif topic.startswith("PTZController/PTZPresets/"):
            token = data.get("PresetToken", "")
            on_preset = data.get("on_preset", data.get("OnPreset", "1"))
            if token and _bool_text(on_preset):
                name = self._presets.get(token, "")
                self.set_state("preset_last", f"{name} ({token})" if name else token)
        elif topic == "AudioSource/TriggerLevel":
            if "triggered" in data:
                self.set_state("audio_alarm", _bool_text(data["triggered"]))
        elif topic.startswith(("VideoAnalytics/", "CameraApplicationPlatform/", "RuleEngine/")):
            flag = None
            for key in ("active", "motion", "state", "triggered"):
                if key in data:
                    flag = data[key]
                    break
            if flag is None and len(data) == 1:
                flag = next(iter(data.values()))
            if flag is not None and str(flag).strip().lower() in ("0", "1", "true", "false"):
                self.set_states({"motion": _bool_text(flag), "motion_source": topic})
        else:
            log.debug(f"[{self.device_id}] Ignoring event topic {topic!r}")

    # ── Polling ──

    async def poll(self) -> None:
        if self._client is None:
            return
        self._poll_count += 1
        try:
            if self._optics_id:
                await self._read_optics()
            if self._ptz:
                await self._read_ptz()
            if self._port_children or self._poll_count == 1:
                await self._read_ports()
            if self._poll_count % SLOW_POLL_EVERY == 1:
                await self._read_image()
                await self._read_daynight()
                await self._read_overlays()
                await self._read_lights()
                await self._read_audio()
                if self._ptz:
                    await self._refresh_presets()
                    await self._read_guard_tours()
        except VapixError as exc:
            if exc.not_authorized:
                raise ConnectionFaultError(
                    "The camera stopped accepting the login.", code="auth_failed",
                ) from exc
            self.set_state("last_error", str(exc))
        except httpx.TransportError as exc:
            raise ConnectionError(f"{self._host} is not responding: {exc}") from exc

    # ── Commands ──

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        params = params or {}
        handler = self._DISPATCH.get(command)
        if handler is None:
            raise VapixCommandError(f"Unknown command: {command}")
        try:
            return await handler(self, params)
        except VapixError as exc:
            if exc.not_authorized:
                raise ConnectionFaultError(
                    "The camera stopped accepting the login.", code="auth_failed",
                ) from exc
            raise VapixCommandError(f"The camera refused {command}: {exc}") from exc
        except httpx.TransportError as exc:
            raise ConnectionError(f"{self._host} is not responding: {exc}") from exc

    # Optics

    async def _cmd_zoom_set(self, params: dict[str, Any]) -> None:
        if "zoom" not in self._optics_caps:
            raise VapixCommandError("This camera has no remote zoom.")
        magnification = _float(params.get("magnification"))
        if magnification is None:
            raise VapixCommandError("Give a magnification.")
        maximum = _float(self.get_state("max_magnification"))
        if maximum is not None and not (1.0 <= magnification <= maximum):
            raise VapixCommandError(f"Magnification is 1 to {maximum:g} on this camera.")
        await self._optics("setMagnification", {"magnification": magnification})
        await self._read_optics()

    async def _relative(self, method: str, params: dict[str, Any], sign: int, default_step: str) -> None:
        amount = _float(params.get("amount"))
        if amount is not None and amount > 0:
            fields = {"type": "numerical", "value": sign * amount}
        else:
            step = OPTICS_STEP_TYPES.get(str(params.get("step") or default_step).lower(), OPTICS_STEP_TYPES[default_step])
            fields = {"type": ("+" if sign > 0 else "-") + step}
        await self._optics(method, fields)
        await self._read_optics()

    async def _cmd_zoom_in(self, params: dict[str, Any]) -> None:
        if "zoom" not in self._optics_caps:
            raise VapixCommandError("This camera has no remote zoom.")
        await self._relative("setRelativeMagnification", params, +1, "big")

    async def _cmd_zoom_out(self, params: dict[str, Any]) -> None:
        if "zoom" not in self._optics_caps:
            raise VapixCommandError("This camera has no remote zoom.")
        await self._relative("setRelativeMagnification", params, -1, "big")

    async def _cmd_focus_set(self, params: dict[str, Any]) -> None:
        if "focus" not in self._optics_caps:
            raise VapixCommandError("This camera has no remote focus.")
        position = _float(params.get("position"))
        if position is None or not (0.0 <= position <= 1.0):
            raise VapixCommandError("Focus position is 0 to 1.")
        await self._optics("setFocus", {"position": position})
        await self._read_optics()

    async def _cmd_focus_near(self, params: dict[str, Any]) -> None:
        if "focus" not in self._optics_caps:
            raise VapixCommandError("This camera has no remote focus.")
        await self._relative("setRelativeFocus", params, -1, "small")

    async def _cmd_focus_far(self, params: dict[str, Any]) -> None:
        if "focus" not in self._optics_caps:
            raise VapixCommandError("This camera has no remote focus.")
        await self._relative("setRelativeFocus", params, +1, "small")

    async def _cmd_autofocus(self, params: dict[str, Any]) -> None:
        if "focus" not in self._optics_caps:
            raise VapixCommandError("This camera has no remote focus.")
        await self._optics("performAutofocus")
        await self._read_optics()

    async def _cmd_focus_window(self, params: dict[str, Any]) -> None:
        if "focus" not in self._optics_caps:
            raise VapixCommandError("This camera has no remote focus.")
        fields = {}
        for key, name in (("x", "upperLeftX"), ("y", "upperLeftY"), ("width", "width"), ("height", "height")):
            value = _float(params.get(key))
            if value is None or not (0.0 <= value <= 1.0):
                raise VapixCommandError("Focus window values are fractions from 0 to 1.")
            fields[name] = value
        await self._optics("setFocusWindow", fields)

    async def _cmd_optics_reset(self, params: dict[str, Any]) -> None:
        zoom = params.get("zoom", True)
        focus = params.get("focus", True)
        await self._optics("reset", {
            "zoom": zoom if isinstance(zoom, bool) else _bool_text(zoom),
            "focus": focus if isinstance(focus, bool) else _bool_text(focus),
        })
        await self._read_optics()

    async def _cmd_optics_calibrate(self, params: dict[str, Any]) -> None:
        if not ({"calibrateZoom", "calibrateFocus"} & self._optics_caps):
            raise VapixCommandError("This camera's lens has nothing to calibrate.")
        await self._optics("calibrate", {
            "zoom": "calibrateZoom" in self._optics_caps,
            "focus": "calibrateFocus" in self._optics_caps,
        })
        await self._read_optics()

    async def _cmd_ir_cut_auto(self, params: dict[str, Any]) -> None:
        await self._set_ir_cut("auto")

    async def _cmd_ir_cut_on(self, params: dict[str, Any]) -> None:
        await self._set_ir_cut("on")

    async def _cmd_ir_cut_off(self, params: dict[str, Any]) -> None:
        await self._set_ir_cut("off")

    # PTZ

    @staticmethod
    def _speed(params: dict[str, Any], default: int = 50) -> int:
        return max(1, min(100, _int(params.get("speed"), default) or default))

    async def _cmd_pt_up(self, params: dict[str, Any]) -> None:
        await self._ptz_get({"continuouspantiltmove": f"0,{self._speed(params)}"})

    async def _cmd_pt_down(self, params: dict[str, Any]) -> None:
        await self._ptz_get({"continuouspantiltmove": f"0,-{self._speed(params)}"})

    async def _cmd_pt_left(self, params: dict[str, Any]) -> None:
        await self._ptz_get({"continuouspantiltmove": f"-{self._speed(params)},0"})

    async def _cmd_pt_right(self, params: dict[str, Any]) -> None:
        await self._ptz_get({"continuouspantiltmove": f"{self._speed(params)},0"})

    async def _cmd_pt_drive(self, params: dict[str, Any]) -> None:
        pan = max(-100, min(100, _int(params.get("pan"), 0) or 0))
        tilt = max(-100, min(100, _int(params.get("tilt"), 0) or 0))
        await self._ptz_get({"continuouspantiltmove": f"{pan},{tilt}"})

    async def _cmd_pt_stop(self, params: dict[str, Any]) -> None:
        await self._ptz_get({"continuouspantiltmove": "0,0"})
        await self._ptz_get({"continuouszoommove": 0})
        if self._ptz_support.get(f"PTZ.Support.S{self._cam}.ContinuousFocus", "").lower() == "true":
            await self._ptz_get({"continuousfocusmove": 0})
        await self._read_ptz()

    async def _cmd_pt_home(self, params: dict[str, Any]) -> None:
        await self._ptz_get({"move": "home"})

    async def _cmd_pt_absolute(self, params: dict[str, Any]) -> None:
        pan = _float(params.get("pan"))
        tilt = _float(params.get("tilt"))
        if pan is None or tilt is None:
            raise VapixCommandError("Give a pan and a tilt in degrees.")
        query: dict[str, Any] = {"pan": f"{pan:g}", "tilt": f"{tilt:g}"}
        if params.get("speed") is not None:
            query["speed"] = self._speed(params)
        await self._ptz_get(query)
        await self._read_ptz()

    async def _cmd_pt_relative(self, params: dict[str, Any]) -> None:
        pan = _float(params.get("pan"), 0.0) or 0.0
        tilt = _float(params.get("tilt"), 0.0) or 0.0
        query: dict[str, Any] = {"rpan": f"{pan:g}", "rtilt": f"{tilt:g}"}
        if params.get("speed") is not None:
            query["speed"] = self._speed(params)
        await self._ptz_get(query)
        await self._read_ptz()

    async def _cmd_ptz_zoom_in(self, params: dict[str, Any]) -> None:
        await self._ptz_get({"continuouszoommove": self._speed(params)})

    async def _cmd_ptz_zoom_out(self, params: dict[str, Any]) -> None:
        await self._ptz_get({"continuouszoommove": -self._speed(params)})

    async def _cmd_ptz_zoom_stop(self, params: dict[str, Any]) -> None:
        await self._ptz_get({"continuouszoommove": 0})
        await self._read_ptz()

    async def _cmd_ptz_zoom_absolute(self, params: dict[str, Any]) -> None:
        zoom = _int(params.get("zoom"))
        if zoom is None:
            raise VapixCommandError("Give a zoom position.")
        await self._ptz_get({"zoom": max(1, min(19999, zoom))})
        await self._read_ptz()

    async def _cmd_ptz_zoom_relative(self, params: dict[str, Any]) -> None:
        zoom = _int(params.get("zoom"))
        if zoom is None:
            raise VapixCommandError("Give a zoom step.")
        await self._ptz_get({"rzoom": max(-19999, min(19999, zoom))})
        await self._read_ptz()

    async def _cmd_ptz_focus_near(self, params: dict[str, Any]) -> None:
        await self._ptz_get({"continuousfocusmove": -self._speed(params)})

    async def _cmd_ptz_focus_far(self, params: dict[str, Any]) -> None:
        await self._ptz_get({"continuousfocusmove": self._speed(params)})

    async def _cmd_ptz_focus_stop(self, params: dict[str, Any]) -> None:
        await self._ptz_get({"continuousfocusmove": 0})

    async def _cmd_ptz_autofocus_on(self, params: dict[str, Any]) -> None:
        await self._ptz_get({"autofocus": "on"})
        await self._read_ptz()

    async def _cmd_ptz_autofocus_off(self, params: dict[str, Any]) -> None:
        await self._ptz_get({"autofocus": "off"})
        await self._read_ptz()

    async def _cmd_center(self, params: dict[str, Any]) -> None:
        x, y = _int(params.get("x")), _int(params.get("y"))
        if x is None or y is None:
            raise VapixCommandError("Give the x and y of the point.")
        query: dict[str, Any] = {"center": f"{x},{y}"}
        if params.get("width") and params.get("height"):
            query["imagewidth"] = _int(params["width"])
            query["imageheight"] = _int(params["height"])
        await self._ptz_get(query)
        await self._read_ptz()

    async def _cmd_area_zoom(self, params: dict[str, Any]) -> None:
        x, y, z = _int(params.get("x")), _int(params.get("y")), _int(params.get("zoom"))
        if x is None or y is None or z is None:
            raise VapixCommandError("Give x, y and a zoom factor.")
        query: dict[str, Any] = {"areazoom": f"{x},{y},{max(1, z)}"}
        if params.get("width") and params.get("height"):
            query["imagewidth"] = _int(params["width"])
            query["imageheight"] = _int(params["height"])
        await self._ptz_get(query)
        await self._read_ptz()

    def _preset_number(self, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise VapixCommandError("Pick a preset.")
        if text in self._presets:
            return text
        for number, name in self._presets.items():
            if name == text or f"{name} ({number})" == text:
                return number
        return text

    async def _cmd_preset_recall(self, params: dict[str, Any]) -> None:
        number = self._preset_number(params.get("preset"))
        if number.isdigit():
            await self._ptz_get({"gotoserverpresetno": number})
        else:
            await self._ptz_get({"gotoserverpresetname": number})
        self.set_state("preset_last", f"{self._presets.get(number, '')} ({number})".strip())

    async def _cmd_preset_save(self, params: dict[str, Any]) -> None:
        name = str(params.get("name") or "").strip()
        if not name:
            raise VapixCommandError("Give the preset a name.")
        if not self._ptz:
            raise VapixCommandError("This camera has no pan/tilt/zoom control.")
        await self._text_get(CGI_PTZ_CONFIG, {"camera": self._cam, "setserverpresetname": name})
        await self._refresh_presets()

    async def _cmd_preset_delete(self, params: dict[str, Any]) -> None:
        number = self._preset_number(params.get("preset"))
        if not self._ptz:
            raise VapixCommandError("This camera has no pan/tilt/zoom control.")
        if number.isdigit():
            await self._text_get(CGI_PTZ_CONFIG, {"camera": self._cam, "removeserverpresetno": number})
        else:
            await self._text_get(CGI_PTZ_CONFIG, {"camera": self._cam, "removeserverpresetname": number})
        await self._refresh_presets()

    async def _cmd_set_home(self, params: dict[str, Any]) -> None:
        if not self._ptz:
            raise VapixCommandError("This camera has no pan/tilt/zoom control.")
        await self._text_get(CGI_PTZ_CONFIG, {"camera": self._cam, "setserverpresetname": "Home", "home": "yes"})
        await self._refresh_presets()

    def _tour_group(self, value: Any) -> str:
        text = str(value or "").strip()
        if text in self._tours:
            return text
        for group, tour in self._tours.items():
            if tour.get("Name") == text:
                return group
        raise VapixCommandError("Pick a guard tour. Refresh from Device to reload the list.")

    async def _cmd_guard_tour_start(self, params: dict[str, Any]) -> None:
        group = self._tour_group(params.get("tour"))
        await self._param_update(**{f"GuardTour.{group}.Running": "yes"})
        await self._read_guard_tours()

    async def _cmd_guard_tour_stop(self, params: dict[str, Any]) -> None:
        if str(params.get("tour") or "").strip():
            groups = [self._tour_group(params.get("tour"))]
        else:
            groups = [g for g, t in self._tours.items() if t.get("Running", "").lower() == "yes"]
        for group in groups:
            await self._param_update(**{f"GuardTour.{group}.Running": "no"})
        await self._read_guard_tours()

    async def _cmd_aux_command(self, params: dict[str, Any]) -> None:
        function = str(params.get("function") or "").strip()
        if not function:
            raise VapixCommandError("Give the auxiliary function's name.")
        await self._ptz_get({"auxiliary": function})

    # I/O

    async def _cmd_port_on(self, params: dict[str, Any]) -> None:
        await self._set_port(str(params.get("port") or ""), True)

    async def _cmd_port_off(self, params: dict[str, Any]) -> None:
        await self._set_port(str(params.get("port") or ""), False)

    async def _cmd_port_pulse(self, params: dict[str, Any]) -> None:
        await self._pulse_port(str(params.get("port") or ""), _int(params.get("duration"), 500) or 500)

    async def _cmd_virtual_input_on(self, params: dict[str, Any]) -> None:
        number = _int(params.get("input"))
        if number is None or number < 1:
            raise VapixCommandError("Give a virtual input number.")
        await self._text_get(CGI_VIRTUAL_INPUT, {"action": f"{number}:/"})

    async def _cmd_virtual_input_off(self, params: dict[str, Any]) -> None:
        number = _int(params.get("input"))
        if number is None or number < 1:
            raise VapixCommandError("Give a virtual input number.")
        await self._text_get(CGI_VIRTUAL_INPUT, {"action": f"{number}:\\"})

    # Illuminators

    async def _cmd_light_on(self, params: dict[str, Any]) -> None:
        light_id = self._light_for(str(params.get("light") or ""))
        await self._json(CGI_LIGHT, "activateLight", {"lightID": light_id})
        await self._read_lights()

    async def _cmd_light_off(self, params: dict[str, Any]) -> None:
        light_id = self._light_for(str(params.get("light") or ""))
        await self._json(CGI_LIGHT, "deactivateLight", {"lightID": light_id})
        await self._read_lights()

    async def _cmd_light_intensity(self, params: dict[str, Any]) -> None:
        light_id = self._light_for(str(params.get("light") or ""))
        intensity = _int(params.get("intensity"))
        if intensity is None:
            raise VapixCommandError("Give an intensity.")
        low, high = self._light_ranges.get(light_id, (0, 100))
        if not (low <= intensity <= high):
            raise VapixCommandError(f"Intensity is {low} to {high} on this illuminator.")
        await self._json(CGI_LIGHT, "setManualIntensity", {"lightID": light_id, "intensity": intensity})
        await self._read_lights()

    async def _cmd_light_auto_intensity(self, params: dict[str, Any]) -> None:
        light_id = self._light_for(str(params.get("light") or ""))
        enabled = params.get("enabled", True)
        enabled = enabled if isinstance(enabled, bool) else _bool_text(enabled)
        await self._json(CGI_LIGHT, "setAutomaticIntensityMode", {"lightID": light_id, "enabled": enabled})
        await self._read_lights()

    # Overlays

    async def _cmd_overlay_add_text(self, params: dict[str, Any]) -> int:
        text = str(params.get("text") or "")
        if not text.strip():
            raise VapixCommandError("Give the overlay some text.")
        fields: dict[str, Any] = {"camera": self._cam, "text": text[:512]}
        position = str(params.get("position") or "topLeft")
        if position in OVERLAY_POSITIONS:
            fields["position"] = position
        size = _int(params.get("font_size"))
        if size is not None and size > 0:
            fields["fontSize"] = size
        color = str(params.get("text_color") or "")
        if color in OVERLAY_COLORS:
            fields["textColor"] = color
        background = str(params.get("background_color") or "")
        if background in OVERLAY_COLORS:
            fields["textBGColor"] = background
        data = await self._overlay("addText", fields)
        await self._read_overlays()
        return _int(data.get("identity"), -1)

    async def _cmd_overlay_set_text(self, params: dict[str, Any]) -> None:
        identity = self._overlay_for(str(params.get("overlay") or ""))
        if self._overlay_kinds.get(str(identity)) != "text":
            raise VapixCommandError("That overlay is an image; only a text overlay has text.")
        await self._overlay("setText", {"identity": identity, "text": str(params.get("text") or "")[:512]})
        await self._read_overlays()

    async def _cmd_overlay_set_position(self, params: dict[str, Any]) -> None:
        identity = self._overlay_for(str(params.get("overlay") or ""))
        position = str(params.get("position") or "")
        if position not in OVERLAY_POSITIONS:
            raise VapixCommandError("Pick a position.")
        method = "setImage" if self._overlay_kinds.get(str(identity)) == "image" else "setText"
        await self._overlay(method, {"identity": identity, "position": position})
        await self._read_overlays()

    async def _cmd_overlay_add_image(self, params: dict[str, Any]) -> int:
        image = str(params.get("image") or "").strip()
        if not image:
            raise VapixCommandError("Pick an image.")
        fields: dict[str, Any] = {"camera": self._cam, "overlayPath": image}
        position = str(params.get("position") or "")
        if position in OVERLAY_POSITIONS:
            fields["position"] = position
        data = await self._overlay("addImage", fields)
        await self._read_overlays()
        return _int(data.get("identity"), -1)

    async def _cmd_overlay_remove(self, params: dict[str, Any]) -> None:
        identity = self._overlay_for(str(params.get("overlay") or ""))
        await self._overlay("remove", {"identity": identity})
        await self._read_overlays()

    # System

    async def _cmd_reboot(self, params: dict[str, Any]) -> None:
        if API_FIRMWARE in self._apis:
            await self._json(CGI_FIRMWARE, "reboot")
        else:
            await self._text_get(CGI_RESTART, {})

    _DISPATCH: dict[str, Any] = {
        "zoom_set": _cmd_zoom_set,
        "zoom_in": _cmd_zoom_in,
        "zoom_out": _cmd_zoom_out,
        "focus_set": _cmd_focus_set,
        "focus_near": _cmd_focus_near,
        "focus_far": _cmd_focus_far,
        "autofocus": _cmd_autofocus,
        "focus_window": _cmd_focus_window,
        "optics_reset": _cmd_optics_reset,
        "optics_calibrate": _cmd_optics_calibrate,
        "ir_cut_auto": _cmd_ir_cut_auto,
        "ir_cut_on": _cmd_ir_cut_on,
        "ir_cut_off": _cmd_ir_cut_off,
        "pt_up": _cmd_pt_up,
        "pt_down": _cmd_pt_down,
        "pt_left": _cmd_pt_left,
        "pt_right": _cmd_pt_right,
        "pt_drive": _cmd_pt_drive,
        "pt_stop": _cmd_pt_stop,
        "pt_home": _cmd_pt_home,
        "pt_absolute": _cmd_pt_absolute,
        "pt_relative": _cmd_pt_relative,
        "ptz_zoom_in": _cmd_ptz_zoom_in,
        "ptz_zoom_out": _cmd_ptz_zoom_out,
        "ptz_zoom_stop": _cmd_ptz_zoom_stop,
        "ptz_zoom_absolute": _cmd_ptz_zoom_absolute,
        "ptz_zoom_relative": _cmd_ptz_zoom_relative,
        "ptz_focus_near": _cmd_ptz_focus_near,
        "ptz_focus_far": _cmd_ptz_focus_far,
        "ptz_focus_stop": _cmd_ptz_focus_stop,
        "ptz_autofocus_on": _cmd_ptz_autofocus_on,
        "ptz_autofocus_off": _cmd_ptz_autofocus_off,
        "center": _cmd_center,
        "area_zoom": _cmd_area_zoom,
        "preset_recall": _cmd_preset_recall,
        "preset_save": _cmd_preset_save,
        "preset_delete": _cmd_preset_delete,
        "set_home": _cmd_set_home,
        "guard_tour_start": _cmd_guard_tour_start,
        "guard_tour_stop": _cmd_guard_tour_stop,
        "aux_command": _cmd_aux_command,
        "port_on": _cmd_port_on,
        "port_off": _cmd_port_off,
        "port_pulse": _cmd_port_pulse,
        "virtual_input_on": _cmd_virtual_input_on,
        "virtual_input_off": _cmd_virtual_input_off,
        "light_on": _cmd_light_on,
        "light_off": _cmd_light_off,
        "light_intensity": _cmd_light_intensity,
        "light_auto_intensity": _cmd_light_auto_intensity,
        "overlay_add_text": _cmd_overlay_add_text,
        "overlay_set_text": _cmd_overlay_set_text,
        "overlay_set_position": _cmd_overlay_set_position,
        "overlay_add_image": _cmd_overlay_add_image,
        "overlay_remove": _cmd_overlay_remove,
        "reboot": _cmd_reboot,
    }

    # ── Device settings ──

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        try:
            await self._write_setting(key, value)
        except VapixError as exc:
            if exc.not_authorized:
                raise ConnectionFaultError(
                    "The camera stopped accepting the login.", code="auth_failed",
                ) from exc
            raise DeviceSettingValueError(f"The camera refused {key}: {exc}") from exc
        except VapixCommandError as exc:
            raise DeviceSettingValueError(str(exc)) from exc
        except httpx.TransportError as exc:
            raise ConnectionError(f"{self._host} is not responding: {exc}") from exc

    async def _write_setting(self, key: str, value: Any) -> None:
        if key == "digital_ptz":
            if not self._ptz_available:
                raise DeviceSettingValueError("This camera has no pan, tilt or zoom to turn on")
            if not self._ptz_digital:
                raise DeviceSettingValueError("This camera's PTZ is mechanical and always on")
            on = value if isinstance(value, bool) else _bool_text(value)
            await self._param_update(**{f"PTZ.ImageSource.I{self._src}.PTZEnabled": "true" if on else "false"})
            if on:
                # A locked view area does not move; unlock it with the enable.
                try:
                    await self._param_update(**{f"PTZ.Various.V{self._cam}.Locked": "false"})
                except VapixError as exc:
                    if exc.not_authorized:
                        raise
            await self._read_ptz(initial=True)
            return
        if key == "ir_cut_filter":
            await self._set_ir_cut(str(value))
            return
        if key in SENSOR_SETTINGS:
            name, kind = SENSOR_SETTINGS[key]
            if self.get_state(key) is None:
                raise DeviceSettingValueError(f"This camera does not report {key}, so it cannot be set")
            if kind == "int":
                number = _int(value)
                if number is None or not (0 <= number <= 100):
                    raise DeviceSettingValueError(f"{key} is 0 to 100")
                wire = str(number)
            elif kind == "onoff":
                wire = _onoff(value)
            elif kind == "yesno":
                wire = _yesno(value)
            else:
                wire = str(value).strip()
                if not wire:
                    raise DeviceSettingValueError(f"{key} needs a value")
            await self._param_update(**{f"ImageSource.I{self._src}.Sensor.{name}": wire})
            await self._read_image()
            return
        if key == "rotation":
            angle = _int(value)
            if angle not in (0, 90, 180, 270):
                raise DeviceSettingValueError("Rotation is 0, 90, 180 or 270")
            param = (
                f"ImageSource.I{self._src}.Rotation" if self._source_rotation
                else f"Image.I{self._src}.Appearance.Rotation"
            )
            await self._param_update(**{param: str(angle)})
            await self._read_image()
            return
        if key == "mirror":
            await self._param_update(**{f"Image.I{self._src}.Appearance.MirrorEnabled": _yesno(value)})
            await self._read_image()
            return
        if key == "overlays_shown":
            if self.get_state("overlays_shown") is None:
                raise DeviceSettingValueError("This camera does not report an overlay visibility setting, so it cannot be set")
            wire = str(value).strip()
            if wire not in ("all", "text", "image", "application", "off", "all-sync", "application-sync"):
                raise DeviceSettingValueError("Overlays shown is all, text, image, application or off")
            await self._param_update(**{f"Image.I{self._src}.Appearance.Overlays": wire})
            await self._read_image()
            return
        if key in DAYNIGHT_SETTINGS:
            if API_DAYNIGHT not in self._apis:
                if key != "day_night_shift_level":
                    raise DeviceSettingValueError(
                        "This camera's software has no day/night configuration API; only the day to night level can be set"
                    )
                number = _int(value)
                if number is None or not (0 <= number <= 100):
                    raise DeviceSettingValueError(f"{key} is 0 to 100")
                await self._param_update(**{f"ImageSource.I{self._src}.DayNight.ShiftLevel": str(number)})
                await self._read_ir_cut_param(read_filter=False)
                return
            name = DAYNIGHT_SETTINGS[key]
            if key == "day_night_autotune" and not self._daynight_caps.get("autotune", True):
                raise DeviceSettingValueError("This camera does not support night to day autotune")
            if key == "night_day_shift_level":
                if not self._daynight_caps.get("night_day_level", True):
                    raise DeviceSettingValueError("This camera has no night to day level")
                if self._daynight_config.get("Autotune"):
                    raise DeviceSettingValueError("Turn Night to Day Autotune off before setting the level")
            if key == "night_filter" and not self._daynight_caps.get("irpass", True):
                raise DeviceSettingValueError("This camera has no IR pass filter")
            if key == "day_night_autotune":
                wire: Any = value if isinstance(value, bool) else _bool_text(value)
            elif key == "night_filter":
                wire = str(value).strip()
                if wire not in ("clear", "irpass"):
                    raise DeviceSettingValueError("Night filter is clear or irpass")
            elif key.endswith("_level"):
                number = _int(value)
                if number is None or not (0 <= number <= 100):
                    raise DeviceSettingValueError(f"{key} is 0 to 100")
                wire = number
            else:
                number = _float(value)
                if number is None or not (1 <= number <= 600):
                    raise DeviceSettingValueError(f"{key} is 1 to 600 seconds")
                wire = number
            await self._json(CGI_DAYNIGHT, "setConfiguration", {"channel": self._src, name: wire}, api_version="1.2")
            await self._read_daynight()
            return
        if key in ("audio_enabled", "audio_input_gain", "audio_output_gain"):
            if not self._audio:
                raise DeviceSettingValueError("This camera has no audio")
            if key == "audio_enabled":
                await self._param_update(**{"Audio.A0.Enabled": _yesno(value)})
            else:
                number = _float(value)
                if number is None:
                    raise DeviceSettingValueError(f"{key} needs a number in dB")
                name = "InputGain" if key == "audio_input_gain" else "OutputGain"
                await self._param_update(**{f"AudioSource.A0.{name}": f"{number:g}"})
            await self._read_audio()
            return
        raise ValueError(f"Unknown device setting: {key}")
