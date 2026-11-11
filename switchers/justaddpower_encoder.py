"""
OpenAVC Just Add Power Encoder Driver (justOS HTTP API).

Controls one Just Add Power encoder (transmitter) of the Omega, Ultra or
MaxColor series through justAPI, the HTTP API every justOS device serves for
itself: its channel (in Unified or Flexible Mode), the HDMI input and network
stream, the EDID it offers the source, the audio DSP mutes of the 3G+AVP and
3G+WP4 encoders, stereo volume and delay, RS-232 and Flux IR pass-through,
image pull, and switching through JPSW or JADConfig from this device. Its
status, channel, input resolution, HDCP, HDMI audio format, firmware and
network details are read back. Decoders have their own driver
(``justaddpower_decoder``); a Just Add Power system has no controller, so each
encoder and decoder is added as its own device.

Push vs poll
------------
Poll only. justAPI documents no subscription or event. The channel and the
device status are read every ``poll_interval`` (default 5 s); the rest every
30 s. A channel change reads the channel back at once.

Why Python
----------
Most justAPI answers are one value with nothing to say what it is:
``/details/channel`` answers ``{"data": 12}``, the stereo volume
``{"data": 6}``, image pull ``{"data": true}``, a setting's write the value
written. A YAML HTTP response rule sees the body and never the path that
asked, so it cannot tell a channel from a volume. Every command answers
``{"data": "OK"}`` and never the new value, so a channel change is read back
with a second request and a channel the encoder accepts and does not take is
reported as refused. In Flexible Mode a channel is one CLI command per
service, and a refusal carries a list of the models that support the command,
which is turned into a sentence.

One driver per role
-------------------
An encoder and a decoder share the API but not what the integrator controls:
a decoder chooses what it shows and drives the display, an encoder offers its
source and EDID. Every Just Add Power unit is one or the other, so each role
has its own driver and a project built before the equipment arrives shows
only the commands that unit has. The driver refuses a device that reports
itself as a decoder.

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

Channels. Each encoder sends on its own channel (0-9999); a decoder set to that
channel shows it. In Unified Mode ``POST /command/channel`` with the bare
number sets the one channel audio, video and USB share. In Flexible Mode (how
AMP 1.6.0 and later set up a new system) that command is not supported and
each service has its own channel, set with the CLI commands ``channel -v N``,
``channel -a N`` and ``channel -u N``; this driver sends them through
``/command/cli``. The documents give no read of the per-service channels, so
in Flexible Mode the driver reports only the channel ``/details/channel``
answers.

Sources:
    justOS HTTP API v1.1.2 (OpenAPI 3.0, Just Add Power):
    https://app.swaggerhub.com/apis-docs/JustAddPower/justOS-HTTP-API/1.1.2
    justOS HTTP API Overview, KB 466 (supported series, firmware minimums):
    https://support.justaddpower.com/kb/article/466-justos-http-api-overview-omega-ultra-and-maxcolor-series/
    Multicast Switching Protocol, KB 346 (Unified and Flexible Mode):
    https://support.justaddpower.com/kb/article/346-multicast-switching-protocol/
    JPSW switching, KB 434.
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
ROLE = "encoder"
OTHER_DRIVER = "Just Add Power Decoder (justOS)"

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
    "/details/imagepull": (("", "image_pull", "bool"),),
    "/settings/device": (
        ("name", "device_name", "str"),
        ("id", "device_id", "str"),
        ("leds", "leds", "leds"),
    ),
    "/settings/video/stream": (("maxbandwidth", "max_bandwidth", "str"),),
    "/settings/video/hdr": (("", "hdr_setting", "hdr"),),
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
    "/details/imagepull", "/settings/device", "/settings/video/stream", "/settings/video/hdr", "/settings/serial",
)
LABELS = {
    "/details/channel": "the channel",
    "/details/device/status": "the status",
    "/details/video": "the video details",
    "/details/audio": "the audio details",
    "/details/audio/stereo/volume": "the stereo volume",
    "/details/imagepull": "image pull",
    "/settings/device": "the device settings",
    "/settings/video/stream": "the stream settings",
    "/settings/video/hdr": "the HDR setting",
    "/settings/serial": "the serial settings",
}

# command -> (uri, body, what) for commands that send one fixed body.
FIXED: dict[str, tuple[str, Any, str]] = {
    "hdmi_input_off": ("/command/video", "off", "HDMI Input Off"),
    "hdmi_input_on": ("/command/video", "on", "HDMI Input On"),
    "stream_stop": ("/command/video", "stop", "Stop Stream"),
    "stream_start": ("/command/video", "start", "Start Stream"),
    "dsp_hdmi_mute": ("/command/audio/dsp/hdmi", "mute", "Mute HDMI Audio (DSP)"),
    "dsp_hdmi_unmute": ("/command/audio/dsp/hdmi", "on", "Unmute HDMI Audio (DSP)"),
    "dsp_line_mute": ("/command/audio/dsp/line", "mute", "Mute Line Input (DSP)"),
    "dsp_line_unmute": ("/command/audio/dsp/line", "on", "Unmute Line Input (DSP)"),
    "dsp_mic_mute": ("/command/audio/dsp/mic", "mute", "Mute Mic Input (DSP)"),
    "dsp_mic_unmute": ("/command/audio/dsp/mic", "on", "Unmute Mic Input (DSP)"),
    "dsp_reset": ("/command/audio/dsp", "reset", "Restart Audio DSP"),
    "save_settings": ("/command/device", "save", "Save Settings"),
    "reboot": ("/command/device", "reboot", "Reboot"),
}
# Flexible Mode CLI service letters.
SERVICES = {
    "set_video_channel": ("v", "Set Video Channel"),
    "set_audio_channel": ("a", "Set Audio Channel"),
    "set_usb_channel": ("u", "Set USB Channel"),
}
HEX_BYTES = re.compile(r"^[0-9A-Fa-f]{2}( +[0-9A-Fa-f]{2})*$")
NO_BODY = object()

# device setting -> (uri, kind, read-back uri)
SETTINGS = {
    "device_name": ("/settings/device/name", "str", "/settings/device"),
    "leds": ("/settings/device/leds", "leds", "/settings/device"),
    "max_bandwidth": ("/settings/video/stream/maxbandwidth", "str", "/settings/video/stream"),
    "hdr_passthrough": ("/settings/video/hdr", "hdr", "/settings/video/hdr"),
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


class JustAddPowerEncoderDriver(BaseDriver):
    """One Just Add Power justOS encoder over justAPI (HTTP)."""

    DRIVER_INFO = {
        "id": "justaddpower_encoder",
        "name": "Just Add Power Encoder (justOS)",
        "manufacturer": "Just Add Power",
        "category": "switcher",
        "version": "1.1.0",
        "min_platform_version": "0.36.0",
        "author": "OpenAVC",
        "description": (
            "Controls a Just Add Power Omega, Ultra or MaxColor encoder over justAPI: "
            "its channel in Unified or Flexible Mode, the HDMI input and stream, the "
            "EDID it offers the source, audio DSP mutes, stereo volume and delay, RS-232 "
            "and IR pass-through, and JPSW switching. Reads back the channel, status, "
            "input resolution, HDCP, HDMI audio format and firmware."
        ),
        "source_url": "https://app.swaggerhub.com/apis-docs/JustAddPower/justOS-HTTP-API/1.1.2",
        "tags": [
            "just-add-power", "jap", "justos", "av-over-ip", "hdmi-over-ip", "encoder",
            "transmitter", "multicast", "edid", "omega", "ultra", "maxcolor", "serial-bridge", "rs232",
        ],
        "verified": False,
        "simulated": True,
        "protocols": ["justaddpower_justapi"],
        "ports": [80],
        "transport": "http",
        "bridge": {"ports": [{"id": "serial:1", "kind": "serial", "passthrough_port": 6752, "label": "RS-232 (Direct mode)"}]},
        "discovery": {
            # justAPI answers without a sign-in. An encoder's model ends in TX
            # ("3G+AVP TX"), a decoder's in RX. MaxColor's model words are not
            # in the API document. No oui: the API document's MACs are locally
            # administered (c2:00:00:...) and the IEEE registries list no Just
            # Add Power block.
            "tcp_probe": {
                "port": 80,
                "send_ascii": "GET /cgi-bin/api/details/device/model HTTP/1.0\r\n\r\n",
                "expect_regex": r'"data"\s*:\s*"[^"]*\bTX"',
                "timeout_ms": 3000,
                "extract": {
                    "model": {"regex": r'"data"\s*:\s*"([^"]*\bTX)"'},
                },
            },
            "manufacturer_alias": ["Just Add Power", "JustAddPower", "J+P"],
        },
        "compatible_models": [
            {
                "manufacturer": "Just Add Power",
                "models": [
                    "Ultra Series encoders (3G TX, 3G+4+ TX, 3G+AVP TX, 3G+HIFI TX, 3G+WP2 TX, 3G+WP4 TX)",
                    "Omega Series encoders (2G/3G TX, 2G/3G+ TX, 2G/3G VGA TX)",
                ],
                "confidence": "untested",
                "notes": (
                    "justOS firmware B1.1.0 or later for justAPI; B2.1.0 or later for "
                    "Multicast Switching. Not in a 2G Series system (firmware A6.5.9), "
                    "where justAPI is unsupported. The DSP mutes are on the 3G+AVP TX and "
                    "3G+WP4 TX only. Not yet tested on this hardware."
                ),
            },
            {
                "manufacturer": "Just Add Power",
                "models": ["MaxColor Series encoders"],
                "confidence": "untested",
                "notes": (
                    "MaxColor firmware MAX v3.5.2 or later. The API document does not "
                    "list MaxColor's model names. Not yet tested on this hardware."
                ),
            },
        ],
        "help": {
            "overview": (
                "Controls one Just Add Power encoder: the channel it sends on, its HDMI "
                "input and network stream, the EDID it offers the source, the audio DSP "
                "mutes on a 3G+AVP or 3G+WP4 encoder, stereo audio, and RS-232 and IR "
                "pass-through. Add each encoder as its own device, and each decoder "
                "with the Just Add Power Decoder (justOS) driver; a decoder set to an "
                "encoder's channel shows that encoder."
            ),
            "setup": (
                "1. Update the encoder to justOS B2.1.0 or later (MaxColor: MAX "
                "v3.5.2 or later).\n"
                "2. Enter the encoder's IP address. justAPI needs no password.\n"
                "3. Set Channel Mode to match the system: Flexible for a Multicast "
                "Switching system set up with AMP 1.6.0 or later (its default), "
                "Unified for an older one or one converted to Unified Mode.\n"
                "4. To control equipment through RS-232, open the encoder's web page, "
                "Comms > Serial Settings. Select Direct (TCP:6752), match the equipment's "
                "baud rate, data bits, parity and stop bits, then apply, save and reboot if prompted. "
                "Check the serial cable pinout and null modem jumper. MaxColor needs an encoder "
                "and decoder linked before Direct mode is available.\n"
                "5. Add the connected equipment with its own serial driver. Choose Through a bridge, "
                "this encoder, and RS-232 (Direct mode). Allow TCP 6752 from OpenAVC to the encoder. "
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
                    "unified: audio, video and USB share one channel. flexible: each has "
                    "its own channel (AMP 1.6.0 and later set up new systems this way); "
                    "Set Channel sets all three, and Set Video, Audio and USB Channel set one."
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
                    "The channel this encoder sends on; a decoder set to it shows this "
                    "encoder. In Flexible Mode, the channel the encoder reports; it does "
                    "not report video, audio and USB separately."
                ),
                "control": True,
                "cloud_priority": "high",
            },
            "status": {
                "type": "string",
                "label": "Status",
                "help": (
                    "What the encoder is doing, in its own words, e.g. Streaming Video, "
                    "Waiting for Receiver, No Video from Source."
                ),
                "cloud_priority": "high",
            },
            "resolution": {"type": "string", "label": "Input Resolution", "help": "The resolution the source sends, e.g. 1920x1080p@60Hz."},
            "hdcp": {"type": "string", "label": "HDCP", "help": "The HDCP version the source asks for (1.x or 2.2)."},
            "hdr": {"type": "enum", "values": ["passthru", "off"], "label": "HDR", "help": "passthru while HDR passes through."},
            "hdmi_audio": {"type": "boolean", "label": "HDMI Audio", "help": "Whether the source sends HDMI audio."},
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
            "image_pull": {"type": "boolean", "label": "Image Pull", "help": "Whether image pull is running."},
            "device_name": {"type": "string", "label": "Device Name"},
            "device_id": {"type": "string", "label": "Device ID", "help": "The encoder's ID in the system, e.g. TX3."},
            "leds": {"type": "enum", "values": ["default", "video", "off"], "label": "LEDs"},
            "max_bandwidth": {
                "type": "enum", "values": ["auto", "10M", "50M", "100M", "150M", "200M"],
                "label": "Max Bandwidth", "help": "The most network bandwidth the stream may use.",
            },
            "hdr_setting": {"type": "enum", "values": ["passthru", "off"], "label": "HDR Passthrough (start-up)"},
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
            "cli_response": {"type": "string", "label": "CLI Response", "help": "What the encoder printed for the last Send CLI Command."},
            "last_error": {
                "type": "string",
                "label": "Last Error",
                "help": "A value the encoder did not answer on the last poll; cleared by the next clean poll.",
            },
        },
        "commands": {
            "set_channel": {
                "label": "Set Channel",
                "help": (
                    "Sets the channel this encoder sends on; decoders set to it show this "
                    "encoder. Flexible Mode: sets the video, audio and USB channels."
                ),
                "params": {
                    "channel": {"type": "integer", "min": 0, "max": 9999, "required": True, "label": "Channel"},
                },
                "sets": {"channel": "{channel}"},
            },
            "set_video_channel": {
                "label": "Set Video Channel",
                "help": "Flexible Mode: sets the channel this encoder sends video on.",
                "params": {
                    "channel": {"type": "integer", "min": 0, "max": 9999, "required": True, "label": "Channel"},
                },
            },
            "set_audio_channel": {
                "label": "Set Audio Channel",
                "help": "Flexible Mode: sets the channel this encoder sends audio on.",
                "params": {
                    "channel": {"type": "integer", "min": 0, "max": 9999, "required": True, "label": "Channel"},
                },
            },
            "set_usb_channel": {
                "label": "Set USB Channel",
                "help": "Flexible Mode: sets the channel this encoder's USB is reached on.",
                "params": {
                    "channel": {"type": "integer", "min": 0, "max": 9999, "required": True, "label": "Channel"},
                },
            },
            "hdmi_input_off": {"label": "HDMI Input Off", "help": "Turns the HDMI input off: decoders showing this encoder lose its picture.", "params": {}},
            "hdmi_input_on": {"label": "HDMI Input On", "help": "Turns the HDMI input back on.", "params": {}},
            "stream_stop": {"label": "Stop Stream", "help": "Stops the encoder sending on the network.", "params": {}},
            "stream_start": {"label": "Start Stream", "help": "Starts the encoder sending on the network again.", "params": {}},
            "edid_default": {
                "label": "Use Built-in EDID",
                "help": "Offers the source one of the encoder's built-in EDIDs: hdmi (stereo) or multichannel.",
                "params": {
                    "edid": {"type": "enum", "values": ["hdmi", "multichannel"], "required": True, "label": "EDID"},
                },
            },
            "edid_custom": {
                "label": "Use Custom EDID",
                "help": "Offers the source this EDID.",
                "params": {
                    "edid": {
                        "type": "string", "required": True, "label": "EDID (hex)", "pattern": "^[0-9A-Fa-f]+$",
                        "help": "The EDID as one string of hex digits, e.g. 00ffffffffffff00...",
                    },
                },
            },
            "dsp_hdmi_mute": {"label": "Mute HDMI Audio (DSP)", "help": "Mutes the HDMI input audio into the DSP (3G+AVP TX and 3G+WP4 TX).", "params": {}},
            "dsp_hdmi_unmute": {"label": "Unmute HDMI Audio (DSP)", "help": "Unmutes the HDMI input audio into the DSP.", "params": {}},
            "dsp_line_mute": {"label": "Mute Line Input (DSP)", "help": "Mutes the line input into the DSP (3G+AVP TX and 3G+WP4 TX).", "params": {}},
            "dsp_line_unmute": {"label": "Unmute Line Input (DSP)", "help": "Unmutes the line input into the DSP.", "params": {}},
            "dsp_mic_mute": {"label": "Mute Mic Input (DSP)", "help": "Mutes the mic input into the DSP (3G+AVP TX and 3G+WP4 TX).", "params": {}},
            "dsp_mic_unmute": {"label": "Unmute Mic Input (DSP)", "help": "Unmutes the mic input into the DSP.", "params": {}},
            "dsp_reset": {
                "label": "Restart Audio DSP",
                "help": "Restarts the Dolby Digital audio processor (3G+AVP TX and 3G+WP4 TX); audio drops while it restarts.",
                "params": {},
            },
            "set_volume": {
                "label": "Set Stereo Volume",
                "help": "Sets the analog stereo output level (3G+4+ TX, 3G+AVP TX and 3G+WP4 TX).",
                "params": {"level": {"type": "integer", "min": 0, "max": 11, "required": True, "label": "Level"}},
                "sets": {"volume": "{level}"},
            },
            "set_audio_delay": {
                "label": "Set Stereo Delay",
                "help": "Delays the analog stereo output by this many milliseconds.",
                "params": {"ms": {"type": "integer", "min": 0, "max": 175, "required": True, "label": "Delay (ms)"}},
                "sets": {"audio_delay": "{ms}"},
            },
            "cec_send": {
                "label": "Send CEC",
                "help": "Sends these bytes on the HDMI CEC bus to the source.",
                "params": {
                    "bytes": {
                        "type": "string", "required": True, "label": "Bytes",
                        "pattern": "^[0-9A-Fa-f]{2}( +[0-9A-Fa-f]{2})*$",
                        "help": "Hex bytes separated by spaces, e.g. 4F 82 10 00.",
                    },
                },
            },
            "serial_send": {
                "label": "Send RS-232",
                "help": (
                    "Sends this text out of the encoder's RS-232 port, with a carriage return "
                    "before and after. Needs serial mode api. For equipment feedback, use its "
                    "own driver Through a bridge with the encoder in Direct mode."
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
                "help": "Sends this command to a Flux Capacitor IR dongle on the encoder; the reply goes to IR Dongle Response.",
                "params": {"text": {"type": "string", "required": True, "label": "Command"}},
            },
            "start_image_pull": {
                "label": "Start Image Pull",
                "help": "Starts image pull now with these values; the encoder's start-up image pull settings are left alone.",
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
                "help": "Runs a command on the encoder's command line; what it prints goes to CLI Response.",
                "params": {"command": {"type": "string", "required": True, "label": "Command"}},
            },
            "save_settings": {"label": "Save Settings", "help": "Saves the encoder's settings so they are kept when it restarts.", "params": {}},
            "reboot": {"label": "Reboot", "help": "Restarts the encoder; decoders showing it lose its picture until it is back.", "params": {}},
        },
        "device_settings": {
            "device_name": {
                "type": "string", "label": "Device Name", "state_key": "device_name", "default": "", "setup": False,
                "help": "The encoder's name. Saved on the encoder.",
            },
            "leds": {
                "type": "enum", "label": "LEDs", "state_key": "leds", "values": ["default", "video", "off"],
                "default": "default", "setup": False,
                "help": "What the encoder's LEDs show. Saved on the encoder; takes effect when it next restarts.",
            },
            "max_bandwidth": {
                "type": "enum", "label": "Max Bandwidth", "state_key": "max_bandwidth",
                "values": ["auto", "10M", "50M", "100M", "150M", "200M"], "default": "auto", "setup": False,
                "help": "The most network bandwidth the stream may use. Saved on the encoder; takes effect when it next restarts.",
            },
            "hdr_passthrough": {
                "type": "enum", "label": "HDR Passthrough", "state_key": "hdr_setting", "values": ["passthru", "off"],
                "default": "passthru", "setup": False,
                "help": "Whether HDR passes through. Saved on the encoder; takes effect when it next restarts.",
            },
        },
        "quick_actions": ["hdmi_input_off", "hdmi_input_on"],
        "actions": [
            {"id": "hdmi_input_off", "kind": "command", "icon": "eye-off"},
            {"id": "hdmi_input_on", "kind": "command", "icon": "eye"},
        ],
    }

    HEALTH_FAULT_MESSAGE = "The encoder stopped answering justAPI requests."

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
            raise ConnectionError(f"The encoder answered its details read with {exc}.") from exc
        role = _role_of(details)
        if role is not None and role != ROLE:
            model = str(details.get("model") or "").strip() if isinstance(details, dict) else ""
            what = f" ({model})" if model else ""
            raise ConnectionFaultError(
                f"This is a Just Add Power {role}{what}. Add it with the {OTHER_DRIVER} driver.",
                code="invalid_config",
            )
        self._apply(CORE, details)
        log.info(f"[{self.device_id}] Connected to Just Add Power encoder at {self._host}:{self._port}")

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
                log.info(f"[{self.device_id}] This encoder has no {uri}; not asked again")
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
        raises (the platform counts it); a read the encoder refuses is named in
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
                    raise ConnectionError(f"The encoder answered its details read with {exc}.") from exc
                self._refused[uri] = str(exc)
        if slow:
            self._slow_due = now + SLOW_INTERVAL_S
        if self._refused:
            what = ", ".join(f"{LABELS.get(uri, uri)} ({why})" for uri, why in self._refused.items())
            self.set_state("last_error", f"The encoder did not answer {what}; those values are not updated.")

    # ── Writes ──

    async def _send(self, uri: str, body: Any, what: str) -> Any:
        try:
            return await self._request("POST", uri, body)
        except DeviceRefused as exc:
            raise CommandRefusedError(f"The encoder refused {what} ({exc}).") from exc

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
            raise CommandRefusedError(f"The encoder accepted {what} but still reports {reported}.")

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        if command in FIXED:
            uri, body, what = FIXED[command]
            await self._send(uri, body, what)
        elif command == "set_channel":
            channel = _as_int(params.get("channel"), -1)
            if not 0 <= channel <= 9999:
                raise ValueError("Channel must be 0 to 9999.")
            if self._flexible:
                for letter, service in (("v", "Video"), ("a", "Audio"), ("u", "USB")):
                    await self._cli(f"channel -{letter} {channel}", f"Set {service} Channel {channel}")
                try:
                    await self._read("/details/channel")
                except DeviceRefused:
                    pass
            else:
                try:
                    await self._request("POST", "/command/channel", channel)
                except DeviceRefused as exc:
                    raise CommandRefusedError(
                        f"The encoder refused channel {channel} ({exc}). If the system is in "
                        "Flexible Mode, set Channel Mode to flexible."
                    ) from exc
                await self._read_back("/details/channel", "channel", channel, f"channel {channel}")
        elif command in SERVICES:
            letter, what = SERVICES[command]
            if not self._flexible:
                raise CommandRefusedError(
                    f"{what} needs Flexible Mode; with Channel Mode unified, use Set Channel."
                )
            channel = _as_int(params.get("channel"), -1)
            if not 0 <= channel <= 9999:
                raise ValueError("Channel must be 0 to 9999.")
            await self._cli(f"channel -{letter} {channel}", f"{what} {channel}")
        elif command == "edid_default":
            choice = str(params.get("edid") or "").strip().lower()
            if choice not in ("hdmi", "multichannel"):
                raise ValueError("EDID must be hdmi or multichannel.")
            await self._send("/command/edid/default", choice, f"the {choice} EDID")
        elif command == "edid_custom":
            edid = str(params.get("edid") or "").strip()
            if not re.fullmatch(r"[0-9A-Fa-f]+", edid) or len(edid) % 2:
                raise ValueError("The EDID must be an even number of hex digits.")
            await self._send("/command/edid/custom", edid, "the custom EDID")
        elif command == "cec_send":
            data = str(params.get("bytes", "")).strip()
            if not HEX_BYTES.match(data):
                raise ValueError("CEC bytes must be hex pairs separated by spaces, e.g. 4F 82 10 00.")
            await self._send("/command/cec/bytes", data, "the CEC bytes")
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
