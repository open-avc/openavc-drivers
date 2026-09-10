"""
OpenAVC BrightSign Player Driver (Local DWS API + presentation UDP).

Controls and monitors BrightSign digital signage players (Series 3 through
Series 6: LS, HD, XD, XT, XC, AU and HS models, and the Moka displays with a
built-in player) through the player's Local Diagnostic Web Server (DWS) REST
API, and drives the presentation running on the player with UDP messages,
the same path a BrightSign Author presentation exposes to Crestron, AMX and
the bsn.Mobile app.

What it does
------------
Identity and health (serial, model, firmware, uptime, IP, PoE and power
source, the player's clock), every HDMI output as a child entity (a display
connected, the display powered, the active and configured video modes, the
audio format, HDMI power-save), reboot, factory reset, a snapshot of what is
playing, a CEC payload out of HDMI 1, the supervisor log level, the registry
(read, write, delete, flush), the player's network self-diagnostics, and on a
Moka display the panel itself (power, volume, brightness, contrast, standby
timeout, video output, white balance, always-on). Presentation control is a
UDP message to the port the presentation listens on: any string a UDP Input
event matches, or ``<variable>:<value>`` to set a presentation User Variable.

Why Python
----------
The HDMI output roster is enumerated from the player (one port on
most models, two on the XC2055 and XT2145, four on the XC4055), the Moka
display endpoints exist on one product family and are probed for, a white
balance write is a read-modify-write of three values, and every error the
player reports comes back as a sentence in the reply body that should reach
the person who pressed the button. None of that fits the declarative
request/response model, so this driver owns an ``httpx`` session.

Push vs poll
------------
Poll only. The Local DWS API documents no subscription, event stream or
webhook, so state is read on ``poll_interval`` (default 10 s): the health
check and every HDMI output on every poll; player information, the clock,
the video mode, the log level, the DWS state and the Moka display on a
slower cadence (``detail_poll_every`` polls).

Authentication and transport
----------------------------
HTTP Digest as user ``admin`` (BrightSign's fixed username). The default
password is the player's serial number; a setup file or a script can set
another one, and a player can be left open with no password at all, so a
blank password is tried rather than refused. A rejected login is a typed
``auth_failed`` fault so the platform waits for new credentials. BrightSignOS
9.0.218 / 9.1.52 and later serve the API over HTTPS with a self-signed
certificate and redirect HTTP, so the driver defaults to HTTPS on 443 with
verification off; older players on plain HTTP set Use HTTPS off and port 80.
Local DWS access is off by default from BrightSignOS 9.0.218 / 9.1.75 and is
enabled from a BrightSign Author setup file (Enable Local Diagnostic Web
Server), which is also where the password is set.

Sources (all public, from BrightSign):
  Local DWS APIs (index, authentication, HTTPS)
      https://docs.brightsign.biz/develop/local-dws-apis
  LDWS endpoint pages (general, info, control, video, display control,
  registry, advanced, diagnostics, snapshot, sendCecX)
      https://docs.brightsign.biz/develop/ldws-general-endpoints  (and siblings)
  DWS: Local Access (how local access is enabled and the password set)
      https://docs.brightsign.biz/manage/dws-local-access
  UDP Event and Presentation Settings (the presentation's UDP receiver)
      https://docs.brightsign.biz/author/udp-event
      https://docs.brightsign.biz/author/presentation-settings
  Model & Series Reference
      https://docs.brightsign.biz/hardware/model-and-series-reference
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.utils.logger import get_logger

log = get_logger(__name__)

API = "/api/v1"

# The most HDMI outputs any documented player has (XC4055: devices 0-3).
MAX_OUTPUTS = 4

# Supervisor log levels, PUT /system/supervisor/logging body values.
LOG_LEVELS = {"error": 0, "warn": 1, "info": 2, "trace": 3}
LOG_LEVEL_NAMES = {v: k for k, v in LOG_LEVELS.items()}

_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}(:\d{2})?$")


class DwsError(Exception):
    """The player answered, and the answer was an error. ``message`` is the
    player's own sentence when it gave one."""

    def __init__(self, message: str, *, http_status: int = 0) -> None:
        super().__init__(message)
        self.http_status = http_status

    @property
    def not_authorized(self) -> bool:
        return self.http_status in (401, 403)


# ── Reply parsing (pure; exercised directly by the tests) ───────────────────


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any) -> str:
    return "" if value is None else str(value)


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1", "on")
    return bool(value)


def unwrap_result(payload: Any, *, http_status: int = 200) -> Any:
    """Return the ``data.result`` of a Local DWS reply, or raise ``DwsError``
    with the player's message for an error body or a failing HTTP status."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = _as_str(error.get("message")) or f"HTTP {error.get('status', http_status)}"
            raise DwsError(message, http_status=_as_int(error.get("status"), http_status))
        if "result" in data:
            result = data["result"]
            if isinstance(result, dict) and result.get("success") is False:
                message = _as_str(result.get("error") or result.get("message")) or "The player refused the request."
                raise DwsError(message, http_status=http_status)
            return result
    if http_status >= 400:
        text = payload if isinstance(payload, str) else ""
        raise DwsError(text.strip()[:200] or f"HTTP {http_status}", http_status=http_status)
    # Some routes answer with the bare value.
    return payload


def _inner(obj: Any, key: str) -> Any:
    """``info`` nests sub-results as ``{"key": {"result": {...}}}``."""
    value = obj.get(key) if isinstance(obj, dict) else None
    if isinstance(value, dict) and "result" in value:
        return value["result"]
    return value


def parse_info(result: dict[str, Any]) -> dict[str, Any]:
    """GET /info -> device state."""
    out: dict[str, Any] = {
        "serial": _as_str(result.get("serial")),
        "model": _as_str(result.get("model")),
        "family": _as_str(result.get("family")),
        # The reference text says fwVersion; the example says FWVersion.
        "firmware_version": _as_str(result.get("fwVersion") or result.get("FWVersion")),
        "boot_version": _as_str(result.get("bootVersion")),
        "uptime": _as_str(result.get("upTime")),
        "uptime_seconds": _as_int(result.get("upTimeSeconds")),
        "connection_type": _as_str(result.get("connectionType")),
    }
    networking = _inner(result, "networking") or {}
    if isinstance(networking, dict):
        out["device_name"] = _as_str(networking.get("name"))
        out["device_description"] = _as_str(networking.get("description"))
    power = _inner(result, "power") or {}
    if isinstance(power, dict):
        out["power_source"] = _as_str(power.get("source"))
    poe = _inner(result, "poe") or {}
    if isinstance(poe, dict):
        out["poe_status"] = _as_str(poe.get("status"))
    features = result.get("hardware_features")
    if isinstance(features, dict):
        out["cec_supported"] = _as_bool(features.get("cec"))
        out["wifi_present"] = _as_bool(features.get("wifi"))
    ip, mac = "", ""
    for iface_list in (result.get("ethernet"), result.get("wireless")):
        for iface in iface_list if isinstance(iface_list, list) else []:
            for addr in iface.get("IPv4", []) if isinstance(iface, dict) else []:
                if isinstance(addr, dict) and not addr.get("internal"):
                    ip = ip or _as_str(addr.get("address"))
                    mac = mac or _as_str(addr.get("mac"))
    out["ip_address"] = ip
    out["mac_address"] = mac.lower()
    return out


def parse_time(result: dict[str, Any]) -> dict[str, Any]:
    """GET /time -> device state."""
    return {
        "player_time": _as_str(result.get("time")),
        "timezone": _as_str(result.get("timezone_name") or result.get("timezone_abbr")),
    }


def parse_video_mode(result: dict[str, Any]) -> dict[str, Any]:
    """GET /video-mode -> device state (width/height/frames are strings at
    the top level and numbers inside ``mode``; the numbers win)."""
    mode = result.get("mode") if isinstance(result.get("mode"), dict) else {}
    return {
        "video_mode": _as_str(result.get("name") or mode.get("modeName")),
        "video_mode_auto": _as_bool(result.get("isAutoMode")),
        "video_width": _as_int(mode.get("width", result.get("width"))),
        "video_height": _as_int(mode.get("height", result.get("height"))),
        "video_frame_rate": _as_int(mode.get("frequency", result.get("frames"))),
        "video_interlaced": _as_bool(mode.get("interlaced", result.get("scan") == "i")),
        "video_color_space": _as_str(mode.get("colorSpace")),
        "video_color_depth": _as_str(mode.get("colorDepth")),
    }


def parse_output(result: dict[str, Any]) -> dict[str, Any]:
    """GET /video/hdmi/output/N -> child state."""
    status = result.get("status") if isinstance(result.get("status"), dict) else {}
    active = result.get("activeMode") if isinstance(result.get("activeMode"), dict) else {}
    configured = result.get("configuredMode") if isinstance(result.get("configuredMode"), dict) else {}
    return {
        "display_connected": _as_bool(status.get("outputPresent")),
        "display_powered": _as_bool(status.get("outputPowered")),
        "signal_unstable": _as_bool(status.get("unstable")),
        "power_save": _as_bool(result.get("powerSaveStatus")),
        "mode": _as_str(active.get("modeName")),
        "configured_mode": _as_str(configured.get("modeName")),
        "best_mode": _as_str(result.get("bestMode")),
        "width": _as_int(active.get("width")),
        "height": _as_int(active.get("height")),
        "frame_rate": _as_int(active.get("frequency")),
        "color_space": _as_str(active.get("colorSpace")),
        "color_depth": _as_str(active.get("colorDepth")),
        "eotf": _as_str(status.get("eotf")),
        "audio_format": _as_str(status.get("audioFormat")),
        "audio_channels": _as_int(status.get("audioChannelCount")),
        "audio_sample_rate": _as_int(status.get("audioSampleRate")),
    }


def parse_power_save(result: dict[str, Any]) -> dict[str, Any]:
    """GET /video/hdmi/output/N/power-save -> child state."""
    return {
        "display_connected": _as_bool(result.get("is_connected")),
        "display_powered": _as_bool(result.get("is_powered")),
        "power_save": _as_bool(result.get("enabled")),
    }


def parse_display_control(result: dict[str, Any]) -> dict[str, Any]:
    """GET /display-control -> device state (Moka displays)."""
    wb = result.get("whiteBalance") if isinstance(result.get("whiteBalance"), dict) else {}
    tv = result.get("tvInfo") if isinstance(result.get("tvInfo"), dict) else {}
    return {
        "display_power": _as_str(result.get("powerSetting")),
        "display_volume": _as_int(result.get("volume")),
        "display_brightness": _as_int(result.get("brightness")),
        "display_contrast": _as_int(result.get("contrast")),
        "display_standby_timeout": _as_int(result.get("idleStandbyTimeout")),
        "display_video_output": _as_str(result.get("videoOutput")),
        "display_always_connected": _as_bool(result.get("alwaysConnectedEnabled")),
        "display_white_balance_red": _as_int(wb.get("redBalance")),
        "display_white_balance_green": _as_int(wb.get("greenBalance")),
        "display_white_balance_blue": _as_int(wb.get("blueBalance")),
        "display_serial": _as_str(tv.get("serialNo")),
        "display_os_version": _as_str(tv.get("osVersion")),
    }


def parse_diagnostics(result: dict[str, Any]) -> dict[str, Any]:
    """GET /diagnostics -> the per-interface verdicts as device state."""
    out: dict[str, Any] = {}
    for section, key in (("ethernet", "diag_ethernet"), ("wifi", "diag_wifi"), ("internet", "diag_internet")):
        block = result.get(section) if isinstance(result.get(section), dict) else {}
        out[key] = _as_str(block.get("diagnosis"))
        out[f"{key}_ok"] = _as_bool(block.get("ok"))
    return out


# ── Driver ──────────────────────────────────────────────────────────────────


class BrightSignPlayerDriver(BaseDriver):
    """BrightSign player control over the Local DWS REST API."""

    DRIVER_INFO = {
        "id": "brightsign_player",
        "name": "BrightSign Player (Local DWS)",
        "manufacturer": "BrightSign",
        "category": "streaming",
        "version": "1.0.4",
        "min_platform_version": "0.34.0",
        "author": "OpenAVC",
        "description": (
            "Monitors and controls BrightSign signage players through the "
            "player's Local Diagnostic Web Server API: identity, health, "
            "clock, every HDMI output with its display and video mode, HDMI "
            "power-save, reboot, snapshot, CEC, registry and log level, plus "
            "the Moka display's own panel controls. Drives the presentation "
            "with UDP messages and sets its User Variables."
        ),
        "source_url": "https://docs.brightsign.biz/develop/local-dws-apis",
        "tags": ["brightsign", "signage", "media-player", "digital-signage", "dws", "udp"],
        "verified": False,
        "simulated": True,
        "ports": [443, 80],
        "protocols": ["brightsign_ldws"],
        "transport": "http",
        "compatible_models": [
            {
                "manufacturer": "BrightSign",
                "models": [
                    "XS156", "XD236", "XD1036", "HD226", "HD1026",
                    "AU335", "HS125", "HS145", "HD225", "HD1025", "LS425", "LS445",
                    "XD235", "XD1035", "XT245", "XT1145", "XT2145", "XC2055", "XC4055",
                    "LS424", "HD224", "HD1024", "XD234", "XD1034", "XT244", "XT1144", "HS124", "HS144",
                    "LS423", "HD223", "HD1023", "XD233", "XD1033", "XD1133", "XT243", "XT1143", "HS123", "HO523",
                ],
                "confidence": "untested",
                "notes": (
                    "Every Series 3 to Series 6 player on BrightSignOS 8.4.6 or later "
                    "with the Local Diagnostic Web Server enabled. The XC2055 and "
                    "XT2145 expose two HDMI outputs and the XC4055 four; the driver "
                    "reads the count from the player."
                ),
            },
            {
                "manufacturer": "BrightSign",
                "models": ["Moka TV with BrightSign"],
                "confidence": "untested",
                "notes": (
                    "A display with a built-in player. The Display settings and the "
                    "Display Power actions use the display-control API, which needs "
                    "BrightSignOS 9.0.189 or later."
                ),
            },
        ],
        "help": {
            "overview": (
                "BrightSign players run a presentation from BrightSign Author (or a "
                "web page) and are managed through BrightSign Cloud. This driver "
                "talks to one player on the local network through its Local "
                "Diagnostic Web Server API: it reports the player's identity, "
                "health, clock and video mode, lists every HDMI output with the "
                "connected display's state, and can put an output to sleep, reboot "
                "the player, take a snapshot of what is playing, send a CEC "
                "payload, read and write the registry and change the log level. "
                "The presentation itself is driven the way BrightSign Author "
                "expects: a UDP message to the port the presentation listens on, "
                "which a UDP Input event in the presentation matches, and "
                "<variable>:<value> messages that set its User Variables. On a "
                "Moka display with a built-in player the display's power, volume, "
                "brightness, contrast, standby timeout, video output and white "
                "balance are settings too."
            ),
            "setup": (
                "1. Turn on the player's Local Diagnostic Web Server. In BrightSign "
                "Author, create or edit a Setup file (Admin > Setup), tick Enable "
                "Local Diagnostic Web Server under Player Settings > Player "
                "Configuration, enter a password, and provision the player with it. "
                "Players on BrightSignOS 9.0.218 / 9.1.75 or later have it off by "
                "default; older ones have it on with the serial number as the "
                "password. A cloud-managed player can also be switched under "
                "Network > Properties.\n"
                "2. Enter the player's IP address. Leave Use HTTPS on and the port "
                "at 443 for BrightSignOS 9.0.218 / 9.1.52 or later; an older player "
                "that still serves plain HTTP uses port 80 with Use HTTPS off.\n"
                "3. The username is always admin. Enter the password from the "
                "setup file, or the player's serial number if none was set. Leave "
                "it blank only for a player set up with no authentication.\n"
                "4. To control the presentation, set the UDP Port to the UDP "
                "Receiver Port in the presentation's settings (Presentation "
                "Settings > Interactive > Networking in BrightSign Author) and "
                "add UDP Input events for the messages you will send.\n"
                "5. Local DWS access is over the local network only; a player "
                "reached through BrightSign Cloud is out of reach of this driver."
            ),
            "connection": (
                "Enable the Local Diagnostic Web Server in the player's setup file first; "
                "user admin, password from the setup file or the serial number."
            ),
        },
        "discovery": {
            # No endpoint of the Local DWS answers without a login, so the
            # driver is hint-only: BrightSign's one OUI (90:AC:3F, maclookup
            # 2026-09-09 and the MAC addresses in BrightSign's own examples)
            # and the factory hostname brightsign-<serial>.
            "oui": ["90:ac:3f"],
            "hostname": ["^brightsign-"],
            "manufacturer_alias": ["BrightSign", "BrightSign LLC"],
        },
        "default_config": {
            "host": "",
            "port": 443,
            "ssl": True,
            "verify_ssl": False,
            "username": "admin",
            "password": "",
            "udp_port": 5000,
            "poll_interval": 10,
            "detail_poll_every": 6,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
                "description": "The player's IP address on the local network.",
            },
            "port": {
                "type": "integer",
                "default": 443,
                "min": 1,
                "max": 65535,
                "label": "Port",
                "help": "443 for HTTPS (BrightSignOS 9.0.218 / 9.1.52 and later), 80 for an older player on plain HTTP.",
            },
            "ssl": {
                "type": "boolean",
                "label": "Use HTTPS",
                "default": True,
                "help": "On for current players, which serve the API over HTTPS and redirect HTTP. Off only for an older player that still answers plain HTTP on port 80.",
            },
            "verify_ssl": {
                "type": "boolean",
                "label": "Verify Certificate",
                "default": False,
                "advanced": True,
                "help": "Only for HTTPS. Off for the self-signed certificate the player ships with; on if you installed a trusted certificate (dws.crt / dws.key).",
            },
            "username": {
                "type": "string",
                "default": "admin",
                "label": "Username",
                "advanced": True,
                "help": "Always admin on a BrightSign player.",
            },
            "password": {
                "type": "string",
                "label": "Password",
                "secret": True,
                "help": "The Local DWS password from the setup file, or the player's serial number if none was set. Blank for a player set up with no authentication.",
            },
            "udp_port": {
                "type": "integer",
                "default": 5000,
                "min": 0,
                "max": 65535,
                "label": "UDP Port",
                "help": "The presentation's UDP Receiver Port (Presentation Settings > Interactive > Networking in BrightSign Author). Send UDP Message and Set Presentation Variable go here. 0 turns them off.",
            },
            "poll_interval": {
                "type": "integer",
                "default": 10,
                "min": 0,
                "label": "Poll Interval (sec)",
                "help": "How often to read the player's health and every HDMI output.",
            },
            "detail_poll_every": {
                "type": "integer",
                "default": 6,
                "min": 1,
                "max": 100,
                "label": "Detail Refresh (polls)",
                "advanced": True,
                "help": "Player information, the clock, the video mode, the log level and the Moka display are re-read every this many polls.",
            },
        },
        "state_variables": {
            "serial": {"type": "string", "label": "Serial Number"},
            "model": {"type": "string", "label": "Model", "help": "The player model, for example XD1035."},
            "family": {"type": "string", "label": "OS Family", "help": "The BrightSignOS family, for example cobra."},
            "firmware_version": {"type": "string", "label": "Firmware Version"},
            "boot_version": {"type": "string", "label": "Boot Version"},
            "device_name": {"type": "string", "label": "Player Name", "help": "The player's name as set in BrightSign Author."},
            "device_description": {"type": "string", "label": "Player Description"},
            "uptime": {"type": "string", "label": "Uptime", "help": "How long the player has been up, as the player words it."},
            "uptime_seconds": {"type": "integer", "label": "Uptime (s)", "unit": "s", "cloud_priority": "low"},
            "connection_type": {"type": "string", "label": "Active Interface", "help": "The interface the player is reached on (eth0, wlan0)."},
            "ip_address": {"type": "string", "label": "IP Address"},
            "mac_address": {"type": "string", "label": "MAC Address"},
            "power_source": {"type": "string", "label": "Power Source", "help": "AC, PoE or battery, as the player reports it."},
            "poe_status": {"type": "string", "label": "PoE Status"},
            "cec_supported": {"type": "boolean", "label": "CEC Supported", "help": "True when the player has a CEC-capable HDMI output; Send CEC needs it."},
            "wifi_present": {"type": "boolean", "label": "Wi-Fi Module Present"},
            "health": {"type": "string", "label": "Health", "help": "The health endpoint's status; the player only ever says active.", "cloud_priority": "high"},
            "health_time": {"type": "string", "label": "Health Reported At"},
            "player_time": {"type": "string", "label": "Player Clock", "help": "The player's date and time with its time zone."},
            "timezone": {"type": "string", "label": "Time Zone"},
            "video_mode": {"type": "string", "label": "Video Mode", "help": "The player's output mode, for example 1920x1080x60p."},
            "video_mode_auto": {"type": "boolean", "label": "Video Mode Auto"},
            "video_width": {"type": "integer", "label": "Video Width", "unit": "px"},
            "video_height": {"type": "integer", "label": "Video Height", "unit": "px"},
            "video_frame_rate": {"type": "integer", "label": "Video Frame Rate", "unit": "Hz"},
            "video_interlaced": {"type": "boolean", "label": "Video Interlaced"},
            "video_color_space": {"type": "string", "label": "Video Color Space"},
            "video_color_depth": {"type": "string", "label": "Video Color Depth"},
            "output_count": {"type": "integer", "label": "HDMI Outputs", "help": "How many HDMI outputs the player reports."},
            "local_dws_enabled": {"type": "boolean", "label": "Local DWS Enabled"},
            "log_level": {
                "type": "enum",
                "values": ["error", "warn", "info", "trace"],
                "label": "Supervisor Log Level",
                "help": "The player's supervisor logging level; info is the default.",
            },
            "last_snapshot_file": {"type": "string", "label": "Last Snapshot File", "help": "Path on the player's storage of the last snapshot taken."},
            "last_snapshot_time": {"type": "string", "label": "Last Snapshot Time"},
            "registry_section": {"type": "string", "label": "Registry Section (last read)"},
            "registry_key": {"type": "string", "label": "Registry Key (last read)"},
            "registry_value": {"type": "string", "label": "Registry Value (last read)"},
            "diag_ethernet": {"type": "string", "label": "Diagnostics: Ethernet", "help": "The player's own verdict from Run Network Diagnostics."},
            "diag_ethernet_ok": {"type": "boolean", "label": "Diagnostics: Ethernet OK"},
            "diag_wifi": {"type": "string", "label": "Diagnostics: Wi-Fi"},
            "diag_wifi_ok": {"type": "boolean", "label": "Diagnostics: Wi-Fi OK"},
            "diag_internet": {"type": "string", "label": "Diagnostics: Internet"},
            "diag_internet_ok": {"type": "boolean", "label": "Diagnostics: Internet OK"},
            "display_control_supported": {
                "type": "boolean",
                "label": "Display Control Available",
                "help": "True on a Moka display with a built-in player; the Display settings and actions work only there.",
            },
            "display_power": {"type": "string", "label": "Display Power", "help": "on or standby (Moka display).", "control": True},
            "display_volume": {"type": "integer", "label": "Display Volume", "min": 0, "max": 100, "control": True},
            "display_brightness": {"type": "integer", "label": "Display Brightness", "min": 0, "max": 100, "control": True},
            "display_contrast": {"type": "integer", "label": "Display Contrast", "min": 0, "max": 100, "control": True},
            "display_standby_timeout": {"type": "integer", "label": "Display Standby Timeout", "unit": "s"},
            "display_video_output": {"type": "string", "label": "Display Video Input", "help": "The display's selected input, for example HDMI1."},
            "display_always_on": {"type": "boolean", "label": "Display Always On"},
            "display_always_connected": {"type": "boolean", "label": "Display Always Connected"},
            "display_white_balance_red": {"type": "integer", "label": "Display White Balance Red"},
            "display_white_balance_green": {"type": "integer", "label": "Display White Balance Green"},
            "display_white_balance_blue": {"type": "integer", "label": "Display White Balance Blue"},
            "display_serial": {"type": "string", "label": "Display Serial Number"},
            "display_os_version": {"type": "string", "label": "Display OS Version"},
            "last_error": {"type": "string", "label": "Last Error", "help": "The last error the player reported to a command; cleared by the next clean poll."},
        },
        "child_entity_types": {
            "hdmi_output": {
                "label": "HDMI Output",
                "label_plural": "HDMI Outputs",
                "id_format": {"type": "integer", "min": 0, "max": 3},
                "state_variables": {
                    "display_connected": {"type": "boolean", "label": "Display Connected", "help": "A display is attached to this output.", "cloud_priority": "high"},
                    "display_powered": {"type": "boolean", "label": "Display Powered", "help": "The attached display is on.", "cloud_priority": "high"},
                    "signal_unstable": {"type": "boolean", "label": "Signal Unstable"},
                    "power_save": {"type": "boolean", "label": "Power Save", "help": "The output is asleep (Display Sleep).", "control": True},
                    "mode": {"type": "string", "label": "Active Mode", "help": "The mode on the wire, for example 1920x1080x60p."},
                    "configured_mode": {"type": "string", "label": "Configured Mode"},
                    "best_mode": {"type": "string", "label": "Best Mode", "help": "The best mode the display's EDID allows."},
                    "width": {"type": "integer", "label": "Width", "unit": "px"},
                    "height": {"type": "integer", "label": "Height", "unit": "px"},
                    "frame_rate": {"type": "integer", "label": "Frame Rate", "unit": "Hz"},
                    "color_space": {"type": "string", "label": "Color Space"},
                    "color_depth": {"type": "string", "label": "Color Depth"},
                    "eotf": {"type": "string", "label": "EOTF", "help": "SDR (GAMMA), HDR (GAMMA), SMPTE 2084 (PQ), or unspecified."},
                    "audio_format": {"type": "string", "label": "Audio Format", "help": "PCM when the player is sending decoded audio."},
                    "audio_channels": {"type": "integer", "label": "Audio Channels"},
                    "audio_sample_rate": {"type": "integer", "label": "Audio Sample Rate", "unit": "Hz"},
                },
                "summary_fields": ["display_connected", "display_powered", "mode", "power_save"],
            },
        },
        "commands": {
            "reboot": {
                "label": "Reboot",
                "params": {},
                # The player is off the network while it boots; the platform reports
                # it as restarting rather than as a fault for this long. Unmeasured
                # (no hardware): the bench should round the real gap up.
                "restarts_device_for": 60,
                "help": "Reboot the player. It drops offline for about a minute and reconnects by itself.",
            },
            "reboot_disable_autorun": {
                "label": "Reboot Without Autorun",
                "params": {},
                # The player is off the network while it boots; the platform reports
                # it as restarting rather than as a fault for this long. Unmeasured
                # (no hardware): the bench should round the real gap up.
                "restarts_device_for": 60,
                "help": "Reboot the player with its autorun script disabled, for troubleshooting a presentation. The player shows its default screen until the presentation is republished.",
            },
            "factory_reset": {
                "label": "Factory Reset",
                "params": {},
                # The player is off the network while it boots; the platform reports
                # it as restarting rather than as a fault for this long. Unmeasured
                # (no hardware): the bench should round the real gap up.
                "restarts_device_for": 60,
                "help": "Erase the player's persistent registry settings (networking, security, applications) and reboot. The player will need to be set up again.",
            },
            "display_sleep": {
                "label": "Display Sleep",
                "params": {
                    "output": {"type": "child_id", "child_type": "hdmi_output", "required": True, "label": "HDMI Output", "help": "HDMI output number: 0 on every single-output player; 0 or 1 on the XC2055 and XT2145; 0 to 3 on the XC4055."},
                },
                "help": "Turn on HDMI power save for an output: the display goes to sleep while the presentation keeps running.",
            },
            "display_wake": {
                "label": "Display Wake",
                "params": {
                    "output": {"type": "child_id", "child_type": "hdmi_output", "required": True, "label": "HDMI Output", "help": "HDMI output number: 0 on every single-output player; 0 or 1 on the XC2055 and XT2145; 0 to 3 on the XC4055."},
                },
                "help": "Turn off HDMI power save for an output so the display wakes.",
            },
            "send_udp_message": {
                "label": "Send UDP Message",
                "params": {
                    "message": {"type": "string", "required": True, "trim": False, "label": "Message", "help": "Sent exactly as written to the presentation's UDP Receiver Port. A UDP Input event in the presentation with this string (or a matching <any> pattern) fires."},
                },
                "help": "Send a message to the presentation. Matches a UDP Input event in BrightSign Author.",
            },
            "set_presentation_variable": {
                "label": "Set Presentation Variable",
                "params": {
                    "name": {"type": "string", "required": True, "label": "Variable Name", "help": "A User Variable defined in the presentation."},
                    "value": {"type": "string", "required": True, "trim": False, "label": "Value"},
                },
                "help": "Send <name>:<value> to the presentation. The presentation needs a UDP Input event with Assign input to variable set to Input specifies variable.",
            },
            "send_cec": {
                "label": "Send CEC Command",
                "params": {
                    "hex_command": {"type": "string", "required": True, "pattern": "^[0-9a-fA-F]+$", "label": "CEC Payload (hex)", "help": "The CEC frame as hex, for example 4f36 (broadcast Standby) or 4f04 (Image View On). cec-o-matic.com builds them."},
                },
                "help": "Send a raw CEC payload out of HDMI 1, for example to switch the display off or on. BrightSign marks this API experimental.",
            },
            "take_snapshot": {
                "label": "Take Snapshot",
                "params": {
                    "width": {"type": "integer", "required": False, "min": 16, "max": 7680, "label": "Width"},
                    "height": {"type": "integer", "required": False, "min": 16, "max": 4320, "label": "Height"},
                },
                "help": "Capture what is playing to the player's storage (remote_snapshots). The file path is reported in Last Snapshot File.",
            },
            "sync_time": {
                "label": "Sync Clock From Controller",
                "params": {},
                "help": "Set the player's date and time from this controller's clock, in the player's own time zone.",
            },
            "set_time": {
                "label": "Set Clock",
                "params": {
                    "date": {"type": "string", "required": True, "pattern": "^\\d{4}-\\d{2}-\\d{2}$", "label": "Date", "help": "YYYY-MM-DD"},
                    "time": {"type": "string", "required": True, "pattern": "^\\d{2}:\\d{2}(:\\d{2})?$", "label": "Time", "help": "HH:MM or HH:MM:SS"},
                    "apply_timezone": {"type": "boolean", "required": False, "label": "In Player's Time Zone", "help": "On: the time is local to the player's time zone. Off: the time is UTC."},
                },
                "help": "Set the player's date and time.",
            },
            "read_registry_key": {
                "label": "Read Registry Key",
                "params": {
                    "section": {"type": "string", "required": True, "label": "Section", "help": "For example networking or html."},
                    "key": {"type": "string", "required": True, "label": "Key"},
                },
                "help": "Read one registry value into Registry Value (last read).",
            },
            "set_registry_key": {
                "label": "Write Registry Key",
                "params": {
                    "section": {"type": "string", "required": True, "label": "Section"},
                    "key": {"type": "string", "required": True, "label": "Key"},
                    "value": {"type": "string", "required": True, "trim": False, "label": "Value"},
                },
                "help": "Write one registry value. Applications rely on the registry; a wrong key can leave the player unstable. Follow with Flush Registry before a power cycle.",
            },
            "delete_registry_key": {
                "label": "Delete Registry Key",
                "params": {
                    "section": {"type": "string", "required": True, "label": "Section"},
                    "key": {"type": "string", "required": True, "label": "Key"},
                },
                "help": "Remove one registry value.",
            },
            "flush_registry": {
                "label": "Flush Registry",
                "params": {},
                "help": "Write the registry to persistent storage now (BrightSignOS 9.0.107 / 8.5.46 and later). Registry writes otherwise buffer for a few seconds.",
            },
            "run_network_diagnostics": {
                "label": "Run Network Diagnostics",
                "params": {},
                "help": "Have the player test its Ethernet, Wi-Fi and internet connectivity (DNS, gateway, time server). Takes several seconds; the verdicts land in the Diagnostics states.",
            },
            "display_power_on": {
                "label": "Display Power On",
                "params": {},
                "help": "Turn the Moka display on.",
            },
            "display_power_standby": {
                "label": "Display Standby",
                "params": {},
                "help": "Put the Moka display into standby.",
            },
        },
        "device_settings": {
            "log_level": {
                "type": "enum",
                "values": ["error", "warn", "info", "trace"],
                "label": "Supervisor Log Level",
                "help": "How much the player's supervisor logs. info is the default; trace is the most.",
                "state_key": "log_level",
                "default": "info",
                "setup": False,
            },
            "display_volume": {
                "type": "integer",
                "label": "Display Volume",
                "help": "Moka display only: the panel's own volume, 0 to 100.",
                "state_key": "display_volume",
                "default": 50,
                "min": 0,
                "max": 100,
                "setup": False,
            },
            "display_brightness": {
                "type": "integer",
                "label": "Display Brightness",
                "help": "Moka display only: 0 to 100.",
                "state_key": "display_brightness",
                "default": 50,
                "min": 0,
                "max": 100,
                "setup": False,
            },
            "display_contrast": {
                "type": "integer",
                "label": "Display Contrast",
                "help": "Moka display only: 0 to 100.",
                "state_key": "display_contrast",
                "default": 50,
                "min": 0,
                "max": 100,
                "setup": False,
            },
            "display_standby_timeout": {
                "type": "integer",
                "label": "Display Standby Timeout (s)",
                "help": "Moka display only: seconds of idle before the display goes to standby.",
                "state_key": "display_standby_timeout",
                "default": 60,
                "min": 0,
                "setup": False,
            },
            "display_video_output": {
                "type": "string",
                "label": "Display Video Input",
                "help": "Moka display only: the display's input, for example HDMI1 or HDMI2.",
                "state_key": "display_video_output",
                "default": "HDMI1",
                "setup": False,
            },
            "display_always_on": {
                "type": "boolean",
                "label": "Display Always On",
                "help": "Moka display only: keep the display on.",
                "state_key": "display_always_on",
                "default": False,
                "setup": False,
            },
            "display_always_connected": {
                "type": "boolean",
                "label": "Display Always Connected",
                "help": "Moka display only: keep the built-in player connected to the display.",
                "state_key": "display_always_connected",
                "default": True,
                "setup": False,
            },
            "display_white_balance_red": {
                "type": "integer",
                "label": "Display White Balance Red",
                "help": "Moka display only. The three white balance values are written together.",
                "state_key": "display_white_balance_red",
                "default": 120,
                "min": 0,
                "setup": False,
            },
            "display_white_balance_green": {
                "type": "integer",
                "label": "Display White Balance Green",
                "help": "Moka display only.",
                "state_key": "display_white_balance_green",
                "default": 120,
                "min": 0,
                "setup": False,
            },
            "display_white_balance_blue": {
                "type": "integer",
                "label": "Display White Balance Blue",
                "help": "Moka display only.",
                "state_key": "display_white_balance_blue",
                "default": 120,
                "min": 0,
                "setup": False,
            },
        },
        "actions": [
            {"id": "reboot", "kind": "command", "icon": "rotate-ccw", "confirm": "Reboot the player? It drops offline until it restarts."},
            {"id": "take_snapshot", "kind": "command", "icon": "camera"},
            {"id": "sync_time", "kind": "command", "icon": "clock"},
            {"id": "run_network_diagnostics", "kind": "command", "icon": "activity"},
            {
                "id": "display_power_on",
                "kind": "command",
                "icon": "monitor",
                "visible_when": {"key": "device.$id.display_control_supported", "operator": "truthy"},
            },
            {
                "id": "display_power_standby",
                "kind": "command",
                "icon": "monitor-off",
                "visible_when": {"key": "device.$id.display_control_supported", "operator": "truthy"},
            },
            {
                "id": "factory_reset",
                "kind": "command",
                "icon": "alert-triangle",
                "confirm": "Factory reset erases the player's network, security and application settings and reboots it. The player will need to be set up again. Continue?",
            },
        ],
    }

    _client: httpx.AsyncClient | None = None

    def __init__(self, device_id: str, config: dict[str, Any], state: Any, events: Any) -> None:
        super().__init__(device_id, config, state, events)
        self._client = None
        self._auth: httpx.DigestAuth | None = None
        self._polls = 0
        self._output_ids: list[int] = []
        password = str(config.get("password", "") or "")
        if password:
            self.redact_in_log(password)

    # ── Config accessors ──

    @property
    def _host(self) -> str:
        return str(self.config.get("host", "") or "").strip()

    @property
    def _port(self) -> int:
        return _as_int(self.config.get("port"), 443) or 443

    @property
    def _scheme(self) -> str:
        return "https" if _as_bool(self.config.get("ssl", True)) else "http"

    @property
    def _username(self) -> str:
        return str(self.config.get("username", "") or "admin").strip() or "admin"

    @property
    def _password(self) -> str:
        return str(self.config.get("password", "") or "")

    @property
    def _udp_port(self) -> int:
        return _as_int(self.config.get("udp_port"), 0)

    def _base_url(self) -> str:
        return f"{self._scheme}://{self._host}:{self._port}"

    def _auth_fault(self) -> ConnectionFaultError:
        if not self._password:
            message = (
                "The player wants a password and none is entered. The default is the "
                "player's serial number; a setup file may have set another one. "
                "Enter it under Edit Device and press Reconnect."
            )
        else:
            message = (
                "The player refused the login. The username is always admin; the "
                "password is the one in the player's setup file, or the serial number "
                "if none was set."
            )
        return ConnectionFaultError(message, code="auth_failed")

    # ── Connection lifecycle ──

    async def _create_transport(self, transport_type: str) -> None:
        if not self._host:
            raise ConnectionFaultError("No IP address configured", code="invalid_config")
        if not await self._verify_reachable(self._host, self._port):
            raise ConnectionError(f"{self._host}:{self._port} is not responding")
        self._auth = httpx.DigestAuth(self._username, self._password)
        self._client = httpx.AsyncClient(
            base_url=self._base_url(),
            verify=_as_bool(self.config.get("verify_ssl", False)),
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=True,
        )

    async def _post_connect(self) -> None:
        try:
            await self._read_info()
        except DwsError as exc:
            if exc.not_authorized:
                raise self._auth_fault() from exc
            raise ConnectionError(f"The player answered with an error: {exc}") from exc
        except httpx.TransportError as exc:
            raise ConnectionError(f"{self._host} is not responding: {exc}") from exc
        log.info(
            f"[{self.device_id}] Connected to BrightSign {self.get_state('model') or 'player'} "
            f"{self.get_state('serial')} at {self._host}:{self._port} "
            f"(BrightSignOS {self.get_state('firmware_version')})"
        )

    async def _initial_sync(self) -> None:
        try:
            await self._enumerate_outputs()
            await self._probe_display_control()
            await self._read_detail()
            await self._read_health()
            await self._read_outputs()
        except DwsError as exc:
            if exc.not_authorized:
                raise self._auth_fault() from exc
            raise ConnectionError(f"The player answered with an error: {exc}") from exc
        except httpx.TransportError as exc:
            raise ConnectionError(f"{self._host} is not responding: {exc}") from exc

    def _link_alive(self) -> bool:
        return self._client is not None

    async def _close_session(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _liveness_probe(self) -> None:
        try:
            await self._read_health()
        except DwsError as exc:
            if exc.not_authorized:
                raise self._auth_fault() from exc
            raise ConnectionFaultError(
                f"Connected, but the player answered the health check with an error: {exc}",
                code="no_response",
            ) from exc
        except httpx.TransportError as exc:
            raise ConnectionError(f"{self._host} is not responding: {exc}") from exc

    # ── HTTP plumbing ──

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """One authenticated request; returns the unwrapped ``data.result``."""
        client = self._client
        if client is None:
            raise ConnectionError("Not connected")
        kwargs: dict[str, Any] = {"auth": self._auth}
        if json_body is not None:
            kwargs["json"] = json_body
        if params:
            kwargs["params"] = params
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(timeout, connect=5.0)
        resp = await client.request(method, f"{API}{path}", **kwargs)
        if resp.status_code in (401, 403):
            raise DwsError("The player refused the login", http_status=resp.status_code)
        try:
            payload = resp.json()
        except ValueError:
            payload = resp.text
        return unwrap_result(payload, http_status=resp.status_code)

    async def _get(self, path: str, **kw: Any) -> Any:
        return await self._request("GET", path, **kw)

    async def _put(self, path: str, body: Any = None, **kw: Any) -> Any:
        return await self._request("PUT", path, json_body=body, **kw)

    async def _post(self, path: str, body: Any = None, **kw: Any) -> Any:
        return await self._request("POST", path, json_body=body, **kw)

    async def _delete(self, path: str, **kw: Any) -> Any:
        return await self._request("DELETE", path, **kw)

    # ── Reads ──

    async def _read_info(self) -> None:
        result = await self._get("/info")
        if not isinstance(result, dict):
            raise DwsError("The info reply was not in the expected format")
        self.set_states(parse_info(result))

    async def _read_health(self) -> None:
        result = await self._get("/health")
        if not isinstance(result, dict) or "status" not in result:
            raise DwsError("The health reply was not in the expected format")
        self.set_states({
            "health": _as_str(result.get("status")),
            "health_time": _as_str(result.get("statusTime")),
        })

    async def _read_time(self) -> None:
        result = await self._get("/time")
        if isinstance(result, dict):
            self.set_states(parse_time(result))

    async def _read_video_mode(self) -> None:
        result = await self._get("/video-mode")
        if isinstance(result, dict):
            self.set_states(parse_video_mode(result))

    async def _read_log_level(self) -> None:
        result = await self._get("/system/supervisor/logging")
        if isinstance(result, dict):
            name = _as_str(result.get("name")).lower()
            if name not in LOG_LEVELS:
                name = LOG_LEVEL_NAMES.get(_as_int(result.get("level"), -1), "")
            if name:
                self.set_state("log_level", name)

    async def _read_local_dws(self) -> None:
        result = await self._get("/control/local-dws")
        if isinstance(result, dict) and "value" in result:
            self.set_state("local_dws_enabled", _as_bool(result.get("value")))

    async def _read_detail(self) -> None:
        """The slow-cadence reads. Each is its own request; one that the
        firmware lacks (an older BrightSignOS without the logging route) is
        logged and skipped rather than failing the whole cycle."""
        for reader in (self._read_time, self._read_video_mode, self._read_log_level, self._read_local_dws):
            try:
                await reader()
            except DwsError as exc:
                if exc.not_authorized:
                    raise
                log.debug(f"[{self.device_id}] {reader.__name__} skipped: {exc}")
        if self.get_state("display_control_supported"):
            await self._read_display_control()

    async def _enumerate_outputs(self) -> None:
        """Register one child per HDMI output the player answers for:
        output 0 always, then 1..3 until the first one the player refuses
        (the XC2055 and XT2145 have two, the XC4055 four)."""
        found: list[int] = []
        for n in range(MAX_OUTPUTS):
            try:
                result = await self._get(f"/video/hdmi/output/{n}")
            except DwsError as exc:
                if exc.not_authorized:
                    raise
                if n == 0:
                    log.warning(f"[{self.device_id}] The player did not answer for HDMI output 0: {exc}")
                break
            if not isinstance(result, dict):
                break
            found.append(n)
            self.register_child("hdmi_output", n, initial_state={"label": f"HDMI {n + 1}"})
            self.set_child_state_batch("hdmi_output", n, parse_output(result))
        for old in list(self._output_ids):
            if old not in found:
                self.deregister_child("hdmi_output", old)
        self._output_ids = found
        self.set_state("output_count", len(found))

    async def _read_outputs(self) -> None:
        for n in self._output_ids:
            result = await self._get(f"/video/hdmi/output/{n}")
            if isinstance(result, dict):
                self.set_child_state_batch("hdmi_output", n, parse_output(result))

    async def _probe_display_control(self) -> None:
        """Moka displays answer /display-control; every other player refuses."""
        try:
            result = await self._get("/display-control")
        except DwsError as exc:
            if exc.not_authorized:
                raise
            self.set_state("display_control_supported", False)
            return
        supported = isinstance(result, dict) and ("powerSetting" in result or "volume" in result)
        self.set_state("display_control_supported", bool(supported))
        if supported:
            self.set_states(parse_display_control(result))
            await self._read_display_always_on()

    async def _read_display_control(self) -> None:
        result = await self._get("/display-control")
        if isinstance(result, dict):
            self.set_states(parse_display_control(result))
        await self._read_display_always_on()

    async def _read_display_always_on(self) -> None:
        try:
            result = await self._get("/display-control/always-on")
        except DwsError as exc:
            if exc.not_authorized:
                raise
            return
        if isinstance(result, dict) and "enabled" in result:
            self.set_state("display_always_on", _as_bool(result.get("enabled")))

    # ── Polling ──

    async def poll(self) -> None:
        if self._client is None:
            return
        self._polls += 1
        every = max(1, _as_int(self.config.get("detail_poll_every"), 6))
        try:
            await self._read_health()
            await self._read_outputs()
            if self._polls % every == 0:
                await self._read_info()
                await self._read_detail()
        except DwsError as exc:
            if exc.not_authorized:
                raise self._auth_fault() from exc
            # The player answered, with something other than its status: it
            # is reachable but not in a state that answers, so say so on the
            # card without counting it as a dead link.
            self.set_state("last_error", str(exc))
        except httpx.TransportError as exc:
            raise ConnectionError(f"{self._host} is not responding: {exc}") from exc

    async def refresh_children(self) -> Any:
        try:
            await self._enumerate_outputs()
        except DwsError as exc:
            raise ValueError(str(exc)) from exc
        except httpx.TransportError as exc:
            raise ConnectionError(f"{self._host} is not responding: {exc}") from exc
        return {"hdmi_output": list(self._output_ids)}

    # ── Commands ──

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        handler = self._DISPATCH.get(command)
        if handler is None:
            raise ValueError(f"Unknown command: {command}")
        try:
            return await handler(self, params)
        except DwsError as exc:
            if exc.not_authorized:
                raise self._auth_fault() from exc
            self.set_state("last_error", str(exc))
            raise ValueError(str(exc)) from exc
        except httpx.TransportError as exc:
            raise ConnectionError(f"{self._host} is not responding: {exc}") from exc

    async def _cmd_reboot(self, params: dict[str, Any]) -> Any:
        return await self._put("/control/reboot")

    async def _cmd_reboot_disable_autorun(self, params: dict[str, Any]) -> Any:
        return await self._put("/control/reboot", {"autorun": "disable"})

    async def _cmd_factory_reset(self, params: dict[str, Any]) -> Any:
        return await self._put("/control/reboot", {"factory_reset": True})

    def _output_param(self, params: dict[str, Any]) -> int:
        n = _as_int(params.get("output"), -1)
        if n < 0 or n >= MAX_OUTPUTS:
            raise ValueError(f"'output' must be between 0 and {MAX_OUTPUTS - 1}")
        if self._output_ids and n not in self._output_ids:
            raise ValueError(f"This player has no HDMI output {n} (it reports {len(self._output_ids)}).")
        return n

    async def _set_power_save(self, params: dict[str, Any], enabled: bool) -> Any:
        n = self._output_param(params)
        await self._put(f"/video/hdmi/output/{n}/power-save", {"enabled": enabled})
        # Read the output back rather than assuming the display followed.
        result = await self._get(f"/video/hdmi/output/{n}/power-save")
        if isinstance(result, dict):
            if self.is_child_registered("hdmi_output", n):
                self.set_child_state_batch("hdmi_output", n, parse_power_save(result))
            return result
        return None

    async def _cmd_display_sleep(self, params: dict[str, Any]) -> Any:
        return await self._set_power_save(params, True)

    async def _cmd_display_wake(self, params: dict[str, Any]) -> Any:
        return await self._set_power_save(params, False)

    async def _udp_send(self, text: str) -> None:
        """One datagram to the presentation's UDP receiver on the player.

        The platform's ``send_udp`` opens a socket for the send and closes it
        after; the port is the ``udp_port`` config field, which is the
        presentation's own setting rather than anything the player reports.
        """
        port = self._udp_port
        if port <= 0:
            raise ValueError(
                "Set the UDP Port under Edit Device to the presentation's UDP Receiver Port first."
            )
        await self.send_udp(text.encode("utf-8"), host=self._host, port=port)

    async def _cmd_send_udp_message(self, params: dict[str, Any]) -> Any:
        message = str(params.get("message", ""))
        if not message:
            raise ValueError("'message' is required")
        await self._udp_send(message)
        return {"sent": message, "port": self._udp_port}

    async def _cmd_set_presentation_variable(self, params: dict[str, Any]) -> Any:
        name = str(params.get("name", "")).strip()
        value = str(params.get("value", ""))
        if not name:
            raise ValueError("'name' is required")
        if ":" in name:
            raise ValueError("A variable name cannot contain ':'")
        message = f"{name}:{value}"
        await self._udp_send(message)
        return {"sent": message, "port": self._udp_port}

    async def _cmd_send_cec(self, params: dict[str, Any]) -> Any:
        payload = str(params.get("hex_command", "")).strip().replace(" ", "").replace(":", "")
        if not payload or not _HEX_RE.match(payload) or len(payload) % 2:
            raise ValueError("'hex_command' must be an even number of hex digits, for example 4f36")
        return await self._post("/sendCecX", {"hexCommand": payload.lower()})

    async def _cmd_take_snapshot(self, params: dict[str, Any]) -> Any:
        body: dict[str, Any] = {}
        for key in ("width", "height"):
            value = params.get(key)
            if value not in (None, ""):
                body[key] = _as_int(value)
        result = await self._post("/snapshot", body or None, timeout=30.0)
        if isinstance(result, dict):
            self.set_states({
                "last_snapshot_file": _as_str(result.get("filename")),
                "last_snapshot_time": _as_str(result.get("timestamp")),
            })
            # The thumbnail is a data: URL, too large for a state value.
            return {k: v for k, v in result.items() if k != "remoteSnapshotThumbnail"}
        return result

    async def _set_time(self, date: str, time_: str, apply_timezone: bool) -> Any:
        if not _DATE_RE.match(date):
            raise ValueError("'date' must be YYYY-MM-DD")
        if not _TIME_RE.match(time_):
            raise ValueError("'time' must be HH:MM or HH:MM:SS")
        if len(time_) == 5:
            time_ += ":00"
        # The reference lists date / time / applyTimezone as the body; its
        # example wraps them in "data". BrightSign's own bsc CLI sends the
        # flat form, so that is what goes on the wire.
        result = await self._put("/time", {"date": date, "time": time_, "applyTimezone": apply_timezone})
        await self._read_time()
        return result

    async def _cmd_sync_time(self, params: dict[str, Any]) -> Any:
        now = datetime.now()
        return await self._set_time(now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), True)

    async def _cmd_set_time(self, params: dict[str, Any]) -> Any:
        apply = params.get("apply_timezone")
        return await self._set_time(
            str(params.get("date", "")).strip(),
            str(params.get("time", "")).strip(),
            True if apply in (None, "") else _as_bool(apply),
        )

    @staticmethod
    def _registry_path(params: dict[str, Any], *, need_key: bool = True) -> str:
        section = str(params.get("section", "")).strip()
        key = str(params.get("key", "")).strip()
        if not section:
            raise ValueError("'section' is required")
        if need_key and not key:
            raise ValueError("'key' is required")
        path = f"/registry/{quote(section, safe='')}"
        if key:
            path += f"/{quote(key, safe='')}"
        return path

    async def _cmd_read_registry_key(self, params: dict[str, Any]) -> Any:
        result = await self._get(self._registry_path(params))
        if isinstance(result, dict):
            self.set_states({
                "registry_section": _as_str(result.get("section") or params.get("section")),
                "registry_key": _as_str(result.get("key") or params.get("key")),
                "registry_value": _as_str(result.get("value")),
            })
        return result

    async def _cmd_set_registry_key(self, params: dict[str, Any]) -> Any:
        result = await self._put(self._registry_path(params), {"value": str(params.get("value", ""))})
        if isinstance(result, dict):
            self.set_states({
                "registry_section": _as_str(result.get("section") or params.get("section")),
                "registry_key": _as_str(result.get("key") or params.get("key")),
                "registry_value": _as_str(result.get("value", params.get("value", ""))),
            })
        return result

    async def _cmd_delete_registry_key(self, params: dict[str, Any]) -> Any:
        return await self._delete(self._registry_path(params))

    async def _cmd_flush_registry(self, params: dict[str, Any]) -> Any:
        return await self._put("/registry/flush")

    async def _cmd_run_network_diagnostics(self, params: dict[str, Any]) -> Any:
        result = await self._get("/diagnostics", timeout=90.0)
        if isinstance(result, dict):
            self.set_states(parse_diagnostics(result))
        return result

    def _require_display_control(self) -> None:
        if not self.get_state("display_control_supported"):
            raise ValueError(
                "This player has no display-control API. It is only on a Moka display "
                "with a built-in BrightSign player (BrightSignOS 9.0.189 or later)."
            )

    async def _set_display_power(self, setting: str) -> Any:
        self._require_display_control()
        result = await self._put("/display-control/power-settings", {"setting": setting})
        if isinstance(result, dict) and "setting" in result:
            self.set_state("display_power", _as_str(result.get("setting")))
        return result

    async def _cmd_display_power_on(self, params: dict[str, Any]) -> Any:
        return await self._set_display_power("on")

    async def _cmd_display_power_standby(self, params: dict[str, Any]) -> Any:
        return await self._set_display_power("standby")

    _DISPATCH = {
        "reboot": _cmd_reboot,
        "reboot_disable_autorun": _cmd_reboot_disable_autorun,
        "factory_reset": _cmd_factory_reset,
        "display_sleep": _cmd_display_sleep,
        "display_wake": _cmd_display_wake,
        "send_udp_message": _cmd_send_udp_message,
        "set_presentation_variable": _cmd_set_presentation_variable,
        "send_cec": _cmd_send_cec,
        "take_snapshot": _cmd_take_snapshot,
        "sync_time": _cmd_sync_time,
        "set_time": _cmd_set_time,
        "read_registry_key": _cmd_read_registry_key,
        "set_registry_key": _cmd_set_registry_key,
        "delete_registry_key": _cmd_delete_registry_key,
        "flush_registry": _cmd_flush_registry,
        "run_network_diagnostics": _cmd_run_network_diagnostics,
        "display_power_on": _cmd_display_power_on,
        "display_power_standby": _cmd_display_power_standby,
    }

    # ── Device settings ──

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        try:
            return await self._write_setting(key, value)
        except DwsError as exc:
            if exc.not_authorized:
                raise self._auth_fault() from exc
            self.set_state("last_error", str(exc))
            raise ValueError(str(exc)) from exc
        except httpx.TransportError as exc:
            raise ConnectionError(f"{self._host} is not responding: {exc}") from exc

    async def _write_setting(self, key: str, value: Any) -> Any:
        if key == "log_level":
            name = str(value).strip().lower()
            if name not in LOG_LEVELS:
                raise ValueError(f"log_level must be one of {', '.join(LOG_LEVELS)}")
            result = await self._put("/system/supervisor/logging", {"level": LOG_LEVELS[name]})
            await self._read_log_level()
            return result
        if not key.startswith("display_"):
            raise ValueError(f"Unknown device setting: {key}")
        self._require_display_control()
        if key in ("display_volume", "display_brightness", "display_contrast"):
            field = key[len("display_"):]
            result = await self._put(f"/display-control/{field}", {field: _as_int(value)})
            if isinstance(result, dict) and field in result:
                self.set_state(key, _as_int(result.get(field)))
            return result
        if key == "display_standby_timeout":
            result = await self._put("/display-control/standby-timeout", {"seconds": _as_int(value)})
            if isinstance(result, dict) and "seconds" in result:
                self.set_state(key, _as_int(result.get("seconds")))
            return result
        if key == "display_video_output":
            output = str(value).strip()
            if not output:
                raise ValueError("The display input cannot be blank")
            result = await self._put("/display-control/video-output", {"output": output})
            if isinstance(result, dict) and "output" in result:
                self.set_state(key, _as_str(result.get("output")))
            return result
        if key in ("display_always_on", "display_always_connected"):
            route = "always-on" if key == "display_always_on" else "always-connected"
            result = await self._put(f"/display-control/{route}", {"enable": _as_bool(value)})
            if isinstance(result, dict) and ("enable" in result or "enabled" in result):
                self.set_state(key, _as_bool(result.get("enable", result.get("enabled"))))
            return result
        if key.startswith("display_white_balance_"):
            # The display takes all three balances in one write.
            colours = {
                "redBalance": _as_int(self.get_state("display_white_balance_red")),
                "greenBalance": _as_int(self.get_state("display_white_balance_green")),
                "blueBalance": _as_int(self.get_state("display_white_balance_blue")),
            }
            colours[key[len("display_white_balance_"):] + "Balance"] = _as_int(value)
            result = await self._put("/display-control/white-balance", colours)
            if isinstance(result, dict):
                self.set_states({
                    "display_white_balance_red": _as_int(result.get("redBalance", colours["redBalance"])),
                    "display_white_balance_green": _as_int(result.get("greenBalance", colours["greenBalance"])),
                    "display_white_balance_blue": _as_int(result.get("blueBalance", colours["blueBalance"])),
                })
            return result
        raise ValueError(f"Unknown device setting: {key}")

