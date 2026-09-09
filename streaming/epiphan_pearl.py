"""
OpenAVC Epiphan Pearl Driver.

Controls Epiphan Pearl video production and lecture-capture systems (Pearl
Nano, Pearl Mini, Pearl Nexus, Pearl-2) through the Pearl device REST API
v2.0: recording per recorder and for the whole unit, streaming per publisher
and per channel, layout switching, bookmarks, input capture settings (gain,
mute, phantom power, delays, SRT / RTSP / NDI parameters), the HDMI output
source, USB and network storage, automatic file upload progress, the
one-touch control, configuration presets, CMS events (Kaltura, Panopto,
Opencast: start, stop, pause, resume, extend, ad-hoc events), the network
speed test, reboot and shutdown.

Why Python
----------
The REST API is a family of JSON resources whose rosters are the device's
own: channels and their publishers, recorders, inputs, outputs, storages and
one-touch controls are enumerated at connect and registered as child
entities, and every input carries a different settings block (an XLR pair
has gain and phantom power, an HDMI port has deinterlacing and an audio
mute, an SRT input has latency and a port), so the inputs are a dynamic
child type whose schema is built from what each input reports. Writes are
JSON PATCH bodies assembled from a dotted path, and a rejected login is
distinguished from a transport failure. None of that fits the declarative
``.avcdriver`` request/response model, so this driver owns an ``httpx``
session.

Push vs poll
------------
Poll only. The REST API describes no subscription, event stream, webhook or
multicast channel, so state is read on ``poll_interval`` (default 5 s):
channels with their publishers' status, every recorder's status, the system
status, the one-touch state and the file-upload queue on every poll; inputs
and their settings, outputs, storages, presets, the CMS events, the
connectivity report and each recorder's archive on a slower cadence
(``detail_poll_every`` polls). Start and stop operations are asynchronous on
the device ("the method does not wait"), so a command returns as soon as the
Pearl accepts it and the next poll shows ``starting`` and then ``started``.

Authentication
--------------
HTTP Basic with a Pearl user account (``admin`` by default). Pearl firmware
4.14.2 and later require a password on every account; a rejected login is a
typed ``auth_failed`` fault so the platform waits for new credentials instead
of retrying into a lockout. The API is served on the web port (80, or 443
when HTTPS is enabled on the Pearl).

Previews
--------
Every channel is available as an RTSP stream at ``rtsp://<host>:<port>/
stream.sdp``, where the port is per channel (554 upward; the Pearl Nano's
single channel is always 554). The REST API does not report the port, so it
is entered per channel under the device's ``channel_rtsp_ports`` setting;
until it is, the channel's preview says so in the Video Panel picker. Still
images of every channel, input and output are published as ``snapshot_url``.

Sources (all public, from Epiphan Video):
  Pearl device REST API v2.0 (OpenAPI, MIT)
      https://epiphan-video.github.io/pearl_api_swagger_ui/
      https://raw.githubusercontent.com/epiphan-video/pearl_api_swagger_ui/main/api/v2.0/openapi.yml
  Network ports used by Pearl-2 (RTSP port per channel, mDNS, HTTP/HTTPS)
      https://www.epiphan.com/userguides/pearl-2/Content/setup/networkPortsUsed.htm
  Share a live broadcast stream (the RTSP URL form)
      https://www.epiphan.com/userguides/pearl-nano/Content/stream/streamHTTPorRTSP.htm
  Pearl System API Guide release history (password requirement since 4.14.2)
      https://www.epiphan.com/userguides/pearl-api/Content/startHere/releaseNotes/whatsNew-featuresonly.htm
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

import httpx

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.utils.logger import get_logger

log = get_logger(__name__)

API_PREFIX = "/api/v2.0"

# The RTSP port of the Pearl Nano's only channel (network ports guide:
# "Pearl Nano: 554"). Other models assign 554 upward per channel and the
# REST API does not say which channel took which port.
NANO_RTSP_PORT = 554

# The three keyword sources an HDMI output accepts beside a channel or an
# input identifier (outputSource parameter).
OUTPUT_KEYWORD_SOURCES = (
    ("multiview", "Multiview"),
    ("deviceinfo", "Device Info"),
    ("console", "Local Console"),
)

PUBLISHER_TYPES = ("rtsp", "rtmp", "rtp-udp", "mpegts-udp", "mpegts-rtp", "ndi", "hls", "srt")
PUBLISHER_STATES = ("starting", "listening", "started", "stopped", "error")
RECORDER_STATES = ("disabled", "starting", "started", "stopped", "error")
INPUT_TYPES = ("embedded", "usb", "rtsp", "ndi", "srt", "web-graphics")
STORAGE_STATES = ("ready", "nodev", "dev", "devro", "formatting")
TRANSFER_STATES = ("completed", "disabled", "nomedia", "running")
AFU_STATES = ("idle", "paused", "uploading", "error", "disabled")
AFU_PROTOCOLS = ("ftp", "rsync", "cifs", "scp", "sftp", "s3", "webdav", "usbcopy",
                 "kaltura", "panopto", "opencast")
EVENT_STATUSES = ("scheduled", "running", "paused", "finished")
CONNECTIVITY_STATES = ("disabled", "ok", "error")
CONNECTIVITY_KEYS = ("dns", "http", "https", "captive_portal", "icmp", "epiphan_edge", "vtun")

# Publisher types whose destination is a single ``url`` field.
URL_PUBLISHER_TYPES = ("rtmp", "rtsp", "hls")

# Settings keys that carry a secret. Never published as state, never offered
# as a control; they are set in the Pearl Admin panel.
SECRET_SETTING_KEYS = ("password", "passphrase")

_CHILD_ID_RE = re.compile(r"[^A-Za-z0-9_-]")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])([A-Z])")


def _sanitize_id(raw: str) -> str:
    """A device identifier as a child local id: ``[A-Za-z0-9_-]`` only."""
    return _CHILD_ID_RE.sub("_", str(raw))


def _snake(segment: str) -> str:
    return _CAMEL_RE.sub(r"_\1", segment).replace("-", "_").lower()


def _int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool_text(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _with_credentials(url: str, username: str, password: str) -> str:
    if not username:
        return url
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    cred = quote(username, safe="")
    if password:
        cred += ":" + quote(password, safe="")
    return f"{scheme}://{cred}@{rest}"


# ── Input settings: what each documented settings field is ──
#
# path -> (state property, var-def). The path is the dotted position in the
# input's settings document; the var-def follows the input settings schemas
# (AudioInputSettings, VideoInputSettings, HdmiInputSettings, SdiInputSettings,
# the SRT / RTSP / NDI / web graphics / USB / local audio blocks). A settings
# field the table does not name is still published, typed from its value.
INPUT_SETTING_DEFS: dict[str, tuple[str, dict[str, Any]]] = {
    "audio.delay": ("audio_delay_ms", {"type": "integer", "label": "Audio Delay (ms)", "min": -300, "max": 300, "unit": "ms", "control": True}),
    "video.nosignal.image": ("nosignal_image", {"type": "string", "label": "No-Signal Image"}),
    "video.nosignal.timeout": ("nosignal_timeout_s", {"type": "integer", "label": "No-Signal Timeout (s)", "min": 0, "unit": "s"}),
    "video.force_full_color_range": ("force_full_color_range", {"type": "boolean", "label": "Force Full Color Range"}),
    "video.hwaccel_decoding": ("hwaccel_decoding", {"type": "boolean", "label": "Hardware Decoding"}),
    "hdmi.deinterlacing": ("deinterlacing", {"type": "boolean", "label": "Deinterlacing", "control": True}),
    "hdmi.downscale_4k_to_fullhd": ("downscale_4k_to_fullhd", {"type": "boolean", "label": "Downscale 4K to 1080p"}),
    "hdmi.scaling": ("scaling", {"type": "string", "label": "Scaling"}),
    "hdmi.audio.mute": ("audio_mute", {"type": "boolean", "label": "Audio Mute", "control": True}),
    "hdmi.audio.delay": ("hdmi_audio_delay_ms", {"type": "integer", "label": "HDMI Audio Delay (ms)", "min": -300, "max": 300, "unit": "ms", "control": True}),
    "sdi.downscale_4k_to_fullhd": ("downscale_4k_to_fullhd", {"type": "boolean", "label": "Downscale 4K to 1080p"}),
    "sdi.scaling": ("scaling", {"type": "string", "label": "Scaling"}),
    "sdi.audio.mute": ("audio_mute", {"type": "boolean", "label": "Audio Mute", "control": True}),
    "sdi.audio.delay": ("sdi_audio_delay_ms", {"type": "integer", "label": "SDI Audio Delay (ms)", "min": -300, "max": 300, "unit": "ms", "control": True}),
    "local_audio.gain": ("gain", {"type": "integer", "label": "Gain", "min": 0, "control": True}),
    "local_audio.mute": ("mute", {"type": "boolean", "label": "Mute", "control": True}),
    "local_audio.phantom_power": ("phantom_power", {"type": "boolean", "label": "Phantom Power (48 V)", "control": True}),
    "local_audio.input_type": ("input_type", {"type": "enum", "label": "Input Type", "values": ["XLR", "RCA", "XLR+RCA", "3.5mm", "RCA+3.5mm"], "control": True}),
    "local_audio.stereo_pair": ("stereo_pair", {"type": "boolean", "label": "Stereo Pair"}),
    "local_audio.channels.channelA.gain": ("channel_a_gain", {"type": "integer", "label": "Channel A Gain", "min": 0, "control": True}),
    "local_audio.channels.channelA.mute": ("channel_a_mute", {"type": "boolean", "label": "Channel A Mute", "control": True}),
    "local_audio.channels.channelB.gain": ("channel_b_gain", {"type": "integer", "label": "Channel B Gain", "min": 0, "control": True}),
    "local_audio.channels.channelB.mute": ("channel_b_mute", {"type": "boolean", "label": "Channel B Mute", "control": True}),
    "srt.mode": ("srt_mode", {"type": "enum", "label": "SRT Mode", "values": ["listener", "caller", "rendezvous"]}),
    "srt.latency": ("srt_latency_ms", {"type": "integer", "label": "SRT Latency (ms)", "min": 80, "max": 8000, "unit": "ms", "control": True}),
    "srt.port": ("srt_port", {"type": "integer", "label": "SRT Port", "min": 1024, "max": 65535}),
    "srt.url": ("srt_url", {"type": "string", "label": "SRT URL"}),
    "srt.stream_id": ("srt_stream_id", {"type": "string", "label": "SRT Stream ID"}),
    "srt.source_port": ("srt_source_port", {"type": "integer", "label": "SRT Source Port", "min": 0, "max": 65535}),
    "srt.ts_utc_midnight_origin": ("srt_utc_midnight_timestamps", {"type": "boolean", "label": "UTC Midnight Timestamps"}),
    "srt.encryption.keylength": ("srt_key_length", {"type": "integer", "label": "SRT Key Length"}),
    "rtsp.url": ("rtsp_url", {"type": "string", "label": "RTSP URL"}),
    "rtsp.username": ("rtsp_username", {"type": "string", "label": "RTSP Username"}),
    "rtsp.transport": ("rtsp_transport", {"type": "enum", "label": "RTSP Transport", "values": ["udp", "tcp", "udp_multicast"]}),
    "web_graphics.url": ("web_url", {"type": "string", "label": "Web Page URL", "control": True}),
    "web_graphics.resolution": ("web_resolution", {"type": "string", "label": "Render Resolution"}),
    "web_graphics.fps": ("web_fps", {"type": "integer", "label": "Render Frame Rate", "min": 1, "max": 99}),
    "usb.capture_mode": ("capture_mode", {"type": "string", "label": "Capture Mode"}),
    "usb.brightness": ("brightness", {"type": "integer", "label": "Brightness", "min": 0, "max": 100, "control": True}),
    "usb.contrast": ("contrast", {"type": "integer", "label": "Contrast", "min": 0, "max": 100, "control": True}),
    "usb.tilt": ("tilt", {"type": "integer", "label": "Tilt", "min": -64, "max": 64, "control": True}),
    "usb.pan": ("pan", {"type": "integer", "label": "Pan", "min": -64, "max": 64, "control": True}),
    "usb.saturation": ("saturation", {"type": "integer", "label": "Saturation", "min": 0, "max": 100, "control": True}),
    "usb.sharpness": ("sharpness", {"type": "integer", "label": "Sharpness", "min": 0, "max": 100, "control": True}),
    "usb.power_line_frequency": ("power_line_frequency", {"type": "integer", "label": "Power Line Frequency (0 off, 1 = 50 Hz, 2 = 60 Hz)", "min": 0, "max": 2}),
    "usb.volume": ("volume", {"type": "integer", "label": "Capture Volume (%)", "min": 0, "max": 100, "unit": "%", "control": True}),
    "ndi.group": ("ndi_group", {"type": "string", "label": "NDI Group"}),
    "ndi.name": ("ndi_name", {"type": "string", "label": "NDI Source Name", "control": True}),
    "ndi.extra_source_ip_addresses": ("ndi_extra_source_ips", {"type": "string", "label": "Extra NDI Source IPs"}),
    "ndi.ignore_timecode": ("ndi_ignore_timecode", {"type": "boolean", "label": "Ignore NDI Timecode"}),
}

# Where an input's audio mute and gain live, by the settings block it carries
# (LocalAudioSettings for analog and USB audio, the HDMI and SDI blocks for
# embedded audio). The driver routes mute_input / set_input_gain through
# whichever block the input actually reported.
MUTE_PATHS = ("local_audio.mute", "hdmi.audio.mute", "sdi.audio.mute")
GAIN_PATHS = ("local_audio.gain",)
DELAY_PATHS = ("audio.delay", "hdmi.audio.delay", "sdi.audio.delay")


def _flatten(doc: Any, prefix: str = "") -> dict[str, Any]:
    """``{"a": {"b": 1}}`` -> ``{"a.b": 1}``. Lists and null stay as leaves."""
    out: dict[str, Any] = {}
    if isinstance(doc, dict):
        for key, value in doc.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                out.update(_flatten(value, path))
            else:
                out[path] = value
    return out


def _nest(path: str, value: Any) -> dict[str, Any]:
    """``"a.b.c", 1`` -> ``{"a": {"b": {"c": 1}}}``."""
    parts = path.split(".")
    body: dict[str, Any] = {parts[-1]: value}
    for part in reversed(parts[:-1]):
        body = {part: body}
    return body


def input_setting_schema(settings: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Build an input's control schema from its settings document.

    Returns ``(schema, paths)``: the per-child state-variable map and, for
    each property, the dotted path it is written back to. Secrets are left
    out entirely. A field the table does not know is typed from its value.
    """
    schema: dict[str, dict[str, Any]] = {}
    paths: dict[str, str] = {}
    for path, value in _flatten(settings).items():
        leaf = path.rsplit(".", 1)[-1]
        if leaf in SECRET_SETTING_KEYS:
            continue
        if value is None or isinstance(value, (list, dict)):
            # A null field (an SRT input with encryption off) says nothing
            # about its type, so it is not offered as a control.
            continue
        known = INPUT_SETTING_DEFS.get(path)
        if known:
            prop, var_def = known
            var_def = dict(var_def)
        else:
            prop = "_".join(_snake(p) for p in path.split("."))
            if isinstance(value, bool):
                var_type = "boolean"
            elif isinstance(value, int):
                var_type = "integer"
            elif isinstance(value, float):
                var_type = "number"
            else:
                var_type = "string"
            var_def = {"type": var_type, "label": path.replace(".", " ").replace("_", " ").title()}
        if prop in schema:
            # Two blocks naming the same property (an HDMI block and a local
            # audio block on one input): keep the first, publish the second
            # under its full path so nothing is lost.
            prop = "_".join(_snake(p) for p in path.split("."))
            var_def = dict(var_def)
        schema[prop] = var_def
        paths[prop] = path
    return schema, paths


def coerce_setting_value(var_def: dict[str, Any], value: Any) -> Any:
    """A value typed by the input's own schema, as the JSON body wants it."""
    var_type = var_def.get("type", "string")
    if var_type == "boolean":
        if isinstance(value, str) and value.strip().lower() not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
            raise ValueError(f"{value!r} is not a yes/no value")
        return _bool_text(value)
    if var_type == "integer":
        number = _int(value)
        if number is None:
            raise ValueError(f"{value!r} is not a whole number")
        return number
    if var_type == "number":
        number = _float(value)
        if number is None:
            raise ValueError(f"{value!r} is not a number")
        return number
    if var_type == "enum":
        allowed = [str(v) for v in var_def.get("values", [])]
        text = str(value)
        if allowed and text not in allowed:
            raise ValueError(f"{value!r} is not one of {', '.join(allowed)}")
        return text
    return str(value)


class PearlError(Exception):
    """An answer the Pearl gave that is not the one asked for: a JSON
    ``status`` other than ``ok``, an HTTP error, or a body that is not JSON."""

    def __init__(self, message: str, *, status: str = "", http_status: int = 0):
        super().__init__(message)
        self.status = status
        self.http_status = http_status

    @property
    def not_authorized(self) -> bool:
        return self.http_status in (401, 403)

    @property
    def not_found(self) -> bool:
        return self.http_status == 404


class EpiphanPearlDriver(BaseDriver):
    """Epiphan Pearl driver over the Pearl device REST API v2.0."""

    DRIVER_INFO = {
        "id": "epiphan_pearl",
        "name": "Epiphan Pearl",
        "manufacturer": "Epiphan",
        "category": "streaming",
        "version": "1.0.0",
        # The connection lifecycle hooks this driver overrides landed in
        # 0.24.0 (the sibling HTTP drivers declare the same floor); the
        # channel_rtsp_ports table field alone would need 0.23.0.
        "min_platform_version": "0.25.0",
        "author": "OpenAVC",
        "description": (
            "Controls Epiphan Pearl Nano, Mini, Nexus and Pearl-2 lecture-capture "
            "and live production systems over the Pearl REST API: start and stop "
            "recording and streaming for the whole unit, a channel or one stream, "
            "switch layouts, add bookmarks, set input gain, mute and phantom power, "
            "pick the HDMI output source, run CMS events (Kaltura, Panopto, "
            "Opencast), apply configuration presets, and watch storage, uploads, "
            "CPU and temperature."
        ),
        "source_url": "https://epiphan-video.github.io/pearl_api_swagger_ui/",
        "tags": ["encoder", "recorder", "lecture-capture", "streaming", "srt", "rtmp", "ndi",
                 "panopto", "kaltura", "opencast"],
        "verified": False,
        "simulated": True,
        "transport": "http",
        "ports": [80, 443],
        "protocols": ["epiphan-rest"],
        "compatible_models": [
            {
                "manufacturer": "Epiphan",
                "models": ["Pearl Nano", "Pearl Mini", "Pearl Nexus", "Pearl-2",
                           "Pearl-2 Rackmount", "Pearl-2 Rackmount Twin"],
                "confidence": "untested",
                "notes": (
                    "Built from the Pearl device REST API v2.0. The Pearl Nano has one "
                    "channel and one layout; multi-channel commands act on that one."
                ),
            },
        ],
        "help": {
            "overview": (
                "Epiphan Pearl is a hardware encoder that records and streams several "
                "channels at once, each composed from the unit's inputs by a layout.\n\n"
                "OpenAVC starts and stops recording (every recorder, or one), starts and "
                "stops streams (every stream, one channel's, or one stream), switches a "
                "channel's layout, drops bookmarks into a recording, sets input gain, "
                "mute, phantom power and delays, picks what the HDMI output shows, "
                "controls scheduled CMS events, applies configuration presets and "
                "reboots the unit. Live state covers every recorder and stream, the "
                "one-touch control, storage, file uploads, CPU load and temperature.\n\n"
                "Each channel is also a Video Panel source over RTSP once its port is "
                "entered under Channel RTSP Ports."
            ),
            "setup": (
                "1. Enter the Pearl's IP address. The REST API answers on the web port: "
                "80, or 443 with Use HTTPS when HTTPS is enabled on the Pearl.\n"
                "2. Enter the admin account's password (Pearl firmware 4.14.2 and later "
                "require one on every account).\n"
                "3. For channel previews in the Video Panel, open the channel's Status "
                "page in the Pearl Admin panel, read the RTSP stream URL's port and "
                "enter it under Channel RTSP Ports. The Pearl Nano's channel is "
                "always 554.\n"
                "4. Stream destinations, layouts and CMS settings are created in the "
                "Pearl Admin panel; this driver starts, stops and switches them."
            ),
            "connection": (
                "Check the admin password and whether HTTPS is enabled on the Pearl "
                "(then tick Use HTTPS and set the port to 443)."
            ),
        },
        "default_config": {
            "host": "",
            "port": 80,
            "ssl": False,
            "verify_ssl": False,
            "username": "admin",
            "password": "",
            "poll_interval": 5,
            "detail_poll_every": 6,
            "channel_rtsp_ports": [],
            "stream_username": "",
            "stream_password": "",
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {
                "type": "integer", "default": 80, "min": 1, "max": 65535, "label": "Port",
                "help": "The Pearl's web port: 80 for HTTP, 443 when HTTPS is enabled.",
            },
            "ssl": {
                "type": "boolean", "default": False, "label": "Use HTTPS",
                "help": "Turn on when HTTPS is enabled on the Pearl, and set the port to 443.",
            },
            "verify_ssl": {
                "type": "boolean", "default": False, "label": "Verify Certificate", "advanced": True,
                "help": "Check the Pearl's HTTPS certificate against the system's trusted roots. Off for the self-signed certificate a Pearl ships with.",
            },
            "username": {
                "type": "string", "default": "admin", "label": "Username",
                "help": "A Pearl user account. The admin account can do everything here.",
            },
            "password": {
                "type": "string", "default": "", "label": "Password", "secret": True,
                "help": "The account's password. Pearl firmware 4.14.2 and later require one.",
            },
            "poll_interval": {
                "type": "integer", "default": 5, "min": 0, "label": "Poll Interval (sec)",
                "help": "How often recorder, stream and system status are read. 0 disables polling.",
            },
            "detail_poll_every": {
                "type": "integer", "default": 6, "min": 1, "max": 120, "label": "Detail Refresh (polls)", "advanced": True,
                "help": "Inputs, outputs, storage, presets, events and recording archives are re-read every this many polls.",
            },
            "channel_rtsp_ports": {
                "type": "table", "label": "Channel RTSP Ports", "row_label": "channel", "advanced": True,
                "help": (
                    "The RTSP port of each channel, from the channel's Status page in the Pearl "
                    "Admin panel (rtsp://<address>:<port>/stream.sdp). Needed for the channel to "
                    "appear as a Video Panel source. The Pearl Nano's channel is always 554."
                ),
                "columns": {
                    "channel": {"type": "string", "label": "Channel ID", "required": True,
                                "help": "The channel number shown in the Admin panel (1, 2, ...)."},
                    "port": {"type": "integer", "label": "RTSP Port", "required": True, "min": 1, "max": 65535},
                },
            },
            "stream_username": {
                "type": "string", "default": "", "label": "Stream Username", "advanced": True,
                "help": (
                    "A Pearl account (viewer is enough) embedded in the published stream and "
                    "snapshot URLs so the Video Panel can open them. Leave blank when the "
                    "Pearl's viewer account has no password."
                ),
            },
            "stream_password": {
                "type": "string", "default": "", "label": "Stream Password", "secret": True, "advanced": True,
                "help": "Its password. It becomes part of the URLs shown in Live State.",
            },
        },
        "state_variables": {
            "product_name": {"type": "string", "label": "Product"},
            "product_id": {"type": "integer", "label": "Product ID"},
            "firmware_version": {"type": "string", "label": "Firmware Version"},
            "firmware_revision": {"type": "string", "label": "Firmware Revision"},
            "device_name": {"type": "string", "label": "Device Name"},
            "device_location": {"type": "string", "label": "Location"},
            "device_description": {"type": "string", "label": "Description"},
            "system_time": {"type": "string", "label": "System Time", "cloud_priority": "low"},
            "uptime_seconds": {"type": "integer", "label": "Uptime (s)", "unit": "s", "cloud_priority": "low"},
            "cpu_load_percent": {"type": "integer", "label": "CPU Load (%)", "min": 0, "max": 100, "unit": "%", "cloud_priority": "low"},
            "cpu_load_high": {"type": "boolean", "label": "CPU Load High", "cloud_priority": "high"},
            "cpu_temp_c": {"type": "number", "label": "CPU Temperature (C)", "unit": "C", "cloud_priority": "low"},
            "cpu_temp_threshold_c": {"type": "number", "label": "CPU Temperature Limit (C)", "unit": "C"},
            "cpu_temp_high": {"type": "boolean", "label": "CPU Temperature High", "cloud_priority": "high",
                              "help": "True while the CPU temperature is at or above the Pearl's own limit."},
            "external_ip": {"type": "string", "label": "External IP"},
            "mdns_name": {"type": "string", "label": "Bonjour Name"},
            "dns_status": {"type": "enum", "values": ["disabled", "ok", "error"], "label": "DNS"},
            "http_status": {"type": "enum", "values": ["disabled", "ok", "error"], "label": "HTTP Reachability"},
            "https_status": {"type": "enum", "values": ["disabled", "ok", "error"], "label": "HTTPS Reachability"},
            "captive_portal_status": {"type": "enum", "values": ["disabled", "ok", "error"], "label": "Captive Portal Check"},
            "icmp_status": {"type": "enum", "values": ["disabled", "ok", "error"], "label": "ICMP Reachability"},
            "epiphan_edge_status": {"type": "enum", "values": ["disabled", "ok", "error"], "label": "Epiphan Edge"},
            "vtun_status": {"type": "enum", "values": ["disabled", "ok", "error"], "label": "Remote Login Tunnel"},
            "channel_count": {"type": "integer", "label": "Channels"},
            "input_count": {"type": "integer", "label": "Inputs"},
            "recorder_count": {"type": "integer", "label": "Recorders"},
            "publisher_count": {"type": "integer", "label": "Streams"},
            "recording": {"type": "boolean", "label": "Recording", "cloud_priority": "high",
                          "help": "True while any recorder is started."},
            "recorders_active": {"type": "integer", "label": "Recorders Running", "cloud_priority": "high"},
            "streaming": {"type": "boolean", "label": "Streaming", "cloud_priority": "high",
                          "help": "True while any stream is started."},
            "publishers_active": {"type": "integer", "label": "Streams Running", "cloud_priority": "high"},
            "event_ongoing_id": {"type": "string", "label": "Ongoing Event ID"},
            "event_ongoing_title": {"type": "string", "label": "Ongoing Event", "cloud_priority": "high"},
            "event_ongoing_status": {"type": "enum", "values": ["scheduled", "running", "paused", "finished"], "label": "Ongoing Event Status", "cloud_priority": "high"},
            "event_ongoing_start": {"type": "integer", "label": "Ongoing Event Start (Unix s)"},
            "event_ongoing_finish": {"type": "integer", "label": "Ongoing Event Finish (Unix s)"},
            "event_upcoming_id": {"type": "string", "label": "Next Event ID"},
            "event_upcoming_title": {"type": "string", "label": "Next Event"},
            "event_upcoming_start": {"type": "integer", "label": "Next Event Start (Unix s)"},
            "event_upcoming_finish": {"type": "integer", "label": "Next Event Finish (Unix s)"},
            "adhoc_user_id": {"type": "string", "label": "Ad-hoc Session User"},
            "adhoc_user_name": {"type": "string", "label": "Ad-hoc Session Name"},
            "adhoc_session_expires": {"type": "integer", "label": "Ad-hoc Session Expiry (Unix s)"},
            "preset_options": {"type": "string", "label": "Configuration Presets",
                               "help": "JSON list of the presets on the device; feeds the Apply Preset picker."},
            "output_source_options": {"type": "string", "label": "Output Sources",
                                      "help": "JSON list of what an HDMI output can show; feeds the Set Output Source picker."},
            "speedtest_bandwidth_bps": {"type": "integer", "label": "Speed Test Bandwidth (bit/s)", "unit": "bit/s"},
            "speedtest_mode": {"type": "string", "label": "Speed Test Direction"},
            "speedtest_protocol": {"type": "string", "label": "Speed Test Protocol"},
            "speedtest_duration_s": {"type": "integer", "label": "Speed Test Duration (s)", "unit": "s"},
            "speedtest_udp_loss": {"type": "integer", "label": "Speed Test UDP Loss"},
            "last_error": {"type": "string", "label": "Last Error"},
        },
        "child_entity_types": {
            "channel": {
                "label": "Channel", "label_plural": "Channels",
                "id_format": {"type": "string"},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "streaming": {"type": "boolean", "label": "Streaming", "cloud_priority": "high",
                                  "help": "True while any of the channel's streams is started."},
                    "publisher_count": {"type": "integer", "label": "Streams"},
                    "publishers_active": {"type": "integer", "label": "Streams Running", "cloud_priority": "high"},
                    "active_layout_id": {"type": "string", "label": "Active Layout ID", "cloud_priority": "high"},
                    "active_layout_name": {"type": "string", "label": "Active Layout", "cloud_priority": "high"},
                    "active_layout_sources": {"type": "string", "label": "Layout Sources"},
                    "video_codec": {"type": "string", "label": "Video Codec"},
                    "video_resolution": {"type": "string", "label": "Resolution"},
                    "video_framerate": {"type": "number", "label": "Frame Rate"},
                    "video_bitrate_kbps": {"type": "integer", "label": "Video Bitrate (kbit/s)", "unit": "kbit/s"},
                    "audio_codec": {"type": "string", "label": "Audio Codec"},
                    "audio_channels": {"type": "integer", "label": "Audio Channels"},
                    "audio_bitrate_kbps": {"type": "integer", "label": "Audio Bitrate (kbit/s)", "unit": "kbit/s"},
                    "preview_url": {"type": "string", "label": "Preview Stream URL"},
                    "preview_format": {"type": "string", "label": "Preview Format"},
                    "preview_status": {"type": "string", "label": "Preview Status"},
                    "preview_setup_field": {"type": "string", "label": "Preview Setup Field"},
                    "preview_status_detail": {"type": "string", "label": "Preview Status Detail"},
                    "snapshot_url": {"type": "string", "label": "Snapshot URL"},
                },
                "summary_fields": ["name", "streaming", "active_layout_name"],
                "label_field": "name",
            },
            "publisher": {
                "label": "Stream", "label_plural": "Streams",
                "id_format": {"type": "string"},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "channel_id": {"type": "string", "label": "Channel ID"},
                    "publisher_id": {"type": "string", "label": "Stream ID"},
                    "type": {"type": "enum", "values": ["rtsp", "rtmp", "rtp-udp", "mpegts-udp", "mpegts-rtp", "ndi", "hls", "srt"], "label": "Type"},
                    "state": {"type": "enum", "values": ["starting", "listening", "started", "stopped", "error"], "label": "State", "cloud_priority": "high"},
                    "started": {"type": "boolean", "label": "Started", "cloud_priority": "high", "control": True},
                    "is_configured": {"type": "boolean", "label": "Configured"},
                    "state_detail": {"type": "string", "label": "Error"},
                    "warnings": {"type": "string", "label": "Warnings"},
                    "duration_s": {"type": "integer", "label": "Duration (s)", "unit": "s", "cloud_priority": "low"},
                    "since": {"type": "integer", "label": "Started At (Unix s)"},
                    "reconnections": {"type": "integer", "label": "Reconnections"},
                    "enabled": {"type": "boolean", "label": "Enabled", "control": True},
                    "single_touch": {"type": "boolean", "label": "In One-Touch"},
                    "audio_disabled": {"type": "boolean", "label": "Audio Disabled"},
                    "url": {"type": "string", "label": "Destination URL"},
                    "srt_mode": {"type": "string", "label": "SRT Mode"},
                    "srt_port": {"type": "integer", "label": "SRT Port"},
                    "srt_latency_ms": {"type": "integer", "label": "SRT Latency (ms)", "unit": "ms"},
                    "ndi_name": {"type": "string", "label": "NDI Name"},
                    "send_rate": {"type": "number", "label": "Send Rate", "cloud_priority": "low"},
                    "rtt": {"type": "number", "label": "Round Trip Time", "cloud_priority": "low"},
                    "loss_ratio": {"type": "number", "label": "Loss Ratio", "cloud_priority": "low"},
                    "estimated_bandwidth": {"type": "number", "label": "Estimated Bandwidth", "cloud_priority": "low"},
                },
                "summary_fields": ["name", "type", "state"],
                "label_field": "name",
            },
            "recorder": {
                "label": "Recorder", "label_plural": "Recorders",
                "id_format": {"type": "string"},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "multisource": {"type": "boolean", "label": "Multitrack"},
                    "state": {"type": "enum", "values": ["disabled", "starting", "started", "stopped", "error"], "label": "State", "cloud_priority": "high"},
                    "recording": {"type": "boolean", "label": "Recording", "cloud_priority": "high", "control": True},
                    "state_detail": {"type": "string", "label": "Error"},
                    "duration_s": {"type": "integer", "label": "Duration (s)", "unit": "s", "cloud_priority": "low"},
                    "active": {"type": "string", "label": "Active Sources"},
                    "total": {"type": "string", "label": "Total Sources"},
                    "latest_recording_name": {"type": "string", "label": "Latest Recording"},
                    "latest_recording_created": {"type": "string", "label": "Latest Recording Created"},
                    "latest_recording_duration_s": {"type": "integer", "label": "Latest Recording Duration (s)", "unit": "s"},
                    "latest_recording_size_bytes": {"type": "integer", "label": "Latest Recording Size (bytes)", "unit": "bytes"},
                    "latest_recording_in_progress": {"type": "boolean", "label": "Latest Recording In Progress"},
                },
                "summary_fields": ["name", "state", "duration_s"],
                "label_field": "name",
            },
            "input": {
                "label": "Input", "label_plural": "Inputs",
                "id_format": {"type": "string"},
                "dynamic": True,
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "real_device_name": {"type": "string", "label": "Device Name"},
                    "type": {"type": "enum", "values": ["embedded", "usb", "rtsp", "ndi", "srt", "web-graphics"], "label": "Type"},
                    "has_audio": {"type": "boolean", "label": "Audio"},
                    "has_video": {"type": "boolean", "label": "Video"},
                    "settings_supported": {"type": "boolean", "label": "Has Settings"},
                    "snapshot_url": {"type": "string", "label": "Snapshot URL"},
                },
                "summary_fields": ["name", "type", "has_audio"],
                "label_field": "name",
            },
            "output": {
                "label": "Output", "label_plural": "Outputs",
                "id_format": {"type": "string"},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "snapshot_url": {"type": "string", "label": "Snapshot URL"},
                },
                "summary_fields": ["name"],
                "label_field": "name",
            },
            "storage": {
                "label": "Storage", "label_plural": "Storage",
                "id_format": {"type": "string"},
                "state_variables": {
                    "state": {"type": "enum", "values": ["ready", "nodev", "dev", "devro", "formatting"], "label": "State", "cloud_priority": "high"},
                    "total_bytes": {"type": "integer", "label": "Capacity (bytes)", "unit": "bytes"},
                    "free_bytes": {"type": "integer", "label": "Free (bytes)", "unit": "bytes"},
                    "free_percent": {"type": "integer", "label": "Free (%)", "min": 0, "max": 100, "unit": "%"},
                    "transfer_state": {"type": "string", "label": "Transfer State"},
                    "transfer_total_count": {"type": "integer", "label": "Transfer Files Total"},
                    "transfer_processed_count": {"type": "integer", "label": "Transfer Files Done"},
                },
                "summary_fields": ["state", "free_percent"],
            },
            "afu": {
                "label": "File Upload", "label_plural": "File Uploads",
                "id_format": {"type": "string"},
                "state_variables": {
                    "state": {"type": "enum", "values": ["idle", "paused", "uploading", "error", "disabled"], "label": "State", "cloud_priority": "high"},
                    "protocol": {"type": "string", "label": "Protocol"},
                    "queue_files": {"type": "integer", "label": "Files Queued"},
                    "queue_size_bytes": {"type": "integer", "label": "Queue Size (bytes)", "unit": "bytes"},
                    "uploading_file": {"type": "string", "label": "Uploading"},
                    "uploaded_bytes": {"type": "integer", "label": "Uploaded (bytes)", "unit": "bytes", "cloud_priority": "low"},
                    "file_size_bytes": {"type": "integer", "label": "File Size (bytes)", "unit": "bytes"},
                    "error_message": {"type": "string", "label": "Error"},
                },
                "summary_fields": ["state", "protocol", "queue_files"],
            },
            "single_touch": {
                "label": "One-Touch Control", "label_plural": "One-Touch Controls",
                "id_format": {"type": "string"},
                "state_variables": {
                    "pressed": {"type": "boolean", "label": "Active", "cloud_priority": "high", "control": True},
                    "status": {"type": "boolean", "label": "All Started", "cloud_priority": "high"},
                    "recorders_total": {"type": "integer", "label": "Recorders"},
                    "recorders_active": {"type": "integer", "label": "Recorders Running"},
                    "recorders_success": {"type": "integer", "label": "Recorders OK"},
                    "publishers_total": {"type": "integer", "label": "Streams"},
                    "publishers_active": {"type": "integer", "label": "Streams Running"},
                    "publishers_success": {"type": "integer", "label": "Streams OK"},
                },
                "summary_fields": ["pressed", "status"],
            },
        },
        "commands": {
            # ── Recording ──
            "start_all_recorders": {"label": "Start All Recorders", "params": {},
                                    "help": "Start every recorder on the unit."},
            "stop_all_recorders": {"label": "Stop All Recorders", "params": {}},
            "start_recorder": {"label": "Start Recorder", "params": {"recorder": {"type": "child_id", "child_type": "recorder", "required": True, "label": "Recorder"}}},
            "stop_recorder": {"label": "Stop Recorder", "params": {"recorder": {"type": "child_id", "child_type": "recorder", "required": True, "label": "Recorder"}}},
            "add_bookmark": {
                "label": "Add Bookmark",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True, "label": "Channel"},
                    "text": {"type": "string", "required": True, "label": "Bookmark Name", "trim": False},
                },
                "help": "Mark this moment in the channel's current recording (MP4 or MOV only).",
            },
            # ── Streaming ──
            "start_all_streams": {"label": "Start All Streams", "params": {},
                                  "help": "Start every stream of every channel."},
            "stop_all_streams": {"label": "Stop All Streams", "params": {}},
            "start_channel_streams": {"label": "Start Channel Streams", "params": {"channel": {"type": "child_id", "child_type": "channel", "required": True, "label": "Channel"}},
                                      "help": "Start every stream of one channel."},
            "stop_channel_streams": {"label": "Stop Channel Streams", "params": {"channel": {"type": "child_id", "child_type": "channel", "required": True, "label": "Channel"}}},
            "start_publisher": {"label": "Start Stream", "params": {"publisher": {"type": "child_id", "child_type": "publisher", "required": True, "label": "Stream", "help": "A channel's publisher (stream destination)."}}},
            "stop_publisher": {"label": "Stop Stream", "params": {"publisher": {"type": "child_id", "child_type": "publisher", "required": True, "label": "Stream", "help": "A channel's publisher (stream destination)."}}},
            "set_publisher_enabled": {
                "label": "Enable / Disable Stream",
                "params": {"publisher": {"type": "child_id", "child_type": "publisher", "required": True, "label": "Stream", "help": "A channel's publisher (stream destination)."},
                           "enabled": {"type": "boolean", "required": True, "label": "Enabled"}},
            },
            "set_publisher_single_touch": {
                "label": "Include Stream in One-Touch",
                "params": {"publisher": {"type": "child_id", "child_type": "publisher", "required": True, "label": "Stream", "help": "A channel's publisher (stream destination)."},
                           "included": {"type": "boolean", "required": True, "label": "Included"}},
                "help": "Whether the one-touch control starts and stops this stream.",
            },
            "rename_publisher": {
                "label": "Rename Stream",
                "params": {"publisher": {"type": "child_id", "child_type": "publisher", "required": True, "label": "Stream", "help": "A channel's publisher (stream destination)."},
                           "name": {"type": "string", "required": True, "label": "Name"}},
            },
            "set_publisher_url": {
                "label": "Set Stream Destination",
                "params": {"publisher": {"type": "child_id", "child_type": "publisher", "required": True, "label": "Stream", "help": "A channel's publisher (stream destination)."},
                           "url": {"type": "string", "required": True, "label": "URL",
                                   "help": "RTMP, RTSP announce, HLS, or SRT caller / rendezvous URL."}},
                "help": "Change where an RTMP, RTSP, HLS or SRT (caller / rendezvous) stream goes.",
            },
            "set_rtmp_stream_key": {
                "label": "Set RTMP Stream Key",
                "params": {"publisher": {"type": "child_id", "child_type": "publisher", "required": True, "label": "Stream", "help": "A channel's publisher (stream destination)."},
                           "stream_key": {"type": "string", "required": True, "label": "Stream Key", "secret": True, "trim": False}},
            },
            "delete_publisher": {"label": "Delete Stream", "params": {"publisher": {"type": "child_id", "child_type": "publisher", "required": True, "label": "Stream", "help": "A channel's publisher (stream destination)."}}},
            "add_rtmp_publisher": {
                "label": "Add RTMP Stream",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True, "label": "Channel"},
                    "name": {"type": "string", "required": True, "label": "Name"},
                    "url": {"type": "string", "required": True, "label": "RTMP URL"},
                    "stream_key": {"type": "string", "label": "Stream Key", "secret": True, "trim": False},
                    "username": {"type": "string", "label": "Username"},
                    "password": {"type": "string", "label": "Password", "secret": True},
                    "enabled": {"type": "boolean", "label": "Enabled"},
                },
            },
            "add_srt_publisher": {
                "label": "Add SRT Stream",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True, "label": "Channel"},
                    "name": {"type": "string", "required": True, "label": "Name"},
                    "mode": {"type": "enum", "required": True, "label": "Mode",
                             "values": ["caller", "listener", "rendezvous"]},
                    "url": {"type": "string", "label": "SRT URL", "help": "Caller and rendezvous modes (srt://host:port)."},
                    "port": {"type": "integer", "label": "Listener Port", "min": 1024, "max": 65535},
                    "latency_ms": {"type": "integer", "label": "Latency (ms)", "min": 80, "max": 8000, "unit": "ms"},
                    "passphrase": {"type": "string", "label": "Passphrase", "secret": True,
                                   "help": "10 to 79 characters. Leave blank for no encryption."},
                    "enabled": {"type": "boolean", "label": "Enabled"},
                },
            },
            "add_ndi_publisher": {
                "label": "Add NDI Stream",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True, "label": "Channel"},
                    "name": {"type": "string", "required": True, "label": "Name"},
                    "ndi_name": {"type": "string", "required": True, "label": "NDI Source Name"},
                    "ndi_group": {"type": "string", "label": "NDI Group"},
                    "enabled": {"type": "boolean", "label": "Enabled"},
                },
            },
            # ── Channels ──
            "set_channel_layout": {
                "label": "Switch Layout",
                "params": {"channel": {"type": "child_id", "child_type": "channel", "required": True, "label": "Channel"},
                           "layout": {"type": "string", "required": True, "label": "Layout ID",
                                      "help": "The layout's number in the channel's Layouts page (1, 2, ...)."}},
            },
            "rename_channel": {
                "label": "Rename Channel",
                "params": {"channel": {"type": "child_id", "child_type": "channel", "required": True, "label": "Channel"},
                           "name": {"type": "string", "required": True, "label": "Name"}},
            },
            # ── Inputs ──
            "mute_input": {"label": "Mute Input", "params": {"input": {"type": "child_id", "child_type": "input", "required": True, "label": "Input"}}},
            "unmute_input": {"label": "Unmute Input", "params": {"input": {"type": "child_id", "child_type": "input", "required": True, "label": "Input"}}},
            "set_input_gain": {
                "label": "Set Input Gain",
                "params": {"input": {"type": "child_id", "child_type": "input", "required": True, "label": "Input"},
                           "gain": {"type": "integer", "required": True, "label": "Gain", "min": 0,
                                    "help": "Capture gain. dB or percent depending on the input."}},
            },
            "set_input_audio_delay": {
                "label": "Set Input Audio Delay",
                "params": {"input": {"type": "child_id", "child_type": "input", "required": True, "label": "Input"},
                           "delay_ms": {"type": "integer", "required": True, "label": "Delay (ms)", "min": -300, "max": 300, "unit": "ms"}},
            },
            "set_input_setting": {
                "label": "Set Input Setting",
                "params": {
                    "input": {"type": "child_id", "child_type": "input", "required": True, "label": "Input"},
                    "setting": {"type": "string", "required": True, "label": "Setting",
                                "options_from": {"param": "input", "source": "child_schema"},
                                "help": "Pick the input above to list its settings."},
                    "value": {"type": "string", "required": True, "label": "Value",
                              "type_from": {"param": "setting"},
                              "help": "Numbers and true/false are typed from the setting."},
                },
                "help": "Change any setting an input reports (phantom power, SRT latency, deinterlacing, ...).",
            },
            "add_rtsp_input": {
                "label": "Add RTSP Input",
                "params": {
                    "name": {"type": "string", "required": True, "label": "Name"},
                    "url": {"type": "string", "required": True, "label": "RTSP URL"},
                    "username": {"type": "string", "label": "Username"},
                    "password": {"type": "string", "label": "Password", "secret": True},
                    "transport": {"type": "enum", "label": "Transport", "values": ["udp", "tcp", "udp_multicast"]},
                },
            },
            "add_srt_input": {
                "label": "Add SRT Input",
                "params": {
                    "name": {"type": "string", "required": True, "label": "Name"},
                    "mode": {"type": "enum", "required": True, "label": "Mode",
                             "values": ["listener", "caller", "rendezvous"]},
                    "url": {"type": "string", "label": "SRT URL", "help": "Caller and rendezvous modes."},
                    "port": {"type": "integer", "label": "Listener Port", "min": 1024, "max": 65535},
                    "latency_ms": {"type": "integer", "label": "Latency (ms)", "min": 80, "max": 8000, "unit": "ms"},
                    "passphrase": {"type": "string", "label": "Passphrase", "secret": True,
                                   "help": "10 to 79 characters. Leave blank for no encryption."},
                },
            },
            "add_ndi_input": {
                "label": "Add NDI Input",
                "params": {
                    "name": {"type": "string", "required": True, "label": "Name"},
                    "ndi_name": {"type": "string", "required": True, "label": "NDI Source Name"},
                    "ndi_group": {"type": "string", "label": "NDI Group"},
                },
            },
            "add_web_graphics_input": {
                "label": "Add Web Graphics Input",
                "params": {
                    "name": {"type": "string", "required": True, "label": "Name"},
                    "url": {"type": "string", "required": True, "label": "Web Page URL"},
                    "resolution": {"type": "string", "label": "Resolution", "pattern": r"^\d+x\d+$|^$",
                                   "help": "Width x height, e.g. 1920x1080."},
                    "fps": {"type": "integer", "label": "Frame Rate", "min": 1, "max": 99},
                },
            },
            # ── Outputs ──
            "set_output_source": {
                "label": "Set Output Source",
                "params": {
                    "output": {"type": "child_id", "child_type": "output", "required": True, "label": "Output"},
                    "source": {"type": "string", "required": True, "label": "Source",
                               "options_state": "output_source_options",
                               "help": "A channel, an input, the multiview, the device info page or the local console."},
                },
            },
            # ── Storage, one-touch, presets ──
            "eject_storage": {"label": "Eject Storage", "params": {"storage": {"type": "child_id", "child_type": "storage", "required": True, "label": "Storage"}},
                              "help": "Safely eject the external drive so it can be removed."},
            "toggle_single_touch": {
                "label": "Toggle One-Touch",
                "params": {"control": {"type": "child_id", "child_type": "single_touch", "required": True, "label": "One-Touch Control"}},
                "help": "Press the one-touch control: starts or stops every recorder and stream it includes.",
            },
            "apply_preset": {
                "label": "Apply Configuration Preset",
                "params": {
                    "preset": {"type": "string", "required": True, "label": "Preset", "options_state": "preset_options"},
                    "sections": {"type": "string", "label": "Sections",
                                 "help": "Comma-separated sections to apply (system, network, sources, channels, ...). Blank applies the whole preset."},
                },
                "help": "Load a saved configuration. The Pearl may reboot to apply it.",
            },
            # ── CMS events ──
            "start_upcoming_event": {"label": "Start Next Event Now", "params": {},
                                     "help": "Force the next scheduled event to start ahead of time."},
            "stop_ongoing_event": {"label": "Stop Ongoing Event", "params": {}},
            "pause_event": {"label": "Pause Event", "params": {}},
            "resume_event": {"label": "Resume Event", "params": {}},
            "extend_event": {
                "label": "Extend Event",
                "params": {"minutes": {"type": "integer", "required": True, "label": "Minutes", "min": 1, "max": 1440}},
                "help": "Add time to the ongoing or paused event's finish.",
            },
            "create_adhoc_event": {
                "label": "Create Ad-hoc Event",
                "params": {
                    "cms": {"type": "enum", "required": True, "label": "CMS", "values": ["kaltura", "panopto", "opencast"]},
                    "title": {"type": "string", "required": True, "label": "Title"},
                    "duration_minutes": {"type": "integer", "required": True, "label": "Duration (min)", "min": 1, "max": 1440},
                    "type": {"type": "enum", "label": "Type", "values": ["vod", "live", "vod-live"],
                             "help": "Recording only (vod), live stream only (live), or both. Opencast events are recordings."},
                    "description": {"type": "string", "label": "Description"},
                    "start_in_minutes": {"type": "integer", "label": "Start In (min)", "min": 0, "max": 525600,
                                         "help": "Minutes from now. Blank or 0 starts immediately."},
                },
                "help": "Create an unscheduled event in the configured CMS. Kaltura and Panopto need an ad-hoc login first unless the CMS is configured with a default user.",
            },
            "adhoc_login": {
                "label": "Ad-hoc Login",
                "params": {
                    "cms": {"type": "enum", "required": True, "label": "CMS", "values": ["kaltura", "panopto"]},
                    "user_id": {"type": "string", "required": True, "label": "User ID or Email"},
                    "password": {"type": "string", "label": "Password", "secret": True, "help": "Panopto only."},
                },
                "help": "Open the one-hour session that ad-hoc events are created under.",
            },
            "adhoc_logout": {"label": "Ad-hoc Logout", "params": {}},
            # ── System ──
            "run_speed_test": {
                "label": "Run Speed Test",
                "params": {
                    "mode": {"type": "enum", "label": "Direction", "values": ["uplink", "downlink"]},
                    "protocol": {"type": "enum", "label": "Protocol", "values": ["tcp", "udp"]},
                    "timeout_s": {"type": "integer", "label": "Maximum Duration (s)", "min": 1, "max": 120, "unit": "s"},
                },
                "help": "Measure the Pearl's network speed. Takes up to the maximum duration (default 30 s).",
            },
            "reboot": {"label": "Reboot", "params": {}},
            "shutdown": {"label": "Shut Down", "params": {}},
        },
        "actions": [
            {"id": "start_all_recorders", "kind": "command", "icon": "circle"},
            {"id": "stop_all_recorders", "kind": "command", "icon": "square"},
            {"id": "start_all_streams", "kind": "command", "icon": "radio"},
            {"id": "stop_all_streams", "kind": "command", "icon": "square"},
            {"id": "toggle_single_touch", "kind": "command", "icon": "play"},
            {"id": "reboot", "kind": "command", "icon": "power",
             "confirm": "Reboot the Pearl? Every recording and stream stops and the unit is offline until it restarts."},
            {"id": "shutdown", "kind": "command", "icon": "power-off",
             "confirm": "Shut the Pearl down? Every recording and stream stops and it must be powered on by hand."},
        ],
        "discovery": {
            # No documented unauthenticated answer identifies a Pearl, and its
            # Bonjour service type is not published, so this driver is a
            # candidate by vendor name only: a scan that captures "Epiphan"
            # anywhere offers it.
            "manufacturer_alias": ["Epiphan", "Epiphan Systems", "Epiphan Systems Inc", "Epiphan Video"],
        },
    }

    # Liveness watchdog: a cheap read that proves the API still answers when
    # polling is off (see BaseDriver._liveness_probe).
    HEALTH_INTERVAL_S = 30.0
    HEALTH_TIMEOUT_S = 10.0
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = "Connected, but the Pearl stopped answering the REST API."

    def __init__(self, device_id: str, config: dict[str, Any], state, events):
        super().__init__(device_id, config, state, events)
        self._client = None
        self._poll_count = 0
        # Device identifiers behind each child local id.
        self._channel_ids: dict[str, str] = {}
        self._publisher_ids: dict[str, tuple[str, str]] = {}
        self._recorder_ids: dict[str, str] = {}
        self._input_ids: dict[str, str] = {}
        self._output_ids: dict[str, str] = {}
        self._storage_ids: dict[str, str] = {}
        self._afu_ids: dict[str, str] = {}
        self._single_touch_ids: dict[str, str] = {}
        # Per input: the dotted settings path behind each control, and its type.
        self._input_paths: dict[str, dict[str, str]] = {}
        self._input_schema: dict[str, dict[str, dict[str, Any]]] = {}
        self._input_names: dict[str, str] = {}
        self._channel_names: dict[str, str] = {}
        self._publisher_types: dict[str, str] = {}
        self._recorder_states: dict[str, str] = {}
        self._archive_dirty: set[str] = set()
        self._no_transfer: set[str] = set()
        self._connectivity_absent = False
        self._events_absent = False
        for key in ("password", "stream_password"):
            secret = str(config.get(key, "") or "")
            if secret:
                self.redact_in_log(secret)

    # ── Config accessors ──

    @property
    def _host(self) -> str:
        return str(self.config.get("host", "") or "").strip()

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
    def _detail_every(self) -> int:
        return max(1, _int(self.config.get("detail_poll_every", 6), 6) or 6)

    def _base_url(self) -> str:
        return f"{self._scheme}://{self._host}:{self._port}"

    def _rtsp_port_for(self, cid: str) -> int | None:
        """The channel's RTSP port: the configured table row, else the Nano's
        fixed port, else unknown."""
        rows = self.config.get("channel_rtsp_ports") or []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if str(row.get("channel", "")).strip() == str(cid):
                    port = _int(row.get("port"))
                    if port and 1 <= port <= 65535:
                        return port
        product = str(self.get_state("product_name") or "")
        if "nano" in product.lower():
            return NANO_RTSP_PORT
        return None

    def _stream_credentials(self) -> tuple[str, str]:
        return (
            str(self.config.get("stream_username", "") or ""),
            str(self.config.get("stream_password", "") or ""),
        )

    def _snapshot_url(self, path: str) -> str:
        user, password = self._stream_credentials()
        return _with_credentials(f"{self._base_url()}{API_PREFIX}{path}?format=jpg", user, password)

    def _auth_fault(self) -> ConnectionFaultError:
        if not self._password:
            message = (
                "The Pearl refused the login and no password is entered. Enter the "
                "admin account's password under Edit Device and press Reconnect "
                "(Pearl firmware 4.14.2 and later require one)."
            )
        else:
            message = (
                "The Pearl refused the login. Check the username and password under "
                "Edit Device and press Reconnect."
            )
        return ConnectionFaultError(message, code="auth_failed")

    # ── Connection lifecycle ──

    async def _create_transport(self, transport_type: str) -> None:
        host, port = self._host, self._port
        if not host:
            raise ConnectionFaultError("No IP address configured", code="invalid_config")
        if not self._username:
            raise ConnectionFaultError(
                "No username is entered. Enter a Pearl account (admin) under Edit Device.",
                code="auth_failed",
            )
        if not await self._verify_reachable(host, port):
            raise ConnectionError(f"{host}:{port} is not responding")
        self._client = httpx.AsyncClient(
            base_url=self._base_url(),
            auth=httpx.BasicAuth(self._username, self._password),
            verify=bool(self.config.get("verify_ssl", False)),
            timeout=httpx.Timeout(10.0, connect=5.0),
        )

    async def _post_connect(self) -> None:
        try:
            await self._read_firmware()
            await self._read_identity()
        except PearlError as exc:
            if exc.not_authorized:
                raise self._auth_fault() from exc
            raise ConnectionError(f"The Pearl answered with an error: {exc}") from exc
        except httpx.TransportError as exc:
            raise ConnectionError(f"{self._host} is not responding: {exc}") from exc
        log.info(
            f"[{self.device_id}] Connected to {self.get_state('product_name') or 'Pearl'} "
            f"at {self._host}:{self._port} (firmware {self.get_state('firmware_version')})"
        )

    async def _initial_sync(self) -> None:
        self._poll_count = 0
        try:
            await self._read_fast()
            await self._read_detail()
        except PearlError as exc:
            if exc.not_authorized:
                raise self._auth_fault() from exc
            raise ConnectionError(f"The Pearl answered with an error: {exc}") from exc
        except httpx.TransportError as exc:
            raise ConnectionError(f"{self._host} is not responding: {exc}") from exc

    def _link_alive(self) -> bool:
        return self._client is not None

    async def _close_session(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _liveness_probe(self) -> None:
        if self._client is None:
            raise ConnectionError("Not connected")
        await self._get("/system/firmware/version")

    # ── HTTP plumbing ──

    async def _request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None,
        json_body: Any = None, timeout: float | None = None, bare: bool = False,
    ) -> Any:
        """One API call. Returns the ``result`` of an ``ok`` answer; anything
        else is a PearlError carrying the device's own message. ``bare`` is
        for the ad-hoc session resource, whose answer is the session object
        itself rather than the ``status`` / ``result`` envelope."""
        client = self._client
        if client is None:
            raise ConnectionError("Not connected")
        kwargs: dict[str, Any] = {}
        if params:
            kwargs["params"] = params
        if json_body is not None:
            kwargs["json"] = json_body
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(timeout, connect=5.0)
        resp = await client.request(method, API_PREFIX + path, **kwargs)
        if resp.status_code in (401, 403):
            raise PearlError("The Pearl refused the login", http_status=resp.status_code)
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise PearlError(f"HTTP {resp.status_code} from {path}", http_status=resp.status_code)
        if bare and resp.status_code < 400 and "status" not in payload:
            return payload
        status = str(payload.get("status", ""))
        if status != "ok":
            message = str(payload.get("message") or status or f"HTTP {resp.status_code}")
            raise PearlError(message, status=status, http_status=resp.status_code)
        return payload.get("result")

    async def _get(self, path: str, **params: Any) -> Any:
        return await self._request("GET", path, params=params or None)

    async def _post(self, path: str, json_body: Any = None, **params: Any) -> Any:
        return await self._request("POST", path, params=params or None, json_body=json_body)

    async def _put(self, path: str, json_body: Any = None, **params: Any) -> Any:
        return await self._request("PUT", path, params=params or None, json_body=json_body)

    async def _patch(self, path: str, json_body: Any) -> Any:
        return await self._request("PATCH", path, json_body=json_body)

    async def _delete(self, path: str) -> Any:
        return await self._request("DELETE", path)

    # ── Connect-time reads ──

    async def _read_firmware(self) -> None:
        info = await self._get("/system/firmware")
        if not isinstance(info, dict):
            raise PearlError("The firmware answer is not the documented object")
        self.set_states({
            "firmware_version": str(info.get("version", "")),
            "firmware_revision": str(info.get("revision", "")),
            "product_id": _int(info.get("product_id")),
            "product_name": str(info.get("product_name", "")),
        })

    async def _read_identity(self) -> None:
        ident = await self._get("/system/ident")
        if isinstance(ident, dict):
            self.set_states({
                "device_name": str(ident.get("name", "")),
                "device_location": str(ident.get("location", "")),
                "device_description": str(ident.get("description", "")),
            })

    # ── Polling ──

    async def poll(self) -> None:
        if self._client is None:
            return
        self._poll_count += 1
        try:
            await self._read_fast()
            if self._poll_count % self._detail_every == 0:
                await self._read_detail()
        except PearlError as exc:
            if exc.not_authorized:
                raise self._auth_fault() from exc
            self.set_state("last_error", str(exc))
            log.warning(f"[{self.device_id}] Poll: {exc}")
        except httpx.TransportError as exc:
            raise ConnectionError(f"Pearl at {self._base_url()} not responding: {exc}") from exc

    async def _read_fast(self) -> None:
        await self._read_channels()
        await self._read_recorder_status()
        await self._read_system_status()
        await self._read_single_touch_states()
        await self._read_afu()

    async def _read_detail(self) -> None:
        await self._read_inputs()
        await self._read_outputs()
        await self._read_recorders()
        await self._read_storages()
        await self._read_single_touch_roster()
        await self._read_presets()
        await self._read_events()
        await self._read_adhoc_session()
        await self._read_connectivity()
        await self._read_archives()
        await self._publish_output_sources()

    # ── Channels and publishers ──

    async def _read_channels(self) -> None:
        channels = await self._get(
            "/channels", publishers="true", **{"publishers-status": "true"},
            encoders="true", active_layout="true",
        )
        if not isinstance(channels, list):
            raise PearlError("The channel list is not the documented array")
        seen_channels: set[str] = set()
        seen_publishers: set[str] = set()
        updates: list[tuple[str, str, dict[str, Any]]] = []
        total_publishers = 0
        active_publishers = 0
        for channel in channels:
            if not isinstance(channel, dict) or "id" not in channel:
                continue
            cid = str(channel["id"])
            local = _sanitize_id(cid)
            seen_channels.add(local)
            self._channel_ids[local] = cid
            name = str(channel.get("name", ""))
            self._channel_names[cid] = name
            publishers = channel.get("publishers") or []
            if not isinstance(publishers, list):
                publishers = []
            running = 0
            for pub in publishers:
                if not isinstance(pub, dict) or "id" not in pub:
                    continue
                pid = str(pub["id"])
                plocal = _sanitize_id(f"{cid}-{pid}")
                seen_publishers.add(plocal)
                self._publisher_ids[plocal] = (cid, pid)
                ptype = str(pub.get("type", ""))
                self._publisher_types[plocal] = ptype
                status = pub.get("status") if isinstance(pub.get("status"), dict) else {}
                state = str(status.get("state", "")) if status else ""
                started = bool(status.get("started", False)) if status else False
                if started:
                    running += 1
                pstate: dict[str, Any] = {
                    "name": str(pub.get("name", "")),
                    "channel_id": cid,
                    "publisher_id": pid,
                    "label": str(pub.get("name", "")),
                }
                if ptype in PUBLISHER_TYPES:
                    pstate["type"] = ptype
                if status:
                    pstate["started"] = started
                    pstate["is_configured"] = bool(status.get("is_configured", False))
                    if state in PUBLISHER_STATES:
                        pstate["state"] = state
                    pstate["state_detail"] = str(status.get("description", "") or "")
                    warnings = status.get("warnings") or []
                    pstate["warnings"] = "; ".join(
                        str(w.get("text", "")) for w in warnings if isinstance(w, dict)
                    )
                    pstate["duration_s"] = _int(status.get("duration"), 0)
                    pstate["since"] = _int(status.get("since"), 0)
                    pstate["reconnections"] = _int(status.get("reconnections"), 0)
                    stats = status.get("statistics")
                    current = stats.get("current") if isinstance(stats, dict) else None
                    if isinstance(current, dict):
                        for key in ("send_rate", "rtt", "loss_ratio", "estimated_bandwidth"):
                            if key in current:
                                pstate[key] = _float(current.get(key), 0.0)
                if not self.is_child_registered("publisher", plocal):
                    self.register_child("publisher", plocal, initial_state=pstate)
                else:
                    updates.append(("publisher", plocal, pstate))
            total_publishers += len(publishers)
            active_publishers += running
            cstate: dict[str, Any] = {
                "name": name,
                "label": name,
                "publisher_count": len(publishers),
                "publishers_active": running,
                "streaming": running > 0,
            }
            for encoder in channel.get("encoders") or []:
                if not isinstance(encoder, dict):
                    continue
                if encoder.get("type") == "video":
                    cstate["video_codec"] = str(encoder.get("name", ""))
                    cstate["video_resolution"] = str(encoder.get("resolution", ""))
                    cstate["video_framerate"] = _float(encoder.get("framerate"), 0.0)
                    cstate["video_bitrate_kbps"] = _int(encoder.get("bitrate"), 0)
                elif encoder.get("type") == "audio":
                    cstate["audio_codec"] = str(encoder.get("name", ""))
                    cstate["audio_channels"] = _int(encoder.get("channels"), 0)
                    cstate["audio_bitrate_kbps"] = _int(encoder.get("bitrate"), 0)
            layout = channel.get("active_layout")
            if isinstance(layout, dict):
                cstate["active_layout_id"] = str(layout.get("id", ""))
                cstate["active_layout_name"] = str(layout.get("name", ""))
                sources = layout.get("sources")
                names: list[str] = []
                if isinstance(sources, dict):
                    for kind in ("video", "audio"):
                        for src in sources.get(kind) or []:
                            if isinstance(src, dict) and src.get("name"):
                                names.append(str(src["name"]))
                cstate["active_layout_sources"] = ", ".join(names)
            cstate.update(self._preview_state(cid))
            if not self.is_child_registered("channel", local):
                self.register_child("channel", local, initial_state=cstate)
            else:
                updates.append(("channel", local, cstate))
        for stale in set(self.list_children("channel")) - seen_channels:
            self.deregister_child("channel", stale)
            self._channel_ids.pop(stale, None)
        for stale in set(self.list_children("publisher")) - seen_publishers:
            self.deregister_child("publisher", stale)
            self._publisher_ids.pop(stale, None)
            self._publisher_types.pop(stale, None)
        if updates:
            self.set_children_state_batch(updates)
        self.set_states({
            "channel_count": len(seen_channels),
            "publisher_count": total_publishers,
            "publishers_active": active_publishers,
            "streaming": active_publishers > 0,
        })

    def _preview_state(self, cid: str) -> dict[str, Any]:
        port = self._rtsp_port_for(cid)
        snapshot = self._snapshot_url(f"/channels/{quote(cid, safe='')}/preview")
        if port is None:
            return {
                "preview_url": "",
                "preview_format": "",
                "preview_status": "needs_setup",
                "preview_setup_field": "channel_rtsp_ports",
                "preview_status_detail": (
                    f"Enter channel {cid}'s RTSP port under Channel RTSP Ports. It is on the "
                    f"channel's Status page in the Pearl Admin panel (rtsp://...:<port>/stream.sdp)."
                ),
                "snapshot_url": snapshot,
            }
        user, password = self._stream_credentials()
        return {
            "preview_url": _with_credentials(f"rtsp://{self._host}:{port}/stream.sdp", user, password),
            "preview_format": "rtsp",
            "preview_status": "",
            "preview_setup_field": "",
            "preview_status_detail": "",
            "snapshot_url": snapshot,
        }

    async def _read_publisher_settings(self, plocal: str) -> None:
        """The documented settings object of one publisher (the channel
        list's ``settings`` is shown in a different, undocumented shape)."""
        cid, pid = self._publisher_ids[plocal]
        settings = await self._get(
            f"/channels/{quote(cid, safe='')}/publishers/{quote(pid, safe='')}/settings"
        )
        if not isinstance(settings, dict):
            return
        common = settings.get("common") if isinstance(settings.get("common"), dict) else {}
        ptype = str(settings.get("type", "") or self._publisher_types.get(plocal, ""))
        block = settings.get(ptype.replace("-", "_")) if ptype else None
        block = block if isinstance(block, dict) else {}
        update: dict[str, Any] = {
            "enabled": bool(common.get("enabled", False)),
            "single_touch": bool(common.get("single_touch", False)),
            "audio_disabled": bool(block.get("disable_audio", False)),
            "url": str(block.get("url", "") or ""),
        }
        if ptype == "srt":
            update["srt_mode"] = str(block.get("mode", ""))
            update["srt_port"] = _int(block.get("port"), 0)
            update["srt_latency_ms"] = _int(block.get("latency"), 0)
        if ptype == "ndi":
            update["ndi_name"] = str(block.get("ndi_name", ""))
        self.set_child_state_batch("publisher", plocal, update)

    async def _read_all_publisher_settings(self) -> None:
        for plocal in list(self.list_children("publisher")):
            if plocal not in self._publisher_ids:
                continue
            try:
                await self._read_publisher_settings(plocal)
            except PearlError as exc:
                if exc.not_authorized:
                    raise
                log.warning(f"[{self.device_id}] Stream {plocal} settings: {exc}")

    # ── Recorders ──

    async def _read_recorder_status(self) -> None:
        recorders = await self._get("/recorders/status")
        if not isinstance(recorders, list):
            raise PearlError("The recorder status is not the documented array")
        seen: set[str] = set()
        updates: list[tuple[str, str, dict[str, Any]]] = []
        running = 0
        for rec in recorders:
            if not isinstance(rec, dict) or "id" not in rec:
                continue
            rid = str(rec["id"])
            local = _sanitize_id(rid)
            seen.add(local)
            self._recorder_ids[local] = rid
            name = str(rec.get("name", ""))
            status = rec.get("status") if isinstance(rec.get("status"), dict) else {}
            state = str(status.get("state", "")) if status else ""
            rstate: dict[str, Any] = {"name": name, "label": name}
            if status:
                if state in RECORDER_STATES:
                    rstate["state"] = state
                rstate["recording"] = state == "started"
                rstate["state_detail"] = str(status.get("description", "") or "")
                rstate["duration_s"] = _int(status.get("duration"), 0)
                rstate["active"] = str(status.get("active", "") or "")
                rstate["total"] = str(status.get("total", "") or "")
                if state == "started":
                    running += 1
                previous = self._recorder_states.get(local)
                if previous == "started" and state != "started":
                    self._archive_dirty.add(local)
                self._recorder_states[local] = state
            if not self.is_child_registered("recorder", local):
                self.register_child("recorder", local, initial_state=rstate)
                self._archive_dirty.add(local)
            else:
                updates.append(("recorder", local, rstate))
        for stale in set(self.list_children("recorder")) - seen:
            self.deregister_child("recorder", stale)
            self._recorder_ids.pop(stale, None)
            self._recorder_states.pop(stale, None)
        if updates:
            self.set_children_state_batch(updates)
        self.set_states({
            "recorder_count": len(seen),
            "recorders_active": running,
            "recording": running > 0,
        })

    async def _read_recorders(self) -> None:
        recorders = await self._get("/recorders")
        if not isinstance(recorders, list):
            return
        updates: list[tuple[str, str, dict[str, Any]]] = []
        for rec in recorders:
            if not isinstance(rec, dict) or "id" not in rec:
                continue
            local = _sanitize_id(str(rec["id"]))
            if self.is_child_registered("recorder", local):
                updates.append(("recorder", local, {"multisource": bool(rec.get("multisource", False))}))
        if updates:
            self.set_children_state_batch(updates)

    async def _read_archives(self) -> None:
        """The newest file of each recorder whose archive may have changed:
        on connect, after a recording stops, and on every tenth detail cycle
        (the file list is not documented as ordered, so it is sorted here)."""
        if self._poll_count and (self._poll_count // self._detail_every) % 10 == 0:
            self._archive_dirty.update(self.list_children("recorder"))
        for local in list(self._archive_dirty):
            rid = self._recorder_ids.get(local)
            if rid is None or not self.is_child_registered("recorder", local):
                self._archive_dirty.discard(local)
                continue
            try:
                files = await self._get(f"/recorders/{quote(rid, safe='')}/archive/files")
            except PearlError as exc:
                if exc.not_authorized:
                    raise
                log.warning(f"[{self.device_id}] Recorder {rid} archive: {exc}")
                self._archive_dirty.discard(local)
                continue
            self._archive_dirty.discard(local)
            if not isinstance(files, list):
                continue
            entries = [f for f in files if isinstance(f, dict)]
            if not entries:
                self.set_child_state_batch("recorder", local, {
                    "latest_recording_name": "",
                    "latest_recording_created": "",
                    "latest_recording_duration_s": 0,
                    "latest_recording_size_bytes": 0,
                    "latest_recording_in_progress": False,
                })
                continue
            latest = max(entries, key=lambda f: str(f.get("created", "")))
            self.set_child_state_batch("recorder", local, {
                "latest_recording_name": str(latest.get("name", "")),
                "latest_recording_created": str(latest.get("created", "")),
                "latest_recording_duration_s": _int(latest.get("duration"), 0),
                "latest_recording_size_bytes": _int(latest.get("size"), 0),
                "latest_recording_in_progress": bool(latest.get("recording", False)),
            })

    # ── System ──

    async def _read_system_status(self) -> None:
        status = await self._get("/system/status")
        if not isinstance(status, dict):
            return
        temp = _float(status.get("cputemp"))
        limit = _float(status.get("cputemp_threshold"))
        updates: dict[str, Any] = {
            "system_time": str(status.get("date", "")),
            "uptime_seconds": _int(status.get("uptime"), 0),
            "cpu_load_percent": _int(status.get("cpuload"), 0),
            "cpu_load_high": bool(status.get("cpuload_high", False)),
        }
        if temp is not None:
            updates["cpu_temp_c"] = temp
        if limit is not None:
            updates["cpu_temp_threshold_c"] = limit
        if temp is not None and limit is not None:
            updates["cpu_temp_high"] = temp >= limit
        self.set_states(updates)

    async def _read_connectivity(self) -> None:
        if self._connectivity_absent:
            return
        try:
            details = await self._get("/system/connectivity/details")
        except PearlError as exc:
            if exc.not_authorized:
                raise
            if exc.not_found:
                self._connectivity_absent = True
                return
            raise
        if not isinstance(details, dict):
            return
        updates: dict[str, Any] = {
            "external_ip": str(details.get("external_ip", "") or ""),
            "mdns_name": str(details.get("mdns", "") or ""),
        }
        for key in CONNECTIVITY_KEYS:
            value = str(details.get(key, "") or "")
            if value in CONNECTIVITY_STATES:
                updates[f"{key}_status"] = value
        self.set_states(updates)

    async def _read_presets(self) -> None:
        presets = await self._get("/system/presets", description="true", readonly="true")
        names: list[str] = []
        if isinstance(presets, list):
            for preset in presets:
                if isinstance(preset, dict) and preset.get("name"):
                    names.append(str(preset["name"]))
        self.set_state("preset_options", json.dumps(names))

    # ── Inputs ──

    async def _read_inputs(self) -> None:
        inputs = await self._get("/inputs")
        if not isinstance(inputs, list):
            raise PearlError("The input list is not the documented array")
        seen: set[str] = set()
        for entry in inputs:
            if not isinstance(entry, dict) or "id" not in entry:
                continue
            sid = str(entry["id"])
            local = _sanitize_id(sid)
            if local in seen:
                log.warning(f"[{self.device_id}] Two inputs map to id {local!r}; keeping the first")
                continue
            seen.add(local)
            self._input_ids[local] = sid
            name = str(entry.get("name", ""))
            self._input_names[sid] = name
            itype = str(entry.get("type", ""))
            base: dict[str, Any] = {
                "name": name,
                "label": name,
                "real_device_name": str(entry.get("real_device_name", "") or ""),
                "has_audio": bool(entry.get("audio", False)),
                "has_video": bool(entry.get("video", False)),
                "snapshot_url": self._snapshot_url(f"/inputs/{quote(sid, safe='')}/preview"),
            }
            if itype in INPUT_TYPES:
                base["type"] = itype
            settings: dict[str, Any] | None
            try:
                doc = await self._get(f"/inputs/{quote(sid, safe='')}/settings")
                settings = doc if isinstance(doc, dict) else {}
            except PearlError as exc:
                if exc.not_authorized:
                    raise
                # 405 "Input settings are not supported" is the documented
                # answer for HDMI on most models, USB, NDI and web graphics.
                settings = None
            schema, paths = input_setting_schema(settings or {})
            # A dynamic child's schema replaces the type's, so the properties
            # every input has ride along with the ones this input reported.
            schema = {**self.DRIVER_INFO["child_entity_types"]["input"]["state_variables"], **schema}
            values: dict[str, Any] = {}
            flat = _flatten(settings or {})
            for prop, path in paths.items():
                raw = flat.get(path)
                var_def = schema[prop]
                try:
                    values[prop] = coerce_setting_value(var_def, raw) if raw is not None else None
                except ValueError:
                    values[prop] = None
            base["settings_supported"] = settings is not None
            if self.is_child_registered("input", local) and self._input_schema.get(local) != schema:
                self.deregister_child("input", local)
            if not self.is_child_registered("input", local):
                self.register_child("input", local, initial_state={**base, **values}, schema=schema)
            else:
                self.set_child_state_batch("input", local, {**base, **values})
            self._input_schema[local] = schema
            self._input_paths[local] = paths
        for stale in set(self.list_children("input")) - seen:
            self.deregister_child("input", stale)
            self._input_ids.pop(stale, None)
            self._input_schema.pop(stale, None)
            self._input_paths.pop(stale, None)
        self.set_state("input_count", len(seen))

    async def _refresh_input(self, local: str) -> None:
        """Read one input's settings back after a write."""
        sid = self._input_ids[local]
        doc = await self._get(f"/inputs/{quote(sid, safe='')}/settings")
        if not isinstance(doc, dict):
            return
        flat = _flatten(doc)
        schema = self._input_schema.get(local, {})
        values: dict[str, Any] = {}
        for prop, path in self._input_paths.get(local, {}).items():
            raw = flat.get(path)
            if raw is None or prop not in schema:
                continue
            try:
                values[prop] = coerce_setting_value(schema[prop], raw)
            except ValueError:
                continue
        if values:
            self.set_child_state_batch("input", local, values)

    async def _write_input_setting(self, local: str, path: str, value: Any) -> None:
        sid = self._input_ids[local]
        await self._patch(f"/inputs/{quote(sid, safe='')}/settings", _nest(path, value))
        await self._refresh_input(local)

    def _input_path(self, local: str, candidates: tuple[str, ...], what: str) -> str:
        """The first of ``candidates`` this input reported, else a refusal
        that names the input."""
        paths = set(self._input_paths.get(local, {}).values())
        for path in candidates:
            if path in paths:
                return path
        name = self._input_names.get(self._input_ids.get(local, ""), local)
        raise ValueError(f"Input {name!r} has no {what} setting.")

    # ── Outputs ──

    async def _read_outputs(self) -> None:
        outputs = await self._get("/outputs")
        if not isinstance(outputs, list):
            return
        seen: set[str] = set()
        updates: list[tuple[str, str, dict[str, Any]]] = []
        for entry in outputs:
            if not isinstance(entry, dict) or "id" not in entry:
                continue
            did = str(entry["id"])
            local = _sanitize_id(did)
            seen.add(local)
            self._output_ids[local] = did
            name = str(entry.get("name", ""))
            ostate = {
                "name": name,
                "label": name,
                "snapshot_url": self._snapshot_url(f"/outputs/{quote(did, safe='')}/preview"),
            }
            if not self.is_child_registered("output", local):
                self.register_child("output", local, initial_state=ostate)
            else:
                updates.append(("output", local, ostate))
        for stale in set(self.list_children("output")) - seen:
            self.deregister_child("output", stale)
            self._output_ids.pop(stale, None)
        if updates:
            self.set_children_state_batch(updates)

    async def _publish_output_sources(self) -> None:
        options: list[dict[str, str]] = []
        for local in self.list_children("channel"):
            cid = self._channel_ids.get(local)
            if cid is None:
                continue
            options.append({"value": cid, "label": f"Channel {cid}: {self._channel_names.get(cid, '')}".rstrip(": ")})
        for local in self.list_children("input"):
            sid = self._input_ids.get(local)
            if sid is None:
                continue
            options.append({"value": sid, "label": f"Input: {self._input_names.get(sid, sid)}"})
        for value, label in OUTPUT_KEYWORD_SOURCES:
            options.append({"value": value, "label": label})
        self.set_state("output_source_options", json.dumps(options))

    # ── Storage and uploads ──

    async def _read_storages(self) -> None:
        storages = await self._get("/system/storages")
        if not isinstance(storages, list):
            return
        seen: set[str] = set()
        for entry in storages:
            if not isinstance(entry, dict) or "id" not in entry:
                continue
            stid = str(entry["id"])
            local = _sanitize_id(stid)
            seen.add(local)
            self._storage_ids[local] = stid
            sstate: dict[str, Any] = {"label": stid.capitalize()}
            status = await self._get(f"/system/storages/{quote(stid, safe='')}/status")
            if isinstance(status, dict):
                state = str(status.get("state", ""))
                if state in STORAGE_STATES:
                    sstate["state"] = state
                total = _int(status.get("total"))
                free = _int(status.get("free"))
                if total is not None:
                    sstate["total_bytes"] = total
                if free is not None:
                    sstate["free_bytes"] = free
                if total and free is not None:
                    sstate["free_percent"] = max(0, min(100, round(free * 100 / total)))
            if local not in self._no_transfer:
                try:
                    transfer = await self._get(f"/system/storages/{quote(stid, safe='')}/transfer/status")
                except PearlError as exc:
                    if exc.not_authorized:
                        raise
                    transfer = None
                    if exc.http_status == 405:
                        self._no_transfer.add(local)
                if isinstance(transfer, dict):
                    tstate = str(transfer.get("state", ""))
                    if tstate in TRANSFER_STATES:
                        sstate["transfer_state"] = tstate
                    session = transfer.get("session") if isinstance(transfer.get("session"), dict) else {}
                    total_files = session.get("total") if isinstance(session.get("total"), dict) else {}
                    done_files = session.get("processed") if isinstance(session.get("processed"), dict) else {}
                    sstate["transfer_total_count"] = _int(total_files.get("count"), 0)
                    sstate["transfer_processed_count"] = _int(done_files.get("count"), 0)
            if not self.is_child_registered("storage", local):
                self.register_child("storage", local, initial_state=sstate)
            else:
                self.set_child_state_batch("storage", local, sstate)
        for stale in set(self.list_children("storage")) - seen:
            self.deregister_child("storage", stale)
            self._storage_ids.pop(stale, None)

    async def _read_afu(self) -> None:
        statuses = await self._get("/afu/status")
        if not isinstance(statuses, list):
            return
        seen: set[str] = set()
        for entry in statuses:
            if not isinstance(entry, dict) or "id" not in entry:
                continue
            aid = str(entry["id"])
            local = _sanitize_id(aid)
            seen.add(local)
            self._afu_ids[local] = aid
            status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
            astate: dict[str, Any] = {"label": f"Upload {aid}"}
            state = str(status.get("state", ""))
            if state in AFU_STATES:
                astate["state"] = state
            astate["protocol"] = str(status.get("protocol", "") or "")
            queue = status.get("queue") if isinstance(status.get("queue"), dict) else {}
            astate["queue_files"] = _int(queue.get("files"), 0)
            astate["queue_size_bytes"] = _int(queue.get("size"), 0)
            current = status.get("file") if isinstance(status.get("file"), dict) else {}
            astate["uploading_file"] = str(current.get("id", "") or "")
            astate["uploaded_bytes"] = _int(current.get("uploaded"), 0)
            astate["file_size_bytes"] = _int(current.get("size"), 0)
            error = status.get("error") if isinstance(status.get("error"), dict) else {}
            astate["error_message"] = str(error.get("message", "") or "")
            if not self.is_child_registered("afu", local):
                self.register_child("afu", local, initial_state=astate)
            else:
                self.set_child_state_batch("afu", local, astate)
        for stale in set(self.list_children("afu")) - seen:
            self.deregister_child("afu", stale)
            self._afu_ids.pop(stale, None)

    # ── One-touch control ──

    async def _read_single_touch_roster(self) -> None:
        controls = await self._get("/system/singletouchcontrol")
        if not isinstance(controls, list):
            return
        seen: set[str] = set()
        for entry in controls:
            if not isinstance(entry, dict) or "id" not in entry:
                continue
            stcid = str(entry["id"])
            local = _sanitize_id(stcid)
            seen.add(local)
            self._single_touch_ids[local] = stcid
            if not self.is_child_registered("single_touch", local):
                self.register_child("single_touch", local, initial_state={"label": f"One-Touch {stcid}"})
        for stale in set(self.list_children("single_touch")) - seen:
            self.deregister_child("single_touch", stale)
            self._single_touch_ids.pop(stale, None)
        await self._read_single_touch_states()

    async def _read_single_touch_states(self) -> None:
        if not self._single_touch_ids:
            await self._read_single_touch_roster()
            if not self._single_touch_ids:
                return
        for local, stcid in list(self._single_touch_ids.items()):
            if not self.is_child_registered("single_touch", local):
                continue
            state = await self._get(f"/system/singletouchcontrol/{quote(stcid, safe='')}/state")
            if not isinstance(state, dict):
                continue
            recorders = state.get("recorders") if isinstance(state.get("recorders"), dict) else {}
            publishers = state.get("publishers") if isinstance(state.get("publishers"), dict) else {}
            self.set_child_state_batch("single_touch", local, {
                "pressed": bool(state.get("pressed", False)),
                "status": bool(state.get("status", False)),
                "recorders_total": _int(recorders.get("total"), 0),
                "recorders_active": _int(recorders.get("active"), 0),
                "recorders_success": _int(recorders.get("success"), 0),
                "publishers_total": _int(publishers.get("total"), 0),
                "publishers_active": _int(publishers.get("active"), 0),
                "publishers_success": _int(publishers.get("success"), 0),
            })

    # ── CMS events ──

    async def _read_event_alias(self, alias: str) -> dict[str, Any] | None:
        try:
            event = await self._get(f"/schedule/events/{alias}")
        except PearlError as exc:
            if exc.not_authorized:
                raise
            return None
        return event if isinstance(event, dict) else None

    async def _read_events(self) -> None:
        if self._events_absent:
            return
        try:
            ongoing = await self._read_event_alias("ongoing")
            upcoming = await self._read_event_alias("upcoming")
        except PearlError:
            raise
        updates: dict[str, Any] = {}
        if ongoing:
            updates.update({
                "event_ongoing_id": str(ongoing.get("id", "")),
                "event_ongoing_title": str(ongoing.get("title", "")),
                "event_ongoing_start": _int(ongoing.get("start"), 0),
                "event_ongoing_finish": _int(ongoing.get("finish"), 0),
            })
            status = str(ongoing.get("status", ""))
            if status in EVENT_STATUSES:
                updates["event_ongoing_status"] = status
        else:
            updates.update({
                "event_ongoing_id": "", "event_ongoing_title": "",
                "event_ongoing_start": 0, "event_ongoing_finish": 0,
            })
            if self.get_state("event_ongoing_status") is not None:
                updates["event_ongoing_status"] = "finished"
        if upcoming:
            updates.update({
                "event_upcoming_id": str(upcoming.get("id", "")),
                "event_upcoming_title": str(upcoming.get("title", "")),
                "event_upcoming_start": _int(upcoming.get("start"), 0),
                "event_upcoming_finish": _int(upcoming.get("finish"), 0),
            })
        else:
            updates.update({
                "event_upcoming_id": "", "event_upcoming_title": "",
                "event_upcoming_start": 0, "event_upcoming_finish": 0,
            })
        self.set_states(updates)

    async def _read_adhoc_session(self) -> None:
        try:
            session = await self._request("GET", "/schedule/events/adhoc/session", bare=True)
        except PearlError as exc:
            if exc.not_authorized:
                raise
            session = None
        if isinstance(session, dict) and session.get("id"):
            self.set_states({
                "adhoc_user_id": str(session.get("id", "")),
                "adhoc_user_name": str(session.get("name", "")),
                "adhoc_session_expires": _int(session.get("expired"), 0),
            })
        else:
            self.set_states({"adhoc_user_id": "", "adhoc_user_name": "", "adhoc_session_expires": 0})

    # ── Refresh from Device ──

    async def refresh_children(self) -> Any:
        try:
            await self._read_fast()
            await self._read_detail()
            await self._read_all_publisher_settings()
        except PearlError as exc:
            if exc.not_authorized:
                raise self._auth_fault() from exc
            raise
        except httpx.TransportError as exc:
            raise ConnectionError(f"{self._host} is not responding: {exc}") from exc
        return {
            "channels": len(self.list_children("channel")),
            "publishers": len(self.list_children("publisher")),
            "recorders": len(self.list_children("recorder")),
            "inputs": len(self.list_children("input")),
            "outputs": len(self.list_children("output")),
            "storages": len(self.list_children("storage")),
        }

    # ── Commands ──

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        handler = self._DISPATCH.get(command)
        if handler is None:
            raise ValueError(f"Unknown command: {command}")
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        try:
            return await handler(self, params)
        except PearlError as exc:
            if exc.not_authorized:
                raise self._auth_fault() from exc
            self.set_state("last_error", str(exc))
            raise ValueError(f"The Pearl refused {command}: {exc}") from exc
        except httpx.TransportError as exc:
            raise ConnectionError(f"{self._host} is not responding: {exc}") from exc

    # Child lookups

    def _cid(self, params: dict[str, Any]) -> str:
        local = str(params.get("channel", ""))
        if local not in self._channel_ids:
            raise ValueError(f"Unknown channel {local!r}")
        return self._channel_ids[local]

    def _rid(self, params: dict[str, Any]) -> str:
        local = str(params.get("recorder", ""))
        if local not in self._recorder_ids:
            raise ValueError(f"Unknown recorder {local!r}")
        return self._recorder_ids[local]

    def _pub(self, params: dict[str, Any]) -> tuple[str, str, str]:
        local = str(params.get("publisher", ""))
        if local not in self._publisher_ids:
            raise ValueError(f"Unknown stream {local!r}")
        cid, pid = self._publisher_ids[local]
        return local, cid, pid

    def _input_local(self, params: dict[str, Any]) -> str:
        local = str(params.get("input", ""))
        if local not in self._input_ids:
            raise ValueError(f"Unknown input {local!r}")
        return local

    @staticmethod
    def _pub_path(cid: str, pid: str, tail: str = "") -> str:
        return f"/channels/{quote(cid, safe='')}/publishers/{quote(pid, safe='')}{tail}"

    # Recording

    async def _cmd_start_all_recorders(self, params: dict[str, Any]) -> None:
        await self._post("/recorders/control/start")

    async def _cmd_stop_all_recorders(self, params: dict[str, Any]) -> None:
        await self._post("/recorders/control/stop")

    async def _cmd_start_recorder(self, params: dict[str, Any]) -> None:
        await self._post(f"/recorders/{quote(self._rid(params), safe='')}/control/start")

    async def _cmd_stop_recorder(self, params: dict[str, Any]) -> None:
        await self._post(f"/recorders/{quote(self._rid(params), safe='')}/control/stop")

    async def _cmd_add_bookmark(self, params: dict[str, Any]) -> None:
        text = str(params.get("text", ""))
        if not text:
            raise ValueError("A bookmark needs a name.")
        await self._post(f"/channels/{quote(self._cid(params), safe='')}/bookmarks", text=text)

    # Streaming

    async def _cmd_start_all_streams(self, params: dict[str, Any]) -> None:
        for cid in list(self._channel_ids.values()):
            await self._post(f"/channels/{quote(cid, safe='')}/publishers/control/start")

    async def _cmd_stop_all_streams(self, params: dict[str, Any]) -> None:
        for cid in list(self._channel_ids.values()):
            await self._post(f"/channels/{quote(cid, safe='')}/publishers/control/stop")

    async def _cmd_start_channel_streams(self, params: dict[str, Any]) -> None:
        await self._post(f"/channels/{quote(self._cid(params), safe='')}/publishers/control/start")

    async def _cmd_stop_channel_streams(self, params: dict[str, Any]) -> None:
        await self._post(f"/channels/{quote(self._cid(params), safe='')}/publishers/control/stop")

    async def _cmd_start_publisher(self, params: dict[str, Any]) -> None:
        _local, cid, pid = self._pub(params)
        await self._post(self._pub_path(cid, pid, "/control/start"))

    async def _cmd_stop_publisher(self, params: dict[str, Any]) -> None:
        _local, cid, pid = self._pub(params)
        await self._post(self._pub_path(cid, pid, "/control/stop"))

    async def _cmd_set_publisher_enabled(self, params: dict[str, Any]) -> None:
        local, cid, pid = self._pub(params)
        await self._patch(self._pub_path(cid, pid, "/settings"),
                          {"common": {"enabled": _bool_text(params.get("enabled"))}})
        await self._read_publisher_settings(local)

    async def _cmd_set_publisher_single_touch(self, params: dict[str, Any]) -> None:
        local, cid, pid = self._pub(params)
        await self._patch(self._pub_path(cid, pid, "/settings"),
                          {"common": {"single_touch": _bool_text(params.get("included"))}})
        await self._read_publisher_settings(local)

    async def _cmd_rename_publisher(self, params: dict[str, Any]) -> None:
        local, cid, pid = self._pub(params)
        name = str(params.get("name", "")).strip()
        if not name:
            raise ValueError("A stream needs a name.")
        await self._put(self._pub_path(cid, pid, "/name"), name=name)
        self.set_child_state_batch("publisher", local, {"name": name, "label": name})

    async def _cmd_set_publisher_url(self, params: dict[str, Any]) -> None:
        local, cid, pid = self._pub(params)
        ptype = self._publisher_types.get(local, "")
        url = str(params.get("url", "")).strip()
        if not url:
            raise ValueError("A destination URL is needed.")
        if ptype in URL_PUBLISHER_TYPES:
            body = {ptype: {"url": url}}
        elif ptype == "srt":
            mode = self.get_child_state("publisher", local).get("srt_mode") or ""
            if mode == "listener":
                raise ValueError("An SRT listener has no destination URL; it is reached on its port.")
            body = {"srt": {"url": url}}
        else:
            raise ValueError(f"A {ptype or 'stream of this'} type has no destination URL.")
        await self._patch(self._pub_path(cid, pid, "/settings"), body)
        await self._read_publisher_settings(local)

    async def _cmd_set_rtmp_stream_key(self, params: dict[str, Any]) -> None:
        local, cid, pid = self._pub(params)
        if self._publisher_types.get(local) != "rtmp":
            raise ValueError("Only an RTMP stream has a stream key.")
        key = str(params.get("stream_key", ""))
        self.redact_in_log(key)
        await self._patch(self._pub_path(cid, pid, "/settings"), {"rtmp": {"stream": key}})

    async def _cmd_delete_publisher(self, params: dict[str, Any]) -> None:
        local, cid, pid = self._pub(params)
        await self._delete(self._pub_path(cid, pid))
        self.deregister_child("publisher", local)
        self._publisher_ids.pop(local, None)
        self._publisher_types.pop(local, None)
        await self._read_channels()

    async def _add_publisher(self, cid: str, name: str, settings: dict[str, Any]) -> None:
        if not name:
            raise ValueError("A stream needs a name.")
        await self._post(f"/channels/{quote(cid, safe='')}/publishers", {"name": name, "settings": settings})
        await self._read_channels()
        await self._read_all_publisher_settings()

    async def _cmd_add_rtmp_publisher(self, params: dict[str, Any]) -> None:
        cid = self._cid(params)
        for secret in ("stream_key", "password"):
            if params.get(secret):
                self.redact_in_log(str(params[secret]))
        settings = {
            "type": "rtmp",
            "rtmp": {
                "url": str(params.get("url", "")).strip(),
                "stream": str(params.get("stream_key", "") or ""),
                "username": str(params.get("username", "") or ""),
                "password": str(params.get("password", "") or ""),
                "disable_audio": False,
            },
            "common": {"enabled": _bool_text(params.get("enabled", True)), "single_touch": True},
        }
        await self._add_publisher(cid, str(params.get("name", "")).strip(), settings)

    async def _cmd_add_srt_publisher(self, params: dict[str, Any]) -> None:
        cid = self._cid(params)
        mode = str(params.get("mode", "caller"))
        srt: dict[str, Any] = {"mode": mode, "disable_audio": False}
        latency = _int(params.get("latency_ms"))
        if latency is not None:
            srt["latency"] = latency
        passphrase = str(params.get("passphrase", "") or "")
        if passphrase:
            if not 10 <= len(passphrase) <= 79:
                raise ValueError("An SRT passphrase is 10 to 79 characters.")
            self.redact_in_log(passphrase)
            srt["encryption"] = {"passphrase": passphrase, "keylength": 128}
        else:
            srt["encryption"] = None
        if mode == "listener":
            port = _int(params.get("port"))
            if port is None:
                raise ValueError("An SRT listener needs a port (1024 to 65535).")
            srt["port"] = port
        else:
            url = str(params.get("url", "")).strip()
            if not url:
                raise ValueError(f"An SRT {mode} needs a URL (srt://host:port).")
            srt["url"] = url
        settings = {
            "type": "srt", "srt": srt,
            "common": {"enabled": _bool_text(params.get("enabled", True)), "single_touch": True},
        }
        await self._add_publisher(cid, str(params.get("name", "")).strip(), settings)

    async def _cmd_add_ndi_publisher(self, params: dict[str, Any]) -> None:
        cid = self._cid(params)
        settings = {
            "type": "ndi",
            "ndi": {
                "disable_audio": False,
                "ndi_name": str(params.get("ndi_name", "")).strip(),
                "ndi_group": str(params.get("ndi_group", "") or ""),
            },
            "common": {"enabled": _bool_text(params.get("enabled", True)), "single_touch": True},
        }
        await self._add_publisher(cid, str(params.get("name", "")).strip(), settings)

    # Channels

    async def _cmd_set_channel_layout(self, params: dict[str, Any]) -> None:
        cid = self._cid(params)
        layout = str(params.get("layout", "")).strip()
        if not layout:
            raise ValueError("A layout ID is needed.")
        await self._put(f"/channels/{quote(cid, safe='')}/layouts/active", id=layout)
        await self._read_channels()

    async def _cmd_rename_channel(self, params: dict[str, Any]) -> None:
        cid = self._cid(params)
        name = str(params.get("name", "")).strip()
        if not name:
            raise ValueError("A channel needs a name.")
        await self._put(f"/channels/{quote(cid, safe='')}/name", name=name)
        await self._read_channels()

    # Inputs

    async def _cmd_mute_input(self, params: dict[str, Any]) -> None:
        local = self._input_local(params)
        await self._write_input_setting(local, self._input_path(local, MUTE_PATHS, "mute"), True)

    async def _cmd_unmute_input(self, params: dict[str, Any]) -> None:
        local = self._input_local(params)
        await self._write_input_setting(local, self._input_path(local, MUTE_PATHS, "mute"), False)

    async def _cmd_set_input_gain(self, params: dict[str, Any]) -> None:
        local = self._input_local(params)
        gain = _int(params.get("gain"))
        if gain is None or gain < 0:
            raise ValueError("Gain is a whole number of 0 or more.")
        await self._write_input_setting(local, self._input_path(local, GAIN_PATHS, "gain"), gain)

    async def _cmd_set_input_audio_delay(self, params: dict[str, Any]) -> None:
        local = self._input_local(params)
        delay = _int(params.get("delay_ms"))
        if delay is None or not -300 <= delay <= 300:
            raise ValueError("Audio delay is -300 to 300 ms.")
        await self._write_input_setting(local, self._input_path(local, DELAY_PATHS, "audio delay"), delay)

    async def _cmd_set_input_setting(self, params: dict[str, Any]) -> None:
        local = self._input_local(params)
        setting = str(params.get("setting", "")).strip()
        paths = self._input_paths.get(local, {})
        schema = self._input_schema.get(local, {})
        if setting not in paths:
            # Accept the label a picker shows as well as the property name.
            by_label = {str(v.get("label", "")).lower(): k for k, v in schema.items()}
            setting = by_label.get(setting.lower(), setting)
        if setting not in paths:
            name = self._input_names.get(self._input_ids.get(local, ""), local)
            raise ValueError(f"Input {name!r} has no setting {params.get('setting')!r}.")
        try:
            value = coerce_setting_value(schema[setting], params.get("value"))
        except ValueError as exc:
            raise ValueError(f"{schema[setting].get('label', setting)}: {exc}") from exc
        await self._write_input_setting(local, paths[setting], value)

    async def _add_input(self, itype: str, name: str, settings: dict[str, Any]) -> None:
        if not name:
            raise ValueError("An input needs a name.")
        await self._post("/inputs", {"type": itype, "name": name, "settings": settings})
        await self._read_inputs()
        await self._publish_output_sources()

    async def _cmd_add_rtsp_input(self, params: dict[str, Any]) -> None:
        if params.get("password"):
            self.redact_in_log(str(params["password"]))
        url = str(params.get("url", "")).strip()
        if not url:
            raise ValueError("An RTSP input needs a URL.")
        settings = {
            "rtsp": {
                "url": url,
                "username": str(params.get("username", "") or ""),
                "password": str(params.get("password", "") or ""),
                "transport": str(params.get("transport", "") or "udp"),
            },
        }
        await self._add_input("rtsp", str(params.get("name", "")).strip(), settings)

    async def _cmd_add_srt_input(self, params: dict[str, Any]) -> None:
        mode = str(params.get("mode", "listener"))
        srt: dict[str, Any] = {"mode": mode}
        latency = _int(params.get("latency_ms"))
        if latency is not None:
            srt["latency"] = latency
        passphrase = str(params.get("passphrase", "") or "")
        if passphrase:
            if not 10 <= len(passphrase) <= 79:
                raise ValueError("An SRT passphrase is 10 to 79 characters.")
            self.redact_in_log(passphrase)
            srt["encryption"] = {"passphrase": passphrase, "keylength": 128}
        else:
            srt["encryption"] = None
        if mode == "listener":
            port = _int(params.get("port"))
            if port is None:
                raise ValueError("An SRT listener needs a port (1024 to 65535).")
            srt["port"] = port
        else:
            url = str(params.get("url", "")).strip()
            if not url:
                raise ValueError(f"An SRT {mode} needs a URL (srt://host:port).")
            srt["url"] = url
        await self._add_input("srt", str(params.get("name", "")).strip(), {"srt": srt})

    async def _cmd_add_ndi_input(self, params: dict[str, Any]) -> None:
        ndi_name = str(params.get("ndi_name", "")).strip()
        if not ndi_name:
            raise ValueError("An NDI input needs the source name.")
        settings = {"ndi": {"name": ndi_name, "group": str(params.get("ndi_group", "") or "")}}
        await self._add_input("ndi", str(params.get("name", "")).strip(), settings)

    async def _cmd_add_web_graphics_input(self, params: dict[str, Any]) -> None:
        url = str(params.get("url", "")).strip()
        if not url:
            raise ValueError("A web graphics input needs a URL.")
        web: dict[str, Any] = {"url": url}
        resolution = str(params.get("resolution", "") or "").strip()
        if resolution:
            web["resolution"] = resolution
        fps = _int(params.get("fps"))
        if fps is not None:
            web["fps"] = fps
        await self._add_input("web-graphics", str(params.get("name", "")).strip(), {"web_graphics": web})

    # Outputs

    async def _cmd_set_output_source(self, params: dict[str, Any]) -> None:
        local = str(params.get("output", ""))
        if local not in self._output_ids:
            raise ValueError(f"Unknown output {local!r}")
        source = str(params.get("source", "")).strip()
        if not source:
            raise ValueError("A source is needed.")
        await self._put(f"/outputs/{quote(self._output_ids[local], safe='')}/settings", source=source)

    # Storage, one-touch, presets

    async def _cmd_eject_storage(self, params: dict[str, Any]) -> None:
        local = str(params.get("storage", ""))
        if local not in self._storage_ids:
            raise ValueError(f"Unknown storage {local!r}")
        await self._post(f"/system/storages/{quote(self._storage_ids[local], safe='')}/control/eject")
        await self._read_storages()

    async def _cmd_toggle_single_touch(self, params: dict[str, Any]) -> None:
        local = str(params.get("control", ""))
        if local not in self._single_touch_ids:
            raise ValueError(f"Unknown one-touch control {local!r}")
        await self._post(f"/system/singletouchcontrol/{quote(self._single_touch_ids[local], safe='')}/control/toggle")

    async def _cmd_apply_preset(self, params: dict[str, Any]) -> Any:
        preset = str(params.get("preset", "")).strip()
        if not preset:
            raise ValueError("A preset name is needed.")
        sections = [s.strip() for s in str(params.get("sections", "") or "").split(",") if s.strip()]
        body = {"sections": sections} if sections else None
        result = await self._post(f"/system/presets/{quote(preset, safe='')}/control/apply", body)
        reboot = bool(result.get("reboot", False)) if isinstance(result, dict) else False
        if reboot:
            log.info(f"[{self.device_id}] Preset {preset!r} applied; the Pearl is rebooting")
        return {"reboot": reboot}

    # CMS events

    async def _cmd_start_upcoming_event(self, params: dict[str, Any]) -> None:
        await self._post("/schedule/events/upcoming/control/start")
        await self._read_events()

    async def _cmd_stop_ongoing_event(self, params: dict[str, Any]) -> None:
        await self._post("/schedule/events/ongoing/control/stop")
        await self._read_events()

    async def _cmd_pause_event(self, params: dict[str, Any]) -> None:
        await self._post("/schedule/events/running/control/pause")
        await self._read_events()

    async def _cmd_resume_event(self, params: dict[str, Any]) -> None:
        await self._post("/schedule/events/paused/control/resume")
        await self._read_events()

    async def _cmd_extend_event(self, params: dict[str, Any]) -> None:
        minutes = _int(params.get("minutes"))
        if minutes is None or minutes < 1:
            raise ValueError("Extend by at least one minute.")
        await self._post("/schedule/events/ongoing/control/extend", {"finish": minutes * 60})
        await self._read_events()

    async def _cmd_create_adhoc_event(self, params: dict[str, Any]) -> Any:
        cms = str(params.get("cms", ""))
        title = str(params.get("title", "")).strip()
        minutes = _int(params.get("duration_minutes"))
        if not title:
            raise ValueError("An event needs a title.")
        if minutes is None or minutes < 1:
            raise ValueError("Duration is at least one minute.")
        start = (_int(params.get("start_in_minutes"), 0) or 0) * 60
        body: dict[str, Any] = {"title": title, "duration": minutes * 60, "start": start}
        description = str(params.get("description", "") or "")
        kind = str(params.get("type", "") or "vod")
        if cms == "kaltura":
            body["type"] = kind
            if description:
                body["description"] = description
        elif cms == "panopto":
            body["type"] = "live" if kind == "live" else "vod"
        elif cms == "opencast":
            if description:
                body["description"] = description
        else:
            raise ValueError("CMS is kaltura, panopto or opencast.")
        result = await self._post("/schedule/events", body)
        await self._read_events()
        return {"id": str(result.get("id", ""))} if isinstance(result, dict) else None

    async def _cmd_adhoc_login(self, params: dict[str, Any]) -> None:
        cms = str(params.get("cms", ""))
        user = str(params.get("user_id", "")).strip()
        if not user:
            raise ValueError("A user ID is needed.")
        body: dict[str, Any] = {"id": user}
        if cms == "panopto":
            password = str(params.get("password", "") or "")
            if not password:
                raise ValueError("A Panopto login needs the password.")
            self.redact_in_log(password)
            body["password"] = password
        elif cms != "kaltura":
            raise ValueError("Ad-hoc login is for Kaltura or Panopto.")
        await self._request("POST", "/schedule/events/adhoc/session", json_body=body, bare=True)
        await self._read_adhoc_session()

    async def _cmd_adhoc_logout(self, params: dict[str, Any]) -> None:
        await self._delete("/schedule/events/adhoc/session")
        await self._read_adhoc_session()

    # System

    async def _cmd_run_speed_test(self, params: dict[str, Any]) -> Any:
        mode = str(params.get("mode", "") or "uplink")
        protocol = str(params.get("protocol", "") or "tcp")
        timeout = _int(params.get("timeout_s"), 30) or 30
        result = await self._request(
            "GET", "/system/connectivity/tools/speedtest",
            params={"mode": mode, "protocol": protocol, "timeout": timeout},
            timeout=float(timeout) + 15.0,
        )
        if isinstance(result, dict):
            udp = result.get("udp") if isinstance(result.get("udp"), dict) else {}
            self.set_states({
                "speedtest_bandwidth_bps": _int(result.get("bandwidth"), 0),
                "speedtest_mode": str(result.get("mode", mode)),
                "speedtest_protocol": str(result.get("protocol", protocol)),
                "speedtest_duration_s": _int(result.get("duration"), 0),
                "speedtest_udp_loss": _int(udp.get("loss"), 0),
            })
        return result

    async def _cmd_reboot(self, params: dict[str, Any]) -> None:
        await self._post("/system/control/reboot")
        log.info(f"[{self.device_id}] Rebooting the Pearl")

    async def _cmd_shutdown(self, params: dict[str, Any]) -> None:
        await self._post("/system/control/shutdown")
        log.info(f"[{self.device_id}] Shutting the Pearl down")

    _DISPATCH = {
        "start_all_recorders": _cmd_start_all_recorders,
        "stop_all_recorders": _cmd_stop_all_recorders,
        "start_recorder": _cmd_start_recorder,
        "stop_recorder": _cmd_stop_recorder,
        "add_bookmark": _cmd_add_bookmark,
        "start_all_streams": _cmd_start_all_streams,
        "stop_all_streams": _cmd_stop_all_streams,
        "start_channel_streams": _cmd_start_channel_streams,
        "stop_channel_streams": _cmd_stop_channel_streams,
        "start_publisher": _cmd_start_publisher,
        "stop_publisher": _cmd_stop_publisher,
        "set_publisher_enabled": _cmd_set_publisher_enabled,
        "set_publisher_single_touch": _cmd_set_publisher_single_touch,
        "rename_publisher": _cmd_rename_publisher,
        "set_publisher_url": _cmd_set_publisher_url,
        "set_rtmp_stream_key": _cmd_set_rtmp_stream_key,
        "delete_publisher": _cmd_delete_publisher,
        "add_rtmp_publisher": _cmd_add_rtmp_publisher,
        "add_srt_publisher": _cmd_add_srt_publisher,
        "add_ndi_publisher": _cmd_add_ndi_publisher,
        "set_channel_layout": _cmd_set_channel_layout,
        "rename_channel": _cmd_rename_channel,
        "mute_input": _cmd_mute_input,
        "unmute_input": _cmd_unmute_input,
        "set_input_gain": _cmd_set_input_gain,
        "set_input_audio_delay": _cmd_set_input_audio_delay,
        "set_input_setting": _cmd_set_input_setting,
        "add_rtsp_input": _cmd_add_rtsp_input,
        "add_srt_input": _cmd_add_srt_input,
        "add_ndi_input": _cmd_add_ndi_input,
        "add_web_graphics_input": _cmd_add_web_graphics_input,
        "set_output_source": _cmd_set_output_source,
        "eject_storage": _cmd_eject_storage,
        "toggle_single_touch": _cmd_toggle_single_touch,
        "apply_preset": _cmd_apply_preset,
        "start_upcoming_event": _cmd_start_upcoming_event,
        "stop_ongoing_event": _cmd_stop_ongoing_event,
        "pause_event": _cmd_pause_event,
        "resume_event": _cmd_resume_event,
        "extend_event": _cmd_extend_event,
        "create_adhoc_event": _cmd_create_adhoc_event,
        "adhoc_login": _cmd_adhoc_login,
        "adhoc_logout": _cmd_adhoc_logout,
        "run_speed_test": _cmd_run_speed_test,
        "reboot": _cmd_reboot,
        "shutdown": _cmd_shutdown,
    }
