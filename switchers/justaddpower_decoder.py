"""
OpenAVC Just Add Power Decoder Driver (justOS HTTP API).

Controls one Just Add Power decoder (receiver) of the Omega, Ultra or
MaxColor series through justAPI, the HTTP API every justOS device serves for
itself: source switching by channel, the HDMI output (video mute, freeze,
off), the display through CEC or PlugPlayPresent, the on-screen display, the
video wall position, rotation, bezel and zoom, stereo volume and delay, RS-232
and Flux IR pass-through, EDID capture, image pull, and switching through
JPSW or JADConfig from this device. Its status, channel, resolution, HDCP,
audio format, firmware and network details are read back. Encoders have their
own driver (``justaddpower_encoder``); a Just Add Power system has no
controller, so each encoder and decoder is added as its own device.

Push vs poll
------------
Poll only. justAPI documents no subscription or event. The channel and the
device status are read every ``poll_interval`` (default 5 s); the rest every
30 s. A switch reads the channel back at once.

Why Python
----------
Most justAPI answers are one value with nothing to say what it is:
``/details/channel`` answers ``{"data": 12}``, the stereo volume
``{"data": 6}``, image pull ``{"data": true}``, a setting's write the value
written. A YAML HTTP response rule sees the body and never the path that
asked, so it cannot tell a channel from a volume. Every command answers
``{"data": "OK"}`` and never the new value, so a switch is read back with a
second request and a channel the decoder accepts and does not take is
reported as refused. In Flexible Mode a switch is one CLI command per service,
and a refusal carries a list of the models that support the command, which is
turned into a sentence.

One driver per role
-------------------
An encoder and a decoder share the API but not what the integrator controls:
a decoder chooses what it shows and drives the display, an encoder offers its
source and EDID. Every Just Add Power unit is one or the other, so each role
has its own driver and a project built before the equipment arrives shows
only the commands that unit has. The driver refuses a device that reports
itself as an encoder.

Protocol
--------
``http://<device>/cgi-bin/api/<uri>``, no sign-in. ``GET`` of a details or
settings URI answers ``{"data": <value>}``; a parent URI answers its children
as one object. ``POST`` of a command takes a JSON body (a bare number, a
quoted string, an object or an array) and answers ``{"data": "OK"}``, or
``{"error": [{"details", "requires", "uri"}]}`` with HTTP 400. ``false`` means
a value that is off or unavailable, ``null`` one this hardware lacks. A
setting needs a ``save`` to be kept and takes effect when the device restarts.
Requests are sent one at a time.

Switching. A decoder shows the encoder whose channel (0-9999) it is set to.
In Unified Mode ``POST /command/channel`` with the bare number moves audio,
video and USB together. In Flexible Mode (how AMP 1.6.0 and later set up a
new system) that command is not supported and each service has its own
channel, set with the CLI commands ``channel -v N``, ``channel -a N`` and
``channel -u N``; this driver sends them through ``/command/cli``. The
documents give no read of the per-service channels, so in Flexible Mode the
driver reports only the channel ``/details/channel`` answers.

Sources:
    justOS HTTP API v1.1.2 (OpenAPI 3.0, Just Add Power):
    https://app.swaggerhub.com/apis-docs/JustAddPower/justOS-HTTP-API/1.1.2
    justOS HTTP API Overview, KB 466 (supported series, firmware minimums):
    https://support.justaddpower.com/kb/article/466-justos-http-api-overview-omega-ultra-and-maxcolor-series/
    Multicast Switching Protocol, KB 346 (Unified and Flexible Mode):
    https://support.justaddpower.com/kb/article/346-multicast-switching-protocol/
    JPSW switching, KB 434; CLI commands, KB 379 (USB connect and stop).
    Serial over IP, KB 473 (Direct mode, TCP 6752, MaxColor link requirement):
    https://support.justaddpower.com/kb/article/473-how-to-use-serial-over-ip-omega-ultra-and-maxcolor-series/
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.utils.logger import get_logger

try:
    from openavc.drivers.base import CommandRefusedError
except ImportError:  # OpenAVC before 0.37.1
    CommandRefusedError = RuntimeError

log = get_logger(__name__)

API = "/cgi-bin/api"
CORE = "/details/device"
SLOW_INTERVAL_S = 30.0
READ_BACK_RETRY_S = 1.0
ROLE = "decoder"
OTHER_DRIVER = "Just Add Power Encoder (justOS)"

# uri -> (path inside "data", state key, kind). An empty path is the bare value.
READS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "/details/channel": (("", "channel", "int"),),
    "/details/device/status": (("", "status", "str"),),
    CORE: (
        ("model", "model", "str"),
        ("status", "status", "str"),
        ("uptime", "uptime", "str"),
        ("firmware.version", "firmware_version", "str"),
        ("firmware.update.status", "firmware_updating", "bool"),
        ("firmware.update.progress", "firmware_update_progress", "int"),
        ("firmware.update.message", "firmware_update_message", "str"),
        ("network.ipaddress", "ip_address", "str"),
        ("network.mac", "mac_address", "str"),
        ("network.speed", "link_speed", "str"),
    ),
    "/details/video": (
        ("resolution", "resolution", "str"),
        ("scaler", "scaler", "str"),
        ("hdcp", "hdcp", "str"),
        ("hdr", "hdr", "hdr"),
    ),
    "/details/audio": (
        ("hdmi.status", "hdmi_audio", "bool"),
        ("hdmi.format", "hdmi_audio_format", "str"),
        ("hdmi.channels", "hdmi_audio_channels", "str"),
        ("hdmi.sampleFrequency", "hdmi_audio_sample_rate", "str"),
        ("hdmi.sampleSize", "hdmi_audio_sample_size", "str"),
        ("stereo.delay", "audio_delay", "int"),
    ),
    "/details/audio/stereo/volume": (("", "volume", "int"),),
    "/details/videowall/layout": (
        ("totalRows", "wall_rows", "int"),
        ("totalColumns", "wall_columns", "int"),
        ("rowPosition", "wall_row", "int"),
        ("columnPosition", "wall_column", "int"),
    ),
    "/details/imagepull": (("", "image_pull", "bool"),),
    "/settings/device": (
        ("name", "device_name", "str"),
        ("id", "device_id", "str"),
        ("leds", "leds", "leds"),
    ),
    "/settings/videowall/rotate": (("clockwise", "wall_rotation", "int"),),
    "/settings/serial": (
        ("mode", "serial_mode", "str"),
        ("speed.baud", "serial_baud", "int"),
        ("speed.databits", "serial_databits", "int"),
        ("speed.parity", "serial_parity", "str"),
        ("speed.stopbits", "serial_stopbits", "int"),
    ),
}
FAST = ("/details/channel", "/details/device/status")
SLOW = (
    CORE, "/details/video", "/details/audio", "/details/audio/stereo/volume",
    "/details/videowall/layout", "/details/imagepull", "/settings/device",
    "/settings/videowall/rotate", "/settings/serial",
)
LABELS = {
    "/details/channel": "the channel",
    "/details/device/status": "the status",
    "/details/video": "the video details",
    "/details/audio": "the audio details",
    "/details/audio/stereo/volume": "the stereo volume",
    "/details/videowall/layout": "the video wall layout",
    "/details/imagepull": "image pull",
    "/settings/device": "the device settings",
    "/settings/videowall/rotate": "the video wall rotation",
    "/settings/serial": "the serial settings",
}

# command -> (uri, body) for commands that send one fixed body.
FIXED: dict[str, tuple[str, Any, str]] = {
    "video_mute": ("/command/video", "mute", "Video Mute"),
    "video_unmute": ("/command/video", "unmute", "Video Unmute"),
    "video_freeze": ("/command/video", "pause", "Freeze"),
    "video_play": ("/command/video", "play", "Unfreeze"),
    "hdmi_output_off": ("/command/video", "off", "HDMI Output Off"),
    "hdmi_output_on": ("/command/video", "on", "HDMI Output On"),
    "stream_stop": ("/command/video", "stop", "Stop Stream"),
    "stream_start": ("/command/video", "start", "Start Stream"),
    "tv_on": ("/command/cec/tv", "on", "TV On (CEC)"),
    "tv_off": ("/command/cec/tv", "off", "TV Off (CEC)"),
    "display_on": ("/command/ppp", {"power": "on"}, "Display On (PlugPlayPresent)"),
    "display_off": ("/command/ppp", {"power": "off"}, "Display Off (PlugPlayPresent)"),
    "identify_on": ("/command/osd", {"identify": "on"}, "Identify On"),
    "identify_off": ("/command/osd", {"identify": "off"}, "Identify Off"),
    "capture_edid": ("/command/edid", "capture", "Capture Display EDID"),
    "save_settings": ("/command/device", "save", "Save Settings"),
    "reboot": ("/command/device", "reboot", "Reboot"),
}
# Flexible Mode CLI service letters.
SERVICES = {"switch_video": ("v", "Switch Video"), "switch_audio": ("a", "Switch Audio"), "switch_usb": ("u", "Switch USB")}
HEX_BYTES = re.compile(r"^[0-9A-Fa-f]{2}( +[0-9A-Fa-f]{2})*$")
NO_BODY = object()

# device setting -> (uri, kind, read-back uri)
SETTINGS = {
    "device_name": ("/settings/device/name", "str", "/settings/device"),
    "leds": ("/settings/device/leds", "leds", "/settings/device"),
}


def _field(payload: Any, path: str) -> Any:
    """The value at a dotted path. A parent that is not an object (false: off
    or unavailable) ends the walk with that value."""
    value = payload
    for part in [p for p in path.split(".") if p]:
        if not isinstance(value, dict):
            return value
        value = value.get(part)
    return value


def _read_value(raw: Any, kind: str) -> Any:
    """A justAPI value as state. None means do not write (null: this hardware
    has no such value)."""
    if raw is None:
        return None
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("true", "on", "1", "yes")
    if kind == "int":
        if isinstance(raw, bool):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    if kind == "hdr":
        return "off" if raw is False else str(raw).strip()
    if kind == "leds":
        return "off" if raw is False else str(raw).strip()
    if raw is False:
        return ""
    if isinstance(raw, (dict, list)):
        return None
    return str(raw).strip()


def _write_value(value: Any, kind: str) -> Any:
    if kind in ("leds", "hdr"):
        return False if str(value) == "off" else str(value)
    return str(value)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _role_of(details: Any) -> str | None:
    """encoder or decoder, from what the device reports; None when it does
    not say."""
    if not isinstance(details, dict):
        return None
    mode = str(details.get("mode") or "").strip().lower()
    if mode.startswith("transmit") or mode.startswith("encod"):
        return "encoder"
    if mode.startswith("receiv") or mode.startswith("decod"):
        return "decoder"
    model = str(details.get("model") or "")
    if re.search(r"\bTX\b", model):
        return "encoder"
    if re.search(r"\bRX\b", model):
        return "decoder"
    return None


def _error_text(payload: dict[str, Any]) -> str:
    """The device's refusal as a sentence fragment."""
    err = payload.get("error")
    if isinstance(err, list):
        parts = []
        for entry in err:
            if isinstance(entry, dict):
                text = str(entry.get("details") or "").strip()
                needs = entry.get("requires")
                if isinstance(needs, list) and needs:
                    text = f"{text}; it works on {', '.join(str(n) for n in needs)}" if text else (
                        f"it works on {', '.join(str(n) for n in needs)}"
                    )
                if text:
                    parts.append(text)
            elif entry not in (None, ""):
                parts.append(str(entry))
        return "; ".join(parts) or "an error"
    if isinstance(err, dict):
        return str(err.get("details") or err) or "an error"
    return str(err) if err not in (None, "") else "an error"


class DeviceRefused(Exception):
    def __init__(self, message: str, *, http_status: int = 0) -> None:
        super().__init__(message)
        self.http_status = http_status


def _serial_setup_problem(data: Any, params: dict[str, Any] | None) -> str:
    """Check reported settings, not whether saved settings have taken effect."""
    if not isinstance(data, dict):
        return "Serial settings unavailable. Check Comms > Serial Settings in the device web page."
    if data.get("mode") != "direct":
        return "Select Direct (TCP:6752) in Comms > Serial Settings, then apply, save and reboot if prompted."
    if params is None:
        return ""
    if params.get("flow_control") not in (None, "none"):
        return "Use Flow control None. The documented Direct serial bridge does not specify hardware flow control."
    parity = str(params.get("parity", "N")).upper()
    expected = {
        "baud": int(params.get("baudrate", 9600)),
        "databits": int(params.get("bytesize", 8)),
        "parity": {"N": "none", "E": "even", "O": "odd"}.get(parity, parity),
        "stopbits": float(params.get("stopbits", 1)),
    }
    speed = data.get("speed")
    if not isinstance(speed, dict):
        return "Serial speed unavailable. Match Comms > Serial Settings to the connected equipment."
    mismatches = [f"{key}: needs {value}, reports {speed.get(key)}" for key, value in expected.items() if speed.get(key) != value]
    if mismatches:
        return "Match Comms > Serial Settings: " + "; ".join(mismatches) + ". Apply, save and reboot if prompted."
    return ""


class JustAddPowerDecoderDriver(BaseDriver):
    """One Just Add Power justOS decoder over justAPI (HTTP)."""

    DRIVER_INFO = {
        "id": "justaddpower_decoder",
        "name": "Just Add Power Decoder (justOS)",
        "manufacturer": "Just Add Power",
        "category": "switcher",
        "version": "1.1.0",
        "min_platform_version": "0.36.0",
        "author": "OpenAVC",
        "description": (
            "Controls a Just Add Power Omega, Ultra or MaxColor decoder over justAPI: "
            "source switching by channel in Unified or Flexible Mode, video mute and "
            "freeze, the display through CEC or PlugPlayPresent, the on-screen display, "
            "video wall layout, rotation, bezel and zoom, stereo volume and delay, "
            "RS-232 and IR pass-through, and JPSW switching. Reads back the channel, "
            "status, resolution, HDCP, audio format and firmware."
        ),
        "source_url": "https://app.swaggerhub.com/apis-docs/JustAddPower/justOS-HTTP-API/1.1.2",
        "tags": [
            "just-add-power", "jap", "justos", "av-over-ip", "hdmi-over-ip", "decoder",
            "receiver", "video-wall", "multicast", "omega", "ultra", "maxcolor", "serial-bridge", "rs232",
        ],
        "verified": False,
        "simulated": True,
        "protocols": ["justaddpower_justapi"],
        "ports": [80],
        "transport": "http",
        "bridge": {"ports": [{"id": "serial:1", "kind": "serial", "passthrough_port": 6752, "label": "RS-232 (Direct mode)"}]},
        "discovery": {
            # justAPI answers without a sign-in. A decoder's model ends in RX
            # ("3G+AVP RX"), an encoder's in TX. MaxColor's model words are
            # not in the API document. No oui: the API document's MACs are
            # locally administered (c2:00:00:...) and the IEEE registries list
            # no Just Add Power block.
            "tcp_probe": {
                "port": 80,
                "send_ascii": "GET /cgi-bin/api/details/device/model HTTP/1.0\r\n\r\n",
                "expect_regex": r'"data"\s*:\s*"[^"]*\bRX"',
                "timeout_ms": 3000,
                "extract": {
                    "model": {"regex": r'"data"\s*:\s*"([^"]*\bRX)"'},
                },
            },
            "manufacturer_alias": ["Just Add Power", "JustAddPower", "J+P"],
        },
        "compatible_models": [
            {
                "manufacturer": "Just Add Power",
                "models": [
                    "Ultra Series decoders (3G RX, 3G+4+ RX, 3G+AVP RX)",
                    "Omega Series decoders (2G/3G RX, 2G/3G+ RX)",
                ],
                "confidence": "untested",
                "notes": (
                    "justOS firmware B1.1.0 or later for justAPI; B2.1.0 or later for "
                    "Multicast Switching. Not in a 2G Series system (firmware A6.5.9), "
                    "where justAPI is unsupported. Not yet tested on this hardware."
                ),
            },
            {
                "manufacturer": "Just Add Power",
                "models": ["MaxColor Series decoders"],
                "confidence": "untested",
                "notes": (
                    "MaxColor firmware MAX v3.5.2 or later. The API document does not "
                    "list MaxColor's model names. Not yet tested on this hardware."
                ),
            },
        ],
        "help": {
            "overview": (
                "Controls one Just Add Power decoder: which encoder it shows (by "
                "channel), video mute and freeze, the display it drives (CEC or "
                "PlugPlayPresent), the on-screen display, its place in a video wall, "
                "stereo audio, and RS-232 and IR pass-through. Add each decoder as "
                "its own device, and each encoder with the Just Add Power Encoder "
                "(justOS) driver. To switch several decoders from one panel, set up "
                "a Matrix with each decoder's Channel as a destination and the "
                "encoders' channel numbers as sources."
            ),
            "setup": (
                "1. Update the decoder to justOS B2.1.0 or later (MaxColor: MAX "
                "v3.5.2 or later).\n"
                "2. Enter the decoder's IP address. justAPI needs no password.\n"
                "3. Set Channel Mode to match the system: Flexible for a Multicast "
                "Switching system set up with AMP 1.6.0 or later (its default), "
                "Unified for an older one or one converted to Unified Mode. If "
                "Switch Source is refused, the mode is the first thing to check.\n"
                "4. To control equipment through RS-232, open the decoder's web page, "
                "Comms > Serial Settings. Select Direct (TCP:6752), match the equipment's "
                "baud rate, data bits, parity and stop bits, then apply, save and reboot if prompted. "
                "Check the serial cable pinout and null modem jumper. MaxColor needs an encoder "
                "and decoder linked before Direct mode is available.\n"
                "5. Add the connected equipment with its own serial driver. Choose Through a bridge, "
                "this decoder, and RS-232 (Direct mode). Allow TCP 6752 from OpenAVC to the decoder. "
                "The equipment's driver handles commands and feedback. Serial Bridge Setup shows "
                "setting mismatches; it does not test the cable or confirm settings have taken effect.\n"
                "6. For one-way Send RS-232 commands instead, select API serial mode. "
                "API mode cannot be used at the same time as the Direct serial bridge."
            ),
        },
        "default_config": {
            "host": "",
            "port": 80,
            "channel_mode": "unified",
            "poll_interval": 5,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {
                "type": "integer",
                "default": 80,
                "min": 1,
                "max": 65535,
                "label": "Port",
                "description": "justAPI's HTTP port (80).",
            },
            "channel_mode": {
                "type": "enum",
                "values": ["unified", "flexible"],
                "default": "unified",
                "label": "Channel Mode",
                "description": (
                    "unified: audio, video and USB share one channel and Switch Source "
                    "moves them together. flexible: each has its own channel (AMP 1.6.0 "
                    "and later set up new systems this way); Switch Source moves video "
                    "and audio, and Switch Video, Audio and USB move one."
                ),
            },
            "poll_interval": {
                "type": "integer",
                "default": 5,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "How often the channel and status are read. The rest is read every "
                    "30 seconds."
                ),
            },
        },
        "state_variables": {
            "channel": {
                "type": "integer",
                "min": 0,
                "max": 9999,
                "label": "Channel",
                "help": (
                    "The channel this decoder is set to: it shows the encoder sending "
                    "on this channel. In Flexible Mode, the channel the decoder reports; "
                    "it does not report video, audio and USB separately."
                ),
                "control": True,
                "cloud_priority": "high",
            },
            "status": {
                "type": "string",
                "label": "Status",
                "help": (
                    "What the decoder is doing, in its own words, e.g. Decoding Video, "
                    "Searching for Transmitter, Services Stopped."
                ),
                "cloud_priority": "high",
            },
            "resolution": {"type": "string", "label": "Output Resolution", "help": "The resolution the decoder sends to the display, e.g. 1920x1080p@60Hz."},
            "scaler": {"type": "string", "label": "Scaler", "help": "The decoder's scaler setting."},
            "hdcp": {"type": "string", "label": "HDCP", "help": "The HDCP version the content needs (1.x or 2.2)."},
            "hdr": {"type": "enum", "values": ["passthru", "off"], "label": "HDR", "help": "passthru while HDR passes through."},
            "hdmi_audio": {"type": "boolean", "label": "HDMI Audio", "help": "Whether HDMI audio is present."},
            "hdmi_audio_format": {"type": "string", "label": "HDMI Audio Format", "help": "e.g. LPCM."},
            "hdmi_audio_channels": {"type": "string", "label": "HDMI Audio Channels"},
            "hdmi_audio_sample_rate": {"type": "string", "label": "HDMI Audio Sample Rate", "help": "e.g. 48KHz."},
            "hdmi_audio_sample_size": {"type": "string", "label": "HDMI Audio Sample Size", "help": "e.g. 24bits."},
            "volume": {
                "type": "integer", "min": 0, "max": 11, "step": 1, "label": "Stereo Volume",
                "help": "The analog stereo output level, 0 to 11, relative to the HDMI level.",
                "control": True,
            },
            "audio_delay": {
                "type": "integer", "min": 0, "max": 175, "step": 1, "unit": "ms", "label": "Stereo Delay",
                "help": "The analog stereo output delay.",
            },
            "wall_rows": {"type": "integer", "min": 1, "label": "Wall Rows", "help": "Rows in the video wall this decoder is part of (1 when not in a wall)."},
            "wall_columns": {"type": "integer", "min": 1, "label": "Wall Columns"},
            "wall_row": {"type": "integer", "min": 1, "label": "Wall Row", "help": "This decoder's row in the wall, from 1 at the top."},
            "wall_column": {"type": "integer", "min": 1, "label": "Wall Column", "help": "This decoder's column in the wall, from 1 at the left."},
            "wall_rotation": {"type": "integer", "label": "Rotation", "help": "Clockwise rotation of the output: 0, 90, 180 or 270."},
            "image_pull": {"type": "boolean", "label": "Image Pull", "help": "Whether image pull is running."},
            "device_name": {"type": "string", "label": "Device Name"},
            "device_id": {"type": "string", "label": "Device ID", "help": "The decoder's ID in the system, e.g. RX12."},
            "leds": {"type": "enum", "values": ["default", "video", "off"], "label": "LEDs"},
            "model": {"type": "string", "label": "Model"},
            "firmware_version": {"type": "string", "label": "Firmware"},
            "firmware_updating": {"type": "boolean", "label": "Firmware Updating"},
            "firmware_update_progress": {"type": "integer", "min": 0, "max": 100, "unit": "%", "label": "Update Progress"},
            "firmware_update_message": {"type": "string", "label": "Update Message"},
            "ip_address": {"type": "string", "label": "IP Address"},
            "mac_address": {"type": "string", "label": "MAC Address"},
            "link_speed": {"type": "string", "label": "Link Speed", "help": "The Ethernet port's speed: 10M, 100M or 1G."},
            "uptime": {"type": "string", "label": "Uptime", "cloud_priority": "low"},
            "serial_response": {"type": "string", "label": "Serial API Result", "help": "The HTTP API result for Send RS-232. This is not confirmed feedback from the connected equipment; use the Direct serial bridge for feedback."},
            "serial_mode": {"type": "string", "label": "Serial Mode (settings)", "help": "direct for the two-way bridge, api for one-way Send RS-232, passive for encoder-to-decoder serial extension. Settings may need a save and reboot."},
            "serial_baud": {"type": "integer", "label": "Serial Baud Rate (settings)"},
            "serial_databits": {"type": "integer", "label": "Serial Data Bits (settings)"},
            "serial_parity": {"type": "string", "label": "Serial Parity (settings)"},
            "serial_stopbits": {"type": "integer", "label": "Serial Stop Bits (settings)"},
            "serial_bridge_setup": {"type": "string", "label": "Serial Bridge Setup", "help": "Commissioning advice from the reported serial settings and the most recently prepared downstream driver. This check does not block its connection or prove serial communication. MaxColor also needs an encoder and decoder linked."},
            "flux_response": {"type": "string", "label": "IR Dongle Response", "help": "What the Flux Capacitor IR dongle answered to the last command."},
            "cli_response": {"type": "string", "label": "CLI Response", "help": "What the decoder printed for the last Send CLI Command."},
            "last_error": {
                "type": "string",
                "label": "Last Error",
                "help": "A value the decoder did not answer on the last poll; cleared by the next clean poll.",
            },
        },
        "commands": {
            "switch_source": {
                "label": "Switch Source",
                "help": (
                    "Shows the encoder on this channel. Unified Mode: audio, video and USB "
                    "move together. Flexible Mode: video and audio move."
                ),
                "params": {
                    "channel": {"type": "integer", "min": 0, "max": 9999, "required": True, "label": "Channel"},
                },
                "sets": {"channel": "{channel}"},
            },
            "switch_video": {
                "label": "Switch Video",
                "help": "Flexible Mode: shows video from the encoder on this channel, leaving audio and USB where they are.",
                "params": {
                    "channel": {"type": "integer", "min": 0, "max": 9999, "required": True, "label": "Channel"},
                },
            },
            "switch_audio": {
                "label": "Switch Audio",
                "help": "Flexible Mode: plays audio from the encoder on this channel, leaving video and USB where they are.",
                "params": {
                    "channel": {"type": "integer", "min": 0, "max": 9999, "required": True, "label": "Channel"},
                },
            },
            "switch_usb": {
                "label": "Switch USB",
                "help": "Flexible Mode: connects USB to the encoder on this channel, leaving video and audio where they are.",
                "params": {
                    "channel": {"type": "integer", "min": 0, "max": 9999, "required": True, "label": "Channel"},
                },
            },
            "usb_connect": {
                "label": "USB Connect",
                "help": "Connects the decoder's USB ports to the encoder (KVM). On MaxColor, to the encoder it is showing.",
                "params": {},
            },
            "usb_disconnect": {"label": "USB Disconnect", "help": "Disconnects the USB connection.", "params": {}},
            "video_mute": {"label": "Video Mute", "help": "Sends a black picture to the display at the same resolution.", "params": {}},
            "video_unmute": {"label": "Video Unmute", "help": "Shows the picture again after Video Mute.", "params": {}},
            "video_freeze": {"label": "Freeze", "help": "Holds the current frame on the display; audio carries on.", "params": {}},
            "video_play": {"label": "Unfreeze", "help": "Shows the live picture again after Freeze.", "params": {}},
            "hdmi_output_off": {"label": "HDMI Output Off", "help": "Turns the HDMI output off: the display sees no signal.", "params": {}},
            "hdmi_output_on": {"label": "HDMI Output On", "help": "Turns the HDMI output back on.", "params": {}},
            "stream_stop": {"label": "Stop Stream", "help": "Stops the decoder's network services; the display shows the decoder's status screen.", "params": {}},
            "stream_start": {"label": "Start Stream", "help": "Starts the decoder's network services again.", "params": {}},
            "tv_on": {"label": "TV On (CEC)", "help": "Turns the display on over HDMI CEC.", "params": {}},
            "tv_off": {"label": "TV Off (CEC)", "help": "Turns the display off over HDMI CEC.", "params": {}},
            "cec_send": {
                "label": "Send CEC",
                "help": "Sends these bytes on the HDMI CEC bus to the display.",
                "params": {
                    "bytes": {
                        "type": "string", "required": True, "label": "Bytes",
                        "pattern": "^[0-9A-Fa-f]{2}( +[0-9A-Fa-f]{2})*$",
                        "help": "Hex bytes separated by spaces, e.g. EF 82 10 00.",
                    },
                },
            },
            "display_on": {
                "label": "Display On (PlugPlayPresent)",
                "help": "Sends the display power-on command set up in PlugPlayPresent for this decoder.",
                "params": {},
            },
            "display_off": {
                "label": "Display Off (PlugPlayPresent)",
                "help": "Sends the display power-off command set up in PlugPlayPresent for this decoder.",
                "params": {},
            },
            "osd_text": {
                "label": "Show On-Screen Text",
                "help": "Shows text over the picture on this decoder's display.",
                "params": {
                    "text": {"type": "string", "required": True, "label": "Text"},
                    "timeout": {"type": "integer", "min": 0, "label": "Timeout", "help": "How long the text stays up."},
                    "size": {"type": "integer", "min": 1, "label": "Size"},
                    "color": {"type": "string", "pattern": "^[0-9A-Fa-f]{6}$", "label": "Color", "help": "RGB hex, e.g. FFFFFF."},
                    "opacity": {"type": "integer", "min": 0, "max": 100, "label": "Opacity", "unit": "%"},
                    "align": {"type": "enum", "values": ["left", "right"], "label": "Align"},
                },
            },
            "identify_on": {
                "label": "Identify On",
                "help": "Shows each decoder's IP and MAC address on its display, on every decoder on the network.",
                "params": {},
            },
            "identify_off": {
                "label": "Identify Off",
                "help": "Clears the identify text from every decoder on the network.",
                "params": {},
            },
            "videowall_layout": {
                "label": "Set Video Wall Position",
                "help": "Puts this decoder in a wall of this many rows and columns, at this row and column. Saved on the decoder.",
                "params": {
                    "rows": {"type": "integer", "min": 1, "required": True, "label": "Rows", "default": 1},
                    "columns": {"type": "integer", "min": 1, "required": True, "label": "Columns", "default": 1},
                    "row": {"type": "integer", "min": 1, "required": True, "label": "Row", "default": 1},
                    "column": {"type": "integer", "min": 1, "required": True, "label": "Column", "default": 1},
                },
                "sets": {"wall_rows": "{rows}", "wall_columns": "{columns}", "wall_row": "{row}", "wall_column": "{column}"},
            },
            "videowall_rotate": {
                "label": "Rotate Output",
                "help": "Rotates the output clockwise. Saved on the decoder.",
                "params": {
                    "degrees": {"type": "enum", "values": ["0", "90", "180", "270"], "required": True, "label": "Degrees"},
                },
            },
            "videowall_bezel": {
                "label": "Set Bezel Compensation",
                "help": (
                    "Hides the picture behind the display's bezel, from the display's "
                    "outer and picture width and height in any one unit. Saved on the decoder."
                ),
                "params": {
                    "outer_width": {"type": "integer", "min": 1, "max": 65535, "required": True, "label": "Outer Width"},
                    "outer_height": {"type": "integer", "min": 1, "max": 65535, "required": True, "label": "Outer Height"},
                    "video_width": {"type": "integer", "min": 1, "max": 65535, "required": True, "label": "Picture Width"},
                    "video_height": {"type": "integer", "min": 1, "max": 65535, "required": True, "label": "Picture Height"},
                },
            },
            "videowall_zoom_horizontal": {
                "label": "Zoom Horizontal",
                "help": "Zooms the output horizontally (0 is no zoom). Saved on the decoder.",
                "params": {"amount": {"type": "integer", "min": 0, "required": True, "label": "Amount"}},
            },
            "videowall_zoom_vertical": {
                "label": "Zoom Vertical",
                "help": "Zooms the output vertically (0 is no zoom). Saved on the decoder.",
                "params": {"amount": {"type": "integer", "min": 0, "required": True, "label": "Amount"}},
            },
            "set_volume": {
                "label": "Set Stereo Volume",
                "help": "Sets the analog stereo output level (3G+AVP RX).",
                "params": {"level": {"type": "integer", "min": 0, "max": 11, "required": True, "label": "Level"}},
                "sets": {"volume": "{level}"},
            },
            "set_audio_delay": {
                "label": "Set Stereo Delay",
                "help": "Delays the analog stereo output by this many milliseconds.",
                "params": {"ms": {"type": "integer", "min": 0, "max": 175, "required": True, "label": "Delay (ms)"}},
                "sets": {"audio_delay": "{ms}"},
            },
            "capture_edid": {
                "label": "Capture Display EDID",
                "help": "Reads the display's EDID and sends it to every encoder this decoder can reach.",
                "params": {},
                "confirm": (
                    "Copies this display's EDID to every encoder this decoder can reach, "
                    "replacing the EDID they offer their sources."
                ),
            },
            "serial_send": {
                "label": "Send RS-232",
                "help": (
                    "Sends this text out of the decoder's RS-232 port, with a carriage return "
                    "before and after. Needs serial mode api. For equipment feedback, use its "
                    "own driver Through a bridge with the decoder in Direct mode."
                ),
                "params": {"text": {"type": "string", "required": True, "label": "Text", "trim": False}},
            },
            "serial_send_hex": {
                "label": "Send RS-232 (Hex)",
                "help": "Sends these bytes out of the RS-232 port exactly. Needs serial mode api. For equipment feedback, use the Direct serial bridge.",
                "params": {
                    "bytes": {
                        "type": "string", "required": True, "label": "Bytes",
                        "pattern": "^[0-9A-Fa-f]{2}( +[0-9A-Fa-f]{2})*$",
                        "help": "Hex bytes separated by spaces. A 0A is followed by 0D.",
                    },
                },
            },
            "flux_send": {
                "label": "Send IR Dongle Command",
                "help": "Sends this command to a Flux Capacitor IR dongle on the decoder; the reply goes to IR Dongle Response.",
                "params": {"text": {"type": "string", "required": True, "label": "Command"}},
            },
            "start_image_pull": {
                "label": "Start Image Pull",
                "help": "Starts image pull now with these values; the decoder's start-up image pull settings are left alone.",
                "params": {
                    "width": {"type": "integer", "min": 0, "max": 640, "required": True, "label": "Width", "default": 320},
                    "frequency": {"type": "integer", "min": 0, "required": True, "label": "Frequency", "default": 3},
                    "priority": {"type": "enum", "values": ["low", "high"], "required": True, "label": "Priority", "default": "low"},
                },
            },
            "jpsw_switch": {
                "label": "JPSW Switch",
                "help": (
                    "AMP Standardized systems: switches these decoders to this encoder. "
                    "Works from any device in the system."
                ),
                "params": {
                    "decoders": {
                        "type": "string", "required": True, "label": "Decoders",
                        "pattern": "^(all|[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*)$",
                        "help": "A number, a list (1,3,5), a range (6-9), both (1,6-9), or all.",
                    },
                    "encoder": {"type": "integer", "min": 1, "required": True, "label": "Encoder"},
                },
            },
            "jadconfig_switch": {
                "label": "JADConfig Switch",
                "help": "JADConfig VLAN systems: switches these receivers to this transmitter.",
                "params": {
                    "receivers": {
                        "type": "string", "required": True, "label": "Receivers",
                        "pattern": "^[0-9]+(,[0-9]+)*$", "help": "Receiver numbers separated by commas.",
                    },
                    "transmitter": {"type": "integer", "min": 1, "required": True, "label": "Transmitter"},
                },
            },
            "send_cli": {
                "label": "Send CLI Command",
                "help": "Runs a command on the decoder's command line; what it prints goes to CLI Response.",
                "params": {"command": {"type": "string", "required": True, "label": "Command"}},
            },
            "save_settings": {"label": "Save Settings", "help": "Saves the decoder's settings so they are kept when it restarts.", "params": {}},
            "reboot": {"label": "Reboot", "help": "Restarts the decoder; its display loses the picture until it is back.", "params": {}},
        },
        "device_settings": {
            "device_name": {
                "type": "string", "label": "Device Name", "state_key": "device_name", "default": "", "setup": False,
                "help": "The decoder's name. Saved on the decoder.",
            },
            "leds": {
                "type": "enum", "label": "LEDs", "state_key": "leds", "values": ["default", "video", "off"],
                "default": "default", "setup": False,
                "help": "What the decoder's LEDs show. Saved on the decoder; takes effect when it next restarts.",
            },
        },
        "quick_actions": ["video_mute", "video_unmute", "tv_on", "tv_off"],
        "actions": [
            {"id": "video_mute", "kind": "command", "icon": "eye-off"},
            {"id": "video_unmute", "kind": "command", "icon": "eye"},
            {"id": "tv_on", "kind": "command", "icon": "power"},
            {"id": "tv_off", "kind": "command", "icon": "power-off"},
        ],
    }

    HEALTH_FAULT_MESSAGE = "The decoder stopped answering justAPI requests."

    def __init__(self, device_id: str, config: dict[str, Any], state: Any, events: Any) -> None:
        super().__init__(device_id, config, state, events)
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._slow_due = 0.0
        # URIs this device answered 404 or null; not asked again on this connection.
        self._unsupported: set[str] = set()
        # Reads the device refused, by URI: named in Last Error by every poll
        # until the URI answers again.
        self._refused: dict[str, str] = {}
        self._serial_params: dict[str, Any] | None = None

    # ── Config accessors ──

    @property
    def _host(self) -> str:
        return str(self.config.get("host", "") or "").strip()

    @property
    def _port(self) -> int:
        return _as_int(self.config.get("port"), 80) or 80

    @property
    def _flexible(self) -> bool:
        return str(self.config.get("channel_mode", "unified") or "unified").strip().lower() == "flexible"

    # ── Connection lifecycle ──

    async def _create_transport(self, transport_type: str) -> None:
        if not self._host:
            raise ConnectionFaultError("No IP address is entered.", code="invalid_config")
        if not await self._verify_reachable(self._host, self._port):
            raise ConnectionError(f"{self._host}:{self._port} is not responding")
        self._client = httpx.AsyncClient(
            base_url=f"http://{self._host}:{self._port}",
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=False,
        )

    async def _post_connect(self) -> None:
        self._unsupported = set()
        self._refused = {}
        self._slow_due = 0.0
        try:
            details = await self._request("GET", CORE)
        except DeviceRefused as exc:
            if exc.http_status in (0, 200, 404) or 300 <= exc.http_status < 400:
                raise ConnectionFaultError(
                    f"The device at {self._host} answered, but not with justAPI ({exc}). "
                    "Check the address, and that the device runs justOS.",
                    code="invalid_config",
                ) from exc
            raise ConnectionError(f"The decoder answered its details read with {exc}.") from exc
        role = _role_of(details)
        if role is not None and role != ROLE:
            model = str(details.get("model") or "").strip() if isinstance(details, dict) else ""
            what = f" ({model})" if model else ""
            raise ConnectionFaultError(
                f"This is a Just Add Power {role}{what}. Add it with the {OTHER_DRIVER} driver.",
                code="invalid_config",
            )
        self._apply(CORE, details)
        log.info(f"[{self.device_id}] Connected to Just Add Power decoder at {self._host}:{self._port}")

    async def _initial_sync(self) -> None:
        await self.poll()

    def _link_alive(self) -> bool:
        return self._client is not None

    async def _close_session(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _liveness_probe(self) -> None:
        # The platform holds the deadline: on a reconnect it waits up to
        # HEALTH_INTERVAL_S for a device still starting up.
        try:
            await self._request("GET", "/details/device/firmware/version", timeout=self.HEALTH_INTERVAL_S)
        except DeviceRefused:
            return  # it answered: the link is up

    # ── HTTP plumbing ──

    async def _request(self, method: str, uri: str, body: Any = NO_BODY, timeout: float | None = None) -> Any:
        """One request under the lock; returns the answer's ``data``. A refusal
        raises DeviceRefused with the device's reason; a dead link raises
        ConnectionError."""
        client = self._client
        if client is None:
            raise ConnectionError("Not connected")
        kwargs: dict[str, Any] = {}
        if body is not NO_BODY:
            kwargs["content"] = json.dumps(body)
            kwargs["headers"] = {"Content-Type": "application/json"}
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(timeout, connect=5.0)
        async with self._lock:
            try:
                resp = await client.request(method, API + uri, **kwargs)
            except httpx.TransportError as exc:
                raise ConnectionError(f"{self._host} is not responding: {exc}") from exc
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict) and "error" in payload:
            raise DeviceRefused(_error_text(payload), http_status=resp.status_code)
        if not 200 <= resp.status_code < 300:
            raise DeviceRefused(f"HTTP {resp.status_code}", http_status=resp.status_code)
        if not isinstance(payload, dict) or "data" not in payload:
            raise DeviceRefused("an answer that is not justAPI's JSON", http_status=resp.status_code)
        return payload["data"]

    # ── Reads ──

    async def _read(self, uri: str) -> Any:
        """Read one URI into state; None when this hardware has no such value
        (it is not asked again on this connection)."""
        if uri in self._unsupported:
            return None
        try:
            data = await self._request("GET", uri)
        except DeviceRefused as exc:
            if exc.http_status == 404 and uri != CORE:
                log.info(f"[{self.device_id}] This decoder has no {uri}; not asked again")
                self._unsupported.add(uri)
                self._refused.pop(uri, None)
                return None
            raise
        self._refused.pop(uri, None)
        if data is None:
            self._unsupported.add(uri)
            return None
        self._apply(uri, data)
        return data

    def _apply(self, uri: str, data: Any) -> None:
        updates: dict[str, Any] = {}
        for path, key, kind in READS.get(uri, ()):
            value = _read_value(_field(data, path), kind)
            if value is not None:
                updates[key] = value
        if updates:
            self.set_states(updates)
        if uri == "/settings/serial":
            self._report_serial_setup(data)

    def _report_serial_setup(self, data: Any) -> str:
        problem = _serial_setup_problem(data, self._serial_params)
        self.set_state("serial_bridge_setup", problem or (
            "Direct mode reported. Match the connected equipment's serial settings; "
            "apply, save and reboot if prompted. MaxColor also needs an encoder and decoder linked."
        ))
        return problem

    async def prepare_bridge_port(self, port_id: str, params: dict[str, Any]) -> None:
        # justOS settings can require a reboot. Commission explicitly, never
        # interrupt the AV stream while a downstream driver connects/reconnects.
        if port_id != "serial:1":
            raise CommandRefusedError("Choose the RS-232 (Direct mode) bridge port.")
        self._serial_params = {key: params[key] for key in ("baudrate", "bytesize", "parity", "stopbits", "flow_control") if key in params}
        try:
            data = await self._request("GET", "/settings/serial")
        except (DeviceRefused, ConnectionError) as exc:
            self.set_state("serial_bridge_setup", f"Cannot read serial settings: {exc}. Check the device web page.")
            raise
        self._apply("/settings/serial", data)
        problem = _serial_setup_problem(data, self._serial_params)
        if problem:
            raise CommandRefusedError(problem)


    async def poll(self) -> None:
        """The channel and status every poll; the rest every 30 s. A dead link
        raises (the platform counts it); a read the decoder refuses is named in
        last_error, written again by every poll while it holds."""
        if self._client is None:
            return
        now = asyncio.get_running_loop().time()
        slow = now >= self._slow_due
        uris = FAST + SLOW if slow else FAST
        for uri in uris:
            try:
                await self._read(uri)
            except DeviceRefused as exc:
                if uri == CORE:
                    raise ConnectionError(f"The decoder answered its details read with {exc}.") from exc
                self._refused[uri] = str(exc)
        if slow:
            self._slow_due = now + SLOW_INTERVAL_S
        if self._refused:
            what = ", ".join(f"{LABELS.get(uri, uri)} ({why})" for uri, why in self._refused.items())
            self.set_state("last_error", f"The decoder did not answer {what}; those values are not updated.")

    # ── Writes ──

    async def _send(self, uri: str, body: Any, what: str) -> Any:
        try:
            return await self._request("POST", uri, body)
        except DeviceRefused as exc:
            raise CommandRefusedError(f"The decoder refused {what} ({exc}).") from exc

    async def _cli(self, line: str, what: str) -> str:
        data = await self._send("/command/cli", line, what)
        if data is None or data is False:
            return ""
        return data if isinstance(data, str) else json.dumps(data)

    async def _read_back(self, uri: str, key: str, wanted: Any, what: str) -> None:
        """Read ``uri`` again; if ``key`` is not ``wanted``, once more a second
        later, then refuse."""
        for attempt in range(2):
            if attempt:
                await asyncio.sleep(READ_BACK_RETRY_S)
            try:
                await self._read(uri)
            except DeviceRefused as exc:
                log.debug(f"[{self.device_id}] Read-back of {uri} not answered: {exc}")
                return
            if self.get_state(key) == wanted:
                return
        reported = self.get_state(key)
        if reported is not None:
            raise CommandRefusedError(f"The decoder accepted {what} but still reports {reported}.")

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        if command in FIXED:
            uri, body, what = FIXED[command]
            await self._send(uri, body, what)
        elif command == "switch_source":
            channel = _as_int(params.get("channel"), -1)
            if not 0 <= channel <= 9999:
                raise ValueError("Channel must be 0 to 9999.")
            if self._flexible:
                await self._cli(f"channel -v {channel}", f"Switch Video to channel {channel}")
                await self._cli(f"channel -a {channel}", f"Switch Audio to channel {channel}")
                try:
                    await self._read("/details/channel")
                except DeviceRefused:
                    pass
            else:
                try:
                    await self._request("POST", "/command/channel", channel)
                except DeviceRefused as exc:
                    raise CommandRefusedError(
                        f"The decoder refused channel {channel} ({exc}). If the system is in "
                        "Flexible Mode, set Channel Mode to flexible."
                    ) from exc
                await self._read_back("/details/channel", "channel", channel, f"channel {channel}")
        elif command in SERVICES:
            letter, what = SERVICES[command]
            if not self._flexible:
                raise CommandRefusedError(
                    f"{what} needs Flexible Mode; with Channel Mode unified, use Switch Source."
                )
            channel = _as_int(params.get("channel"), -1)
            if not 0 <= channel <= 9999:
                raise ValueError("Channel must be 0 to 9999.")
            await self._cli(f"channel -{letter} {channel}", f"{what} to channel {channel}")
        elif command == "usb_connect":
            await self._cli("usb connect", "USB Connect")
        elif command == "usb_disconnect":
            await self._cli("usb stop", "USB Disconnect")
        elif command == "cec_send":
            data = str(params.get("bytes", "")).strip()
            if not HEX_BYTES.match(data):
                raise ValueError("CEC bytes must be hex pairs separated by spaces, e.g. EF 82 10 00.")
            await self._send("/command/cec/bytes", data, "the CEC bytes")
        elif command == "osd_text":
            body: dict[str, Any] = {"string": str(params.get("text", ""))}
            for key in ("timeout", "size", "opacity"):
                if params.get(key) not in (None, ""):
                    body[key] = _as_int(params.get(key), 0)
            for key in ("color", "align"):
                if params.get(key) not in (None, ""):
                    body[key] = str(params.get(key)).strip()
            await self._send("/command/osd", body, "the on-screen text")
        elif command == "videowall_layout":
            layout = [_as_int(params.get(k), 1) for k in ("rows", "columns", "row", "column")]
            rows, columns, row, column = layout
            if min(layout) < 1 or row > rows or column > columns:
                raise ValueError("Row and Column must be within the wall's Rows and Columns, all from 1.")
            await self._send("/command/videowall/layout", layout, "the video wall position")
            try:
                await self._read("/details/videowall/layout")
            except DeviceRefused:
                pass
        elif command == "videowall_rotate":
            degrees = _as_int(params.get("degrees"), -1)
            if degrees not in (0, 90, 180, 270):
                raise ValueError("Degrees must be 0, 90, 180 or 270.")
            await self._send("/command/videowall/rotate/clockwise", degrees, f"Rotate Output {degrees}")
            await self._read_back("/settings/videowall/rotate", "wall_rotation", degrees, f"Rotate Output {degrees}")
        elif command == "videowall_bezel":
            body = {
                "ow": _as_int(params.get("outer_width"), 0),
                "oh": _as_int(params.get("outer_height"), 0),
                "vw": _as_int(params.get("video_width"), 0),
                "vh": _as_int(params.get("video_height"), 0),
            }
            if not all(1 <= v <= 65535 for v in body.values()):
                raise ValueError("Each bezel measurement must be 1 to 65535.")
            await self._send("/command/videowall/bezel", body, "the bezel compensation")
        elif command in ("videowall_zoom_horizontal", "videowall_zoom_vertical"):
            axis = "horizontal" if command.endswith("horizontal") else "vertical"
            amount = _as_int(params.get("amount"), -1)
            if amount < 0:
                raise ValueError("Zoom must be 0 or more.")
            await self._send(f"/command/videowall/zoom/{axis}", amount, f"the {axis} zoom")
        elif command == "set_volume":
            level = _as_int(params.get("level"), -1)
            if not 0 <= level <= 11:
                raise ValueError("Volume must be 0 to 11.")
            await self._send("/command/audio/stereo/volume", level, f"stereo volume {level}")
            await self._read_back("/details/audio/stereo/volume", "volume", level, f"stereo volume {level}")
        elif command == "set_audio_delay":
            ms = _as_int(params.get("ms"), -1)
            if not 0 <= ms <= 175:
                raise ValueError("Delay must be 0 to 175 ms.")
            await self._send("/command/audio/stereo/delay", ms, f"stereo delay {ms} ms")
            await self._read_back("/details/audio", "audio_delay", ms, f"stereo delay {ms} ms")
        elif command == "serial_send":
            data = await self._send("/command/serial/ascii", str(params.get("text", "")), "the RS-232 text")
            self.set_state("serial_response", "" if data in (None, False) else str(data))
        elif command == "serial_send_hex":
            data = str(params.get("bytes", "")).strip()
            if not HEX_BYTES.match(data):
                raise ValueError("Bytes must be hex pairs separated by spaces, e.g. 0D 50 4F 57 52.")
            reply = await self._send("/command/serial/hex", data, "the RS-232 bytes")
            self.set_state("serial_response", "" if reply in (None, False) else str(reply))
        elif command == "flux_send":
            data = await self._send("/command/flux", str(params.get("text", "")), "the IR dongle command")
            self.set_state("flux_response", "" if data in (None, False) else str(data))
        elif command == "start_image_pull":
            priority = str(params.get("priority") or "low").strip().lower()
            if priority not in ("low", "high"):
                raise ValueError("Priority must be low or high.")
            body = {
                "width": max(0, min(640, _as_int(params.get("width"), 320))),
                "priority": priority,
                "frequency": max(0, _as_int(params.get("frequency"), 3)),
            }
            await self._send("/command/imagepull", body, "Start Image Pull")
            try:
                await self._read("/details/imagepull")
            except DeviceRefused:
                pass
        elif command == "jpsw_switch":
            decoders = str(params.get("decoders", "")).replace(" ", "")
            if not re.match(r"^(all|[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*)$", decoders):
                raise ValueError("Decoders must be a number, a list (1,3,5), a range (6-9), or all.")
            encoder = _as_int(params.get("encoder"), 0)
            if encoder < 1:
                raise ValueError("Encoder must be 1 or more.")
            output = await self._cli(f"jpsw -r {decoders} -t {encoder}", f"JPSW Switch to encoder {encoder}")
            if output.strip():
                self.set_state("cli_response", output.strip())
        elif command == "jadconfig_switch":
            receivers = [int(r) for r in str(params.get("receivers", "")).replace(" ", "").split(",") if r.isdigit()]
            transmitter = _as_int(params.get("transmitter"), 0)
            if not receivers or transmitter < 1:
                raise ValueError("Give at least one receiver number and a transmitter number.")
            await self._send("/command/switch", {"rx": receivers, "tx": transmitter}, f"JADConfig Switch to transmitter {transmitter}")
        elif command == "send_cli":
            line = str(params.get("command", "")).strip()
            if not line:
                raise ValueError("Enter a command.")
            output = await self._cli(line, "the CLI command")
            self.set_state("cli_response", output.strip())
        else:
            raise ValueError(f"Unknown command: {command}")
        return None

    # ── Device settings ──

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        spec = SETTINGS.get(key)
        if spec is None:
            raise ValueError(f"Unknown device setting: {key}")
        uri, kind, read_uri = spec
        info = self.DRIVER_INFO["device_settings"][key]
        allowed = info.get("values")
        if allowed and str(value) not in allowed:
            raise ValueError(f"{info['label']} must be one of {', '.join(allowed)}.")
        what = f"{info['label']} = {value}"
        await self._send(uri, _write_value(value, kind), what)
        await self._send("/command/device", "save", "Save Settings")
        await self._read_back(read_uri, info["state_key"], str(value), what)
        return None
