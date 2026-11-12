"""AMX NMX-DEC-N2422A / N2622S / N3322D. Direct Control reports a CR-separated status
packet even for writes. Python correlates fresh fields across those packets,
prefixes secure commands with the receiver's command password, and subscribes
to N2622S HDMI events. N3322D Matrix selections combine several independently
reported fields from one status packet and respect Dante AV-H ownership.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from ipaddress import IPv4Address
import json
import re
from urllib.parse import urlsplit

from openavc.core.device_traffic import get_traffic_recorder
from openavc.drivers.base import BaseDriver, CommandRefusedError, ConnectionFaultError

STATUS = "SVSI_RXGEN2"
NETWORK = "SVSI_NETSTATS"
SERIAL = ("serial_baud", "serial_bits", "serial_parity", "serial_stops")
BAUD = {1200, 1800, 2400, 4800, 9600, 19200, 38400, 57600, 115200}
ROUTES = {
    "route_video",
    "route_audio",
    "set_video_stream",
    "set_audio_stream",
    "follow_video",
}
MODEL_PATTERN = r"(?:NMX-DEC-)?(N2422A|N2622S|N3322D)(?!-C)(?=\d|[ :\t-]|$)"
N24_MODES = [
    "auto",
    "480p",
    "480p59.94",
    "720p59.94",
    "720p60",
    "1080p59.94",
    "1080p60",
    "2160p60",
]
N24_BAUD = {
    300,
    1200,
    2400,
    4800,
    9600,
    14400,
    19200,
    28800,
    38400,
    57600,
    115200,
    230400,
}
N24_FEEDBACK = {
    "video_source",
    "audio_selection",
    "video_stream",
    "audio_stream",
    "play_mode",
    "usb_enabled",
    "kvm_enabled",
}
N33_MODES = [
    "auto",
    "720p50",
    "720p60",
    "1080p50",
    "1080p60",
    "2160p25",
    "2160p30",
    "2160p50",
    "2160p60",
]
N26_ONLY = {
    "usb_enabled",
    "usb_host",
    "kvm_enabled",
    "kvm_host",
    "wall_enabled",
    "wall_rows",
    "wall_columns",
    "wall_row",
    "wall_column",
    "playlist",
    "output_resolution",
    "hdcp_status",
}
N33_ONLY = {
    "dante_av_h_enabled",
    "hold_last_frame",
    "disable_hdmi_on_loss",
    "hdmi_audio_mode",
    "local_uses_stream_audio",
    "audio_sample_rate",
}
OWNED_COMMANDS = ROUTES | {"set_receive_mode", "set_stream_url", "select_audio_source"}
MATRIX_READINGS = {"video_source", "audio_selection", "dante_av_h_enabled"}
ROUTE_READINGS = {
    "dante_av_h_enabled",
    "play_mode",
    "receive_mode",
    "audio_stream",
    "video_stream",
    "stream_url",
}
AUDIO_INPUTS = {"svsi": "SVSI", "dante": "Dante", "H.26x": "H.26x"}
ON_OFF = {"off": False, "on": True}
HDMI_ROOT = "/decoder/systemStatus/"
HDMI_EVENTS = {
    "hdmiOutCableStatusChanged": "hdmiOutStatus",
    "hdmiOutResolutionChanged": "hdmiOutResolution",
}
HDMI_READS = {
    "hdmiOutStatus": "hdmi_connected",
    "hdmiOutResolution": "output_resolution",
    "hdcpStatus": "hdcp_status",
}
TOGGLES = {
    "hold_last_frame": (
        "setSettings:holdlastframe:off",
        "setSettings:holdlastframe:on",
    ),
    "disable_hdmi_on_loss": (
        "setSettings:disableHDMILostStream:off",
        "setSettings:disableHDMILostStream:on",
    ),
    "audio_muted": ("unmute", "mute"),
    "video_muted": ("vunmute", "vmute"),
    "hdmi_enabled": ("hdmiOff", "hdmiOn"),
    "scaler_enabled": ("scalerdisable", "scalerenable"),
    "wall_enabled": ("setSettings:wallEnable:off", "setSettings:wallEnable:on"),
    "usb_enabled": ("setSettings:usbEnable:off", "setSettings:usbEnable:on"),
    "kvm_enabled": ("setSettings:kvmEnable:off", "setSettings:kvmEnable:on"),
}
LEVELS = {
    "set_left_volume": "lovolleft",
    "set_right_volume": "lovolright",
    "set_stereo_volume": "lovol",
}
FIELDS = {
    STATUS: ("identity", str),
    "HDMIAUDIO": ("hdmi_audio_mode", str.lower),
    "LIVEAUDIOLP": ("local_uses_stream_audio", ON_OFF),
    "SAMPLE": ("audio_sample_rate", str),
    "AVHENABLE": ("dante_av_h_enabled", {"0": False, "1": True}),
    "FRAMEHOLD": ("hold_last_frame", ON_OFF),
    "VIDOFFNOSTRM": ("disable_hdmi_on_loss", ON_OFF),
    "NAME": ("device_name", str),
    "IP": ("ip", str),
    "MAC": ("mac", str),
    "SWVER": ("firmware", str),
    "CHASSISID": ("switch_chassis", str),
    "SYSNAME": ("switch_name", str),
    "PORTID": ("switch_port", str),
    "PORTDESCR": ("switch_port_description", str),
    "STREAM": ("video_stream", int),
    "STREAMAUDIO": ("audio_stream", int),
    "MUTE": ("audio_muted", {"0": False, "1": True}),
    "VMUTE": ("video_muted", {"0": False, "1": True}),
    "HDMIOFF": ("hdmi_enabled", {"on": False, "off": True}),
    "HDMISTATUS": ("hdmi_connected", {"connected": True, "disconnected": False}),
    "INPUTRES": ("input_resolution", str),
    "PLAYMODE": ("play_mode", str.lower),
    "PLAYLIST": ("playlist", int),
    "LINEOUTVOL_L": ("left_volume", int),
    "LINEOUTVOL_R": ("right_volume", int),
    "SCALERBYPASS": ("scaler_enabled", {"no": True, "yes": False}),
    "MODE": ("scaler_mode", lambda s: s.lower().removesuffix(".mode")),
    "RCVMODE": ("receive_mode", {"1": "MWC", "2": "H26X"}),
    "AUDIOSTREAM": ("audio_source", {"1": "SVSI", "2": "Dante"}),
    "KVMENABLESTATUS": ("kvm_enabled", {"0": False, "1": True}),
    "USB20ENABLESTATUS": ("usb_enabled", {"0": False, "1": True}),
    "KVMMAINIP": ("kvm_host", str),
    "USB20MAINIP": ("usb_host", str),
    "WALLENABLE": ("wall_enabled", {"0": False, "1": True}),
    "WALLHORMONS": ("wall_columns", int),
    "WALLVERMONS": ("wall_rows", int),
    "WALLMONPOSH": ("wall_column", int),
    "WALLMONPOSV": ("wall_row", int),
    "BAUD": ("serial_baud", int),
    "SNUMB": ("serial_bits", int),
    "SPAR": ("serial_parity", str.lower),
    "SP2S": ("serial_stops", int),
    "ID": ("identified", {"0": True, "1": False}),
}


def number(value, low, high):
    if not re.fullmatch(r"[0-9]+", str(value)) or not low <= int(value) <= high:
        raise CommandRefusedError(f"Enter a whole number from {low} to {high}.")
    return int(value)


def flag(value):
    if isinstance(value, bool):
        return value
    values = {"true": True, "false": False, "1": True, "0": False}
    try:
        return values[str(value).lower()]
    except KeyError:
        raise CommandRefusedError("Choose on or off.") from None


def serial_format(params, rates=BAUD):
    baud = number(params.get("baudrate", 9600), min(rates), max(rates))
    bits = number(params.get("bytesize", 8), 7, 8)
    parity = {"N": "none", "O": "odd", "E": "even"}.get(
        str(params.get("parity", "N")).upper()
    )
    stops = {"1": 1, "1.0": 1, "2": 2, "2.0": 2}.get(str(params.get("stopbits", 1)))
    if not all((baud in rates, parity, stops)):
        raise CommandRefusedError(
            "Choose a supported baud rate, N/O/E parity and one or two stop bits."
        )
    if any(params.get(k) for k in ("rtscts", "xonxoff", "dsrdtr")):
        raise CommandRefusedError("Use no flow control on the decoder RS-232 port.")
    return dict(zip(SERIAL, (baud, bits, parity, stops)))


BASIC_SCHEMES = {"rtsp", "rtp", "udp"}
N26_SCHEMES = BASIC_SCHEMES | {"http", "https"}
N33_SCHEMES = BASIC_SCHEMES | {"rtmp", "rtmps", "srtp"}


def stream_url(value, *, schemes=N26_SCHEMES):
    value = str(value)
    if not re.fullmatch(r"[!-~]+", value):
        raise CommandRefusedError(
            "Use an ASCII stream URL without spaces or line breaks."
        )
    url = parse_address(value)
    if url.scheme not in schemes:
        raise CommandRefusedError("Enter a supported H.26x stream URL.")
    if any((url.username is not None, url.query, url.fragment)):
        raise CommandRefusedError(
            "Use the decoder web page for URLs with credentials, query strings or fragments."
        )
    return value


def parse_address(value):
    try:
        url = urlsplit(value)
        if not url.hostname or url.port == 0:
            raise ValueError
        return url
    except ValueError:
        raise CommandRefusedError("Enter a valid stream address and port.") from None


def report_url(value):
    # Site-configured URLs can contain secrets. Publish only credential-free addresses.
    try:
        return stream_url(value, schemes=N26_SCHEMES | N33_SCHEMES) if value else ""
    except (CommandRefusedError, ValueError):
        return "Private stream URL"


def encoder_address(value):
    try:
        addr = IPv4Address(value)
    except ValueError:
        raise CommandRefusedError("Enter the encoder's unicast IPv4 address.") from None
    if any((addr.is_multicast, addr.is_unspecified, int(addr) == 0xFFFFFFFF)):
        raise CommandRefusedError("Enter the encoder's unicast IPv4 address.")
    return str(addr)


def wall_layout(params):
    dimensions = {
        k: number(params[k], 1, 16) for k in ("rows", "columns", "row", "column")
    }
    if (
        dimensions["row"] > dimensions["rows"]
        or dimensions["column"] > dimensions["columns"]
    ):
        raise CommandRefusedError("Select a position inside the video wall.")
    wire = "setSettings:VideoWallHorVer:" + ",".join(map(str, dimensions.values()))
    return wire, {"wall_" + k: v for k, v in dimensions.items()}


def valid_hc_packet(body):
    packet = json.loads(body)
    if not isinstance(packet, dict):
        return None
    path = packet.get("path")
    return packet if isinstance(path, str) and path.startswith(HDMI_ROOT) else None


def split_field(line):
    for header in (STATUS, NETWORK):
        if line.upper().startswith(header):
            return header, line[len(header) :].lstrip(": \t")
    wire, _, value = line.partition(":")
    return wire.upper(), value.strip()


def n33_audio(value):
    # AMX names the sources but does not define numeric AUDIOSTREAM codes.
    return {"svsi": "SVSI", "dante": "Dante", "h.26x": "H.26x", "h26x": "H.26x"}.get(
        value.lower(), value
    )


def read_field(spec, value):
    key, convert = spec
    try:
        parsed = convert[value.lower()] if isinstance(convert, dict) else convert(value)
    except (ValueError, KeyError):
        return {}
    return {key: parsed}


N24_ROUTE_INPUTS = {"aes67_enabled", "audio_stream", "play_mode", "video_stream"}


def n24_selections(packet):
    # N2400 has one numbered video format. Reuse the live/local selection
    # rule, treating stream zero as the decoder's local fallback.
    routes = video_selection({**packet, "receive_mode": "SVSI"})
    if routes.get("video_source") == "0":
        routes["video_source"] = ""
    if packet.get("aes67_enabled"):
        routes["audio_selection"] = ""
    elif packet.get("aes67_enabled") is False and "audio_stream" in packet:
        routes["audio_selection"] = str(packet["audio_stream"])
    return routes


def video_selection(packet):
    if packet.get("play_mode") == "local":
        return {"video_source": ""}
    source_key = {"SVSI": "video_stream", "H26X": "stream_url"}.get(
        packet.get("receive_mode")
    )
    if packet.get("play_mode") != "live" or source_key not in packet:
        return {}
    source = packet[source_key]
    return {"video_source": "" if source == "Private stream URL" else str(source)}


def video_recall_steps(source, urls):
    if source in urls:
        mode = "H26X"
        selection = ("setSettings:streamURL:" + source, "stream_url", source)
    else:
        mode = "SVSI"
        selection = ("set:" + source, "video_stream", int(source))
    return [
        ("setSettings:ReceiveSrc:" + mode, "receive_mode", mode),
        selection,
        ("live", "play_mode", "live"),
    ]


def numbered_source(row, name):
    stream = str(number(row.get("stream"), 1, 32511))
    return stream, f"{name} ({stream})"


def usb_source(row, name):
    addr = encoder_address(row.get("address"))
    return addr, f"{name} ({addr})"


def url_source(row, name):
    return stream_url(row.get("url", ""), schemes=N33_SCHEMES), name


def source_choices(rows, parse):
    choices = []
    ids = set()
    for row in rows or ():
        name = str(row.get("name", "")).strip()
        value, label = parse(row, name)
        if not name or value in ids:
            raise ValueError(
                "Each source needs a name and a unique stream number or URL."
            )
        ids.add(value)
        choices.append({"value": value, "label": label})
    return ids, choices


FIELDS["STREAMURL"] = ("stream_url", report_url)
N33_FIELDS = {
    **FIELDS,
    "AUDIOSTREAM": ("audio_source", n33_audio),
    "RCVMODE": ("receive_mode", {"1": "SVSI", "2": "H26X"}),
}
N24_FIELDS = {
    **FIELDS,
    "AES67": ("aes67_enabled", {"0": False, "1": True}),
    "AESRX": ("aes67_address", str),
    "DVIOFF": FIELDS["HDMIOFF"],
    "DVISTATUS": FIELDS["HDMISTATUS"],
    "USB": FIELDS["USB20ENABLESTATUS"],
    "KVM": FIELDS["KVMENABLESTATUS"],
}
for alias in ("HDMIOFF", "HDMISTATUS", "USB20ENABLESTATUS", "KVMENABLESTATUS"):
    del N24_FIELDS[alias]
LEGACY_SELECTIONS = {"video_stream": "video_source", "audio_stream": "audio_selection"}


class AMXN2622SDriver(BaseDriver):
    DRIVER_INFO = {
        "id": "amx_svsi_n2622s",
        "name": "AMX SVSI N2422A / N2622S / N3322D Decoders",
        "manufacturer": "AMX",
        "category": "switcher",
        "author": "OpenAVC",
        "version": "1.2.0",
        "transport": "tcp",
        "delimiter": "\r",
        "ports": [50002, 50102],
        "protocols": ["amx-svsi-direct-control", "hcontrol"],
        "simulated": True,
        "verified": False,
        "min_platform_version": "0.37.2",
        "source_url": "https://www.amx.com/da/site_elements/amx-direct-control-api-n26xxs",
        "description": "Routes named video and independent audio to an N2422A, N2622S or N3322D. Controls local playback, scaling, "
        "line levels and HDMI output. The N3322D supports named H.26x sources and Dante AV-H ownership feedback.",
        "tags": ["av-over-ip", "decoder", "svsi", "4k", "serial-bridge", "cec"],
        "compatible_models": [
            {
                "manufacturer": "AMX",
                "models": ["NMX-DEC-N2422A"],
                "confidence": "untested",
                "notes": "Standalone N2400 JPEG 2000 decoder with independent audio and keyboard/mouse routing. Hardware verification pending.",
            },
            {
                "manufacturer": "AMX",
                "models": ["NMX-DEC-N2622S"],
                "confidence": "untested",
                "notes": "Standalone decoder. Derived from AMX Direct Control API v1.3 and the "
                "N2600S HControl guide. Hardware verification pending.",
            },
            {
                "manufacturer": "AMX",
                "models": ["NMX-DEC-N3322D"],
                "confidence": "untested",
                "notes": "Standalone 4K60 H.26x/Dante AV-H decoder. AMX N33x2D Direct Control. Hardware verification pending.",
            },
        ],
        "help": {
            "overview": "One decoder is one destination. Name the encoder streams in Sources to build "
            "video and audio Matrix controls. Audio stream 0 follows video. Incoming "
            "resolution and HDMI detection help distinguish a selected route from a displayed "
            "picture.",
            "setup": "1. Enter the receiver address. Use TCP 50002 for ordinary control. With Secure Ports "
            "Only, enable TLS, enter port 50102 and the receiver command password. Use a trusted "
            "receiver certificate. Each Direct Control port accepts one controller at a time.\n"
            "2. Match Routing configured on receiver to its actual Dante AV setting. Dante "
            "Controller, Domain Manager or Director owns the routes in Dante AV mode. This driver "
            "leaves that device setting alone.\n"
            "3. On the N2622S with ordinary control, allow outbound TCP 4197 for live HDMI events and "
            "output resolution. Polling and replies cover the Direct Control readings. HControl "
            "output resolution and HDCP readings clear if the event link closes.\n"
            "4. To control a serial display, add the display with its own serial driver, select "
            "Through a bridge and choose this receiver’s RS-232 port. Use this only where "
            "ordinary TCP 50004 is enabled. Match baud, data bits, parity and stop bits to the "
            "display, and use no flow control. Check Serial line setup and request status from "
            "the display. Unsolicited serial replies require AMX firmware 1.8.8 or later. Binary "
            "serial protocols that send NUL (00) require firmware 2.0.8 or later. Secure serial "
            "on 50104 is unavailable. Keep site security settings in place.\n"
            "5. Simulate receiver control and line settings in OpenAVC. The physical serial "
            "display and its replies require a separate peer.",
            "connection": "Only one connection can occupy the selected Direct Control port. Check the "
            "address, port, TLS setting, trust and command password. For absent audio or "
            "video, check stream format, local playback, mute, HDMI detection and the "
            "routing owner.",
        },
        "default_config": {
            "decoder_model": "Auto",
            "usb_sources": [],
            "url_sources": [],
            "host": "",
            "port": 50002,
            "ssl": False,
            "verify_ssl": True,
            "password": "",
            "routing_owner": "SVSI",
            "sources": [],
            "hdmi_events": True,
            "poll_interval": 10,
        },
        "config_schema": {
            "decoder_model": {
                "type": "enum",
                "label": "Decoder model",
                "values": ["Auto", "N2622S", "N3322D", "N2422A"],
                "default": "Auto",
                "help": "Auto identifies the connected decoder. Choose a model for offline design or simulation. Auto simulates the N2622S.",
            },
            "usb_sources": {
                "type": "table",
                "label": "N2422A keyboard and mouse encoders",
                "default": [],
                "row_label": "Encoder",
                "columns": {
                    "name": {
                        "type": "string",
                        "label": "Source name",
                        "required": True,
                    },
                    "address": {
                        "type": "string",
                        "label": "Encoder IPv4 address",
                        "required": True,
                    },
                },
            },
            "url_sources": {
                "type": "table",
                "label": "N3322D H.26x video sources",
                "default": [],
                "row_label": "Source",
                "help": "Each name appears in the Video Matrix alongside the numbered streams. Use stream addresses without credentials or query strings.",
                "columns": {
                    "name": {
                        "type": "string",
                        "label": "Source name",
                        "required": True,
                    },
                    "url": {"type": "string", "label": "Stream URL", "required": True},
                },
            },
            "host": {"type": "string", "label": "Decoder address", "required": True},
            "port": {
                "type": "integer",
                "label": "Control port",
                "default": 50002,
                "min": 1,
                "max": 65535,
                "help": "50002 for ordinary control or 50102 for secure control.",
            },
            "ssl": {
                "type": "boolean",
                "label": "Secure Direct Control (TLS)",
                "default": False,
            },
            "verify_ssl": {
                "type": "boolean",
                "label": "Verify receiver certificate",
                "default": True,
                "advanced": True,
            },
            "password": {
                "type": "string",
                "label": "Command password",
                "default": "",
                "secret": True,
                "help": "Required for secure control. Enter the command password "
                "configured on the receiver.",
            },
            "routing_owner": {
                "type": "enum",
                "label": "Routing configured on receiver",
                "values": ["SVSI", "Dante AV"],
                "default": "SVSI",
                "help": "Match the decoder’s Dante AV setting. This choice changes "
                "the available controls in OpenAVC.",
            },
            "sources": {
                "type": "table",
                "label": "SVSI sources",
                "default": [],
                "row_label": "Source",
                "columns": {
                    "name": {
                        "type": "string",
                        "label": "Source name",
                        "required": True,
                    },
                    "stream": {
                        "type": "integer",
                        "label": "Stream number",
                        "min": 1,
                        "max": 32511,
                        "required": True,
                    },
                },
            },
            "hdmi_events": {
                "type": "boolean",
                "label": "Listen for N2622S live HDMI changes",
                "default": True,
                "help": "Uses HControl on TCP 4197 with ordinary control. Secure "
                "control uses Direct Control feedback.",
            },
            "poll_interval": {
                "type": "integer",
                "label": "Status refresh (seconds)",
                "default": 10,
                "min": 0,
                "advanced": True,
            },
        },
        "discovery": {
            "port_open": [50002, 50102],
            "manufacturer_alias": ["amx", "svsi"],
            "tcp_probe": {
                "port": 50002,
                "send_ascii": "getStatus\r",
                "expect_regex": "(?i)SVSI_RXGEN2\\s*:?\\s*(?:NMX-DEC-)?(?:N2422A|N2622S|N3322D)(?!-C)(?=[\\d\\s:-]|$)",
                "timeout_ms": 1500,
            },
        },
        "bridge": {
            "ports": [
                {
                    "id": "serial:1",
                    "label": "RS-232 (ordinary TCP 50004)",
                    "kind": "serial",
                    "passthrough_port": 50004,
                }
            ]
        },
        "routing": {
            "source_param": "source",
            "planes": [
                {
                    "label": "Video",
                    "route_property": "video_source",
                    "command": "route_video",
                },
                {
                    "label": "Audio",
                    "route_property": "audio_selection",
                    "command": "route_audio",
                },
            ],
        },
        "state_variables": {
            "audio_sample_rate": {
                "type": "string",
                "label": "Reported audio sample rate",
            },
            "local_uses_stream_audio": {
                "type": "boolean",
                "label": "Local playback uses stream audio",
            },
            "hdmi_audio_mode": {
                "type": "string",
                "label": "HDMI audio output",
                "control": True,
            },
            "dante_av_h_enabled": {
                "type": "boolean",
                "label": "Dante AV-H owns routing",
                "cloud_priority": "high",
            },
            "hold_last_frame": {
                "type": "boolean",
                "label": "Hold last frame on signal loss",
                "control": True,
            },
            "disable_hdmi_on_loss": {
                "type": "boolean",
                "label": "Disable HDMI on signal loss",
                "control": True,
            },
            "identity": {
                "type": "string",
                "label": "Decoder identifier",
                "cloud_priority": "low",
            },
            "device_name": {"type": "string", "label": "Device name"},
            "firmware": {
                "type": "string",
                "label": "Firmware version",
                "cloud_priority": "low",
            },
            "mac": {"type": "string", "label": "MAC address", "cloud_priority": "low"},
            "ip": {"type": "string", "label": "IP address", "cloud_priority": "low"},
            "switch_chassis": {
                "type": "string",
                "label": "Connected switch chassis ID",
                "cloud_priority": "low",
            },
            "switch_name": {
                "type": "string",
                "label": "Connected switch",
                "cloud_priority": "low",
            },
            "switch_port": {
                "type": "string",
                "label": "Connected switch port",
                "cloud_priority": "low",
            },
            "switch_port_description": {
                "type": "string",
                "label": "Switch port description",
                "cloud_priority": "low",
            },
            "input_resolution": {
                "type": "string",
                "label": "Incoming video resolution",
            },
            "output_resolution": {
                "type": "string",
                "label": "HDMI output resolution (HControl)",
            },
            "hdcp_status": {"type": "string", "label": "HDCP output status (HControl)"},
            "hdmi_feedback": {"type": "string", "label": "HDMI feedback connection"},
            "serial_setup": {
                "type": "string",
                "label": "Serial line setup",
                "cloud_priority": "high",
            },
            "audio_source": {"type": "string", "label": "Decoder audio source"},
            "kvm_host": {"type": "string", "label": "KVM host"},
            "usb_host": {"type": "string", "label": "USB 2.0 host"},
            "serial_parity": {
                "type": "string",
                "label": "Serial parity",
                "cloud_priority": "low",
            },
            "receive_mode": {"type": "string", "label": "Stream format"},
            "play_mode": {"type": "string", "label": "Playback mode"},
            "scaler_mode": {"type": "string", "label": "Scaler output mode"},
            "video_source": {
                "type": "string",
                "label": "Video stream selection",
                "control": True,
                "cloud_priority": "high",
            },
            "audio_selection": {
                "type": "string",
                "label": "Audio stream selection (0 follows video)",
                "control": True,
                "cloud_priority": "high",
            },
            "video_stream": {"type": "integer", "label": "Video stream number"},
            "audio_stream": {
                "type": "integer",
                "label": "Audio stream number (0 follows video)",
            },
            "playlist": {"type": "integer", "label": "Local playlist"},
            "left_volume": {
                "type": "integer",
                "label": "Left line output (%)",
                "min": 0,
                "max": 100,
                "unit": "%",
                "control": True,
            },
            "right_volume": {
                "type": "integer",
                "label": "Right line output (%)",
                "min": 0,
                "max": 100,
                "unit": "%",
                "control": True,
            },
            "serial_baud": {
                "type": "integer",
                "label": "Serial baud rate",
                "cloud_priority": "low",
            },
            "serial_bits": {
                "type": "integer",
                "label": "Serial data bits",
                "cloud_priority": "low",
            },
            "serial_stops": {
                "type": "integer",
                "label": "Serial stop bits",
                "cloud_priority": "low",
            },
            "wall_columns": {"type": "integer", "label": "Video wall columns"},
            "wall_rows": {"type": "integer", "label": "Video wall rows"},
            "wall_column": {"type": "integer", "label": "Display column"},
            "wall_row": {"type": "integer", "label": "Display row"},
            "audio_muted": {"type": "boolean", "label": "Audio muted", "control": True},
            "video_muted": {"type": "boolean", "label": "Video muted", "control": True},
            "hdmi_enabled": {
                "type": "boolean",
                "label": "HDMI output enabled",
                "control": True,
            },
            "hdmi_connected": {
                "type": "boolean",
                "label": "HDMI display detected",
                "control": False,
                "cloud_priority": "high",
            },
            "scaler_enabled": {
                "type": "boolean",
                "label": "Output scaler enabled",
                "control": True,
            },
            "usb_enabled": {
                "type": "boolean",
                "label": "USB 2.0 enabled",
                "control": True,
            },
            "kvm_enabled": {"type": "boolean", "label": "KVM enabled", "control": True},
            "wall_enabled": {
                "type": "boolean",
                "label": "Video wall enabled",
                "control": True,
            },
            "identified": {
                "type": "boolean",
                "label": "Identification active",
                "control": False,
            },
            "stream_url": {"type": "string", "label": "H.26x stream URL"},
        },
        "commands": {
            "route_video": {
                "label": "Route video by source",
                "params": {
                    "source": {
                        "type": "enum",
                        "label": "Source",
                        "required": True,
                        "values": [],
                    }
                },
                "sets": {"video_source": "{source}"},
                "help": "Choose an encoder from Sources. The receiver reports the stream "
                "it selected. Check incoming resolution separately for a picture.",
            },
            "set_video_stream": {
                "label": "Select video stream number",
                "params": {
                    "stream": {
                        "type": "integer",
                        "label": "Stream number",
                        "required": True,
                        "min": 1,
                        "max": 32511,
                    }
                },
                "sets": {"video_stream": "{stream}"},
            },
            "route_audio": {
                "label": "Route audio by source",
                "params": {
                    "source": {
                        "type": "enum",
                        "label": "Source",
                        "required": True,
                        "values": [],
                    }
                },
                "sets": {"audio_selection": "{source}"},
                "help": "Choose an encoder or Follow video. This selects the SVSI audio "
                "stream. The decoder can also be configured for Dante audio.",
            },
            "set_audio_stream": {
                "label": "Select audio stream number",
                "params": {
                    "stream": {
                        "type": "integer",
                        "label": "Stream number",
                        "required": True,
                        "min": 0,
                        "max": 32511,
                    }
                },
                "sets": {"audio_stream": "{stream}"},
            },
            "follow_video": {
                "label": "Have audio follow video",
                "sets": {"audio_stream": 0},
                "help": "Sets the audio stream to 0. Subsequent video route changes carry "
                "the audio selection with them.",
            },
            "set_audio_muted": {
                "label": "Set Audio muted",
                "params": {
                    "enabled": {"type": "boolean", "label": "Enabled", "required": True}
                },
                "sets": {"audio_muted": "{enabled}"},
            },
            "set_video_muted": {
                "label": "Set Video muted",
                "params": {
                    "enabled": {"type": "boolean", "label": "Enabled", "required": True}
                },
                "sets": {"video_muted": "{enabled}"},
            },
            "set_hdmi_enabled": {
                "label": "Set HDMI output enabled",
                "params": {
                    "enabled": {"type": "boolean", "label": "Enabled", "required": True}
                },
                "sets": {"hdmi_enabled": "{enabled}"},
            },
            "set_scaler_enabled": {
                "label": "Set Output scaler enabled",
                "params": {
                    "enabled": {"type": "boolean", "label": "Enabled", "required": True}
                },
                "sets": {"scaler_enabled": "{enabled}"},
            },
            "set_usb_enabled": {
                "label": "Set USB 2.0 enabled",
                "params": {
                    "enabled": {"type": "boolean", "label": "Enabled", "required": True}
                },
                "sets": {"usb_enabled": "{enabled}"},
            },
            "set_kvm_enabled": {
                "label": "Set KVM enabled",
                "params": {
                    "enabled": {"type": "boolean", "label": "Enabled", "required": True}
                },
                "sets": {"kvm_enabled": "{enabled}"},
            },
            "set_wall_enabled": {
                "label": "Set Video wall enabled",
                "params": {
                    "enabled": {"type": "boolean", "label": "Enabled", "required": True}
                },
                "sets": {"wall_enabled": "{enabled}"},
            },
            "set_left_volume": {
                "label": "Set left volume (%)",
                "params": {
                    "level": {
                        "type": "integer",
                        "label": "Level (%)",
                        "min": 0,
                        "max": 100,
                        "required": True,
                    }
                },
                "sets": {"left_volume": "{level}"},
            },
            "set_right_volume": {
                "label": "Set right volume (%)",
                "params": {
                    "level": {
                        "type": "integer",
                        "label": "Level (%)",
                        "min": 0,
                        "max": 100,
                        "required": True,
                    }
                },
                "sets": {"right_volume": "{level}"},
            },
            "set_stereo_volume": {
                "label": "Set stereo volume (%)",
                "params": {
                    "level": {
                        "type": "integer",
                        "label": "Level (%)",
                        "min": 0,
                        "max": 100,
                        "required": True,
                    }
                },
                "sets": {"left_volume": "{level}", "right_volume": "{level}"},
                "help": "Sets both analog outputs to the same level. Read the left "
                "and right channels separately.",
            },
            "set_scaler_mode": {
                "label": "Set scaler output mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "label": "Output timing",
                        "required": True,
                        "values": [
                            "auto",
                            "720p50",
                            "720p60",
                            "1080p24",
                            "1080p50",
                            "1080p60",
                            "1400x1050p60",
                            "1920x1200p60",
                            "2160p25",
                            "2160p30",
                            "2160p50",
                            "2160p60",
                            "2560x1440p60",
                            "2560x1600p60",
                            "1440x900p60",
                            "1366x768p60",
                        ],
                    }
                },
                "sets": {"scaler_mode": "{mode}"},
                "help": "Sets the output timing used while scaling is enabled. The "
                "receiver supports downscaling 4K60 to 1080p.",
            },
            "set_receive_mode": {
                "label": "Select stream format",
                "params": {
                    "mode": {
                        "type": "enum",
                        "label": "Format",
                        "required": True,
                        "values": ["MWC", "H26X"],
                    }
                },
                "sets": {"receive_mode": "{mode}"},
                "help": "Select Motion Wavelet (MWC) or H.26x. Set the H.26x stream "
                "URL separately.",
            },
            "set_stream_url": {
                "label": "Set H.26x stream URL",
                "params": {
                    "url": {"type": "string", "label": "Stream URL", "required": True}
                },
                "sets": {"stream_url": "{url}"},
                "help": "Sets the stored address. Choose H26X as the stream format to "
                "receive it. Enter private URLs in the receiver web page.",
            },
            "set_wall_layout": {
                "label": "Set video wall layout and position",
                "params": {
                    "rows": {
                        "type": "integer",
                        "label": "Rows",
                        "min": 1,
                        "max": 16,
                        "required": True,
                    },
                    "columns": {
                        "type": "integer",
                        "label": "Columns",
                        "min": 1,
                        "max": 16,
                        "required": True,
                    },
                    "row": {
                        "type": "integer",
                        "label": "Row",
                        "min": 1,
                        "max": 16,
                        "required": True,
                    },
                    "column": {
                        "type": "integer",
                        "label": "Column",
                        "min": 1,
                        "max": 16,
                        "required": True,
                    },
                },
                "sets": {
                    "wall_rows": "{rows}",
                    "wall_columns": "{columns}",
                    "wall_row": "{row}",
                    "wall_column": "{column}",
                },
                "help": "Sets the wall dimensions and the position of this receiver. "
                "Video wall enable is separate.",
            },
            "set_usb_host": {
                "label": "Route KVM and USB to encoder",
                "params": {
                    "address": {
                        "type": "string",
                        "label": "Encoder IPv4 address",
                        "required": True,
                    }
                },
                "sets": {"usb_host": "{address}", "kvm_host": "{address}"},
                "help": "Sets the KVM and USB 2.0 encoder host addresses together. Each "
                "function has its own enable setting.",
            },
            "play_local": {
                "label": "Play local playlist",
                "params": {
                    "playlist": {
                        "type": "integer",
                        "label": "Playlist",
                        "required": True,
                        "min": 1,
                        "max": 8,
                    }
                },
                "sets": {"playlist": "{playlist}", "play_mode": "local"},
                "help": "Switches to one of the eight playlists stored on the receiver.",
            },
            "play_live": {
                "label": "Return to live stream",
                "sets": {"play_mode": "live"},
                "help": "Leaves local playback and returns to the incoming stream.",
            },
            "display_cec": {
                "label": "Send display power via CEC",
                "params": {
                    "power": {
                        "type": "enum",
                        "label": "Power",
                        "values": ["on", "off"],
                        "required": True,
                    }
                },
                "help": "Sends HDMI CEC power to the attached display. The receiver does "
                "not confirm display power.",
            },
            "send_saved_ir": {
                "label": "Send saved IR command",
                "params": {
                    "name": {
                        "type": "string",
                        "label": "Command name on receiver",
                        "required": True,
                    }
                },
                "help": "Enter a command already learned or stored on this receiver. IR "
                "provides no confirmation from the controlled display.",
            },
            "identify": {
                "label": "Identify this decoder",
                "sets": {"identified": True},
            },
            "refresh_network": {
                "label": "Read connected switch information",
                "query_for": "switch_port",
                "help": "Reads the connected switch and port advertised to the "
                "receiver. Switch-provided labels may be empty.",
            },
            "refresh_status": {"label": "Read decoder status", "query_for": "identity"},
            "read_audio_sample_rate": {
                "label": "Read audio sample rate",
                "query_for": "audio_sample_rate",
            },
            "read_local_uses_stream_audio": {
                "label": "Read audio source policy during local playback",
                "query_for": "local_uses_stream_audio",
            },
            "read_hdmi_audio_mode": {
                "label": "Read HDMI audio output setting",
                "query_for": "hdmi_audio_mode",
            },
            "set_hdmi_audio_mode": {
                "label": "Set HDMI audio output",
                "params": {
                    "mode": {
                        "type": "enum",
                        "label": "HDMI audio",
                        "required": True,
                        "values": ["auto", "off"],
                    }
                },
                "help": "Auto sends audio when the display supports it. Off suppresses HDMI audio.",
                "sets": {"hdmi_audio_mode": "{mode}"},
            },
            "read_dante_av_h_enabled": {
                "label": "Read Dante AV-H routing ownership",
                "query_for": "dante_av_h_enabled",
            },
            "read_audio_source": {
                "label": "Read receiver audio source",
                "query_for": "audio_source",
            },
            "select_audio_source": {
                "label": "Select audio input type",
                "params": {
                    "source": {
                        "type": "enum",
                        "label": "Audio input",
                        "values": ["svsi", "dante", "H.26x"],
                        "required": True,
                    }
                },
                "help": "Select numbered SVSI audio, Dante audio or H.26x stream audio. Inspect the decoder audio source reading. AMX leaves the AUDIOSTREAM return encoding unspecified.",
            },
            "set_hold_last_frame": {
                "label": "Hold last frame on signal loss",
                "params": {
                    "enabled": {"type": "boolean", "label": "Enabled", "required": True}
                },
                "sets": {"hold_last_frame": "{enabled}"},
            },
            "read_hold_last_frame": {
                "label": "Read last frame hold",
                "query_for": "hold_last_frame",
            },
            "set_disable_hdmi_on_loss": {
                "label": "Disable HDMI on signal loss",
                "params": {
                    "enabled": {"type": "boolean", "label": "Enabled", "required": True}
                },
                "sets": {"disable_hdmi_on_loss": "{enabled}"},
            },
            "read_disable_hdmi_on_loss": {
                "label": "Read HDMI behavior on signal loss",
                "query_for": "disable_hdmi_on_loss",
            },
            "read_video_source": {
                "label": "Read Video stream selection",
                "query_for": "video_source",
            },
            "read_audio_selection": {
                "label": "Read Audio stream selection (0 follows video)",
                "query_for": "audio_selection",
            },
            "read_video_stream": {
                "label": "Read Video stream number",
                "query_for": "video_stream",
            },
            "read_audio_stream": {
                "label": "Read Audio stream number (0 follows video)",
                "query_for": "audio_stream",
            },
            "read_audio_muted": {
                "label": "Read Audio muted",
                "query_for": "audio_muted",
            },
            "read_video_muted": {
                "label": "Read Video muted",
                "query_for": "video_muted",
            },
            "read_left_volume": {
                "label": "Read Left line output (%)",
                "query_for": "left_volume",
            },
            "read_right_volume": {
                "label": "Read Right line output (%)",
                "query_for": "right_volume",
            },
            "read_hdmi_enabled": {
                "label": "Read HDMI output enabled",
                "query_for": "hdmi_enabled",
            },
            "read_scaler_enabled": {
                "label": "Read Output scaler enabled",
                "query_for": "scaler_enabled",
            },
            "read_usb_enabled": {
                "label": "Read USB 2.0 enabled",
                "query_for": "usb_enabled",
            },
            "read_kvm_enabled": {
                "label": "Read KVM enabled",
                "query_for": "kvm_enabled",
            },
            "read_wall_enabled": {
                "label": "Read Video wall enabled",
                "query_for": "wall_enabled",
            },
            "read_play_mode": {"label": "Read Playback mode", "query_for": "play_mode"},
            "read_playlist": {"label": "Read Local playlist", "query_for": "playlist"},
            "read_scaler_mode": {
                "label": "Read Scaler output mode",
                "query_for": "scaler_mode",
            },
            "read_receive_mode": {
                "label": "Read Stream format",
                "query_for": "receive_mode",
            },
            "read_usb_host": {"label": "Read USB 2.0 host", "query_for": "usb_host"},
            "read_kvm_host": {"label": "Read KVM host", "query_for": "kvm_host"},
            "read_identified": {
                "label": "Read Identification active",
                "query_for": "identified",
            },
            "read_hdmi_connected": {
                "label": "Read HDMI display detected",
                "query_for": "hdmi_connected",
            },
            "read_stream_url": {
                "label": "Read H.26x stream URL",
                "query_for": "stream_url",
            },
            "read_wall_rows": {
                "label": "Read Video wall rows",
                "query_for": "wall_rows",
            },
            "read_wall_columns": {
                "label": "Read Video wall columns",
                "query_for": "wall_columns",
            },
            "read_wall_column": {
                "label": "Read Display column",
                "query_for": "wall_column",
            },
            "read_wall_row": {"label": "Read Display row", "query_for": "wall_row"},
        },
        "device_settings": {
            "hdmi_audio_mode": {
                "type": "enum",
                "label": "HDMI audio output",
                "values": ["auto", "off"],
                "state_key": "hdmi_audio_mode",
                "setup": False,
            },
            "hold_last_frame": {
                "type": "boolean",
                "label": "Hold last frame on signal loss",
                "state_key": "hold_last_frame",
                "setup": False,
            },
            "disable_hdmi_on_loss": {
                "type": "boolean",
                "label": "Disable HDMI on signal loss",
                "state_key": "disable_hdmi_on_loss",
                "setup": False,
            },
            "scaler_enabled": {
                "type": "boolean",
                "label": "Output scaler enabled",
                "state_key": "scaler_enabled",
                "setup": False,
            },
            "scaler_mode": {
                "type": "enum",
                "label": "Scaler output mode",
                "values": [
                    "auto",
                    "720p50",
                    "720p60",
                    "1080p24",
                    "1080p50",
                    "1080p60",
                    "1400x1050p60",
                    "1920x1200p60",
                    "2160p25",
                    "2160p30",
                    "2160p50",
                    "2160p60",
                    "2560x1440p60",
                    "2560x1600p60",
                    "1440x900p60",
                    "1366x768p60",
                ],
                "state_key": "scaler_mode",
                "setup": False,
            },
            "receive_mode": {
                "type": "enum",
                "label": "Stream format",
                "values": ["MWC", "H26X"],
                "state_key": "receive_mode",
                "setup": False,
            },
            "wall_enabled": {
                "type": "boolean",
                "label": "Video wall enabled",
                "state_key": "wall_enabled",
                "setup": False,
            },
            "usb_enabled": {
                "type": "boolean",
                "label": "USB 2.0 enabled",
                "state_key": "usb_enabled",
                "setup": False,
            },
            "kvm_enabled": {
                "type": "boolean",
                "label": "KVM enabled",
                "state_key": "kvm_enabled",
                "setup": False,
            },
        },
        "actions": [
            {"id": "identify"},
            {"id": "refresh_status"},
            {"id": "refresh_network"},
            {"id": "display_cec"},
            {"id": "play_local"},
            {"id": "play_live"},
        ],
        "quick_actions": [
            "route_video",
            "follow_video",
            "set_audio_muted",
            "play_live",
            "refresh_status",
        ],
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._model = self.config.get("decoder_model", "Auto")
        self._identifying = True
        self._ownership = None
        self._route_lock = asyncio.Lock()
        self._missing_routes = set()
        self._packet = {}
        self._secure = False
        self._lock = asyncio.Lock()
        self._serial_lock = asyncio.Lock()
        self._reply = None
        self._header = None
        self._in_packet = False
        self._needed = set()
        self._fresh = {}
        self._watch = None
        self._configure_surface()

    def _configure_surface(self):
        self._n24 = self._model == "N2422A"
        self._n33 = self._model == "N3322D"
        info = deepcopy(type(self).DRIVER_INFO)
        self.DRIVER_INFO = info
        self._route_keys = {"N2422A": N24_FEEDBACK, "N3322D": MATRIX_READINGS}.get(
            self._model, set()
        )
        self._source_ids = set()
        self._urls = set()
        fields = FIELDS
        if self._model in ("N2622S", "N3322D"):
            info["config_schema"].pop("usb_sources")
        if self._n24:
            self._n24_surface(info)
            fields = N24_FIELDS
        elif self._n33:
            self._n33_surface(info)
            fields = N33_FIELDS
        else:
            self._omit(N33_ONLY, {"select_audio_source", "read_audio_source"})
            if self._model == "N2622S":
                info["config_schema"].pop("url_sources")
        self._fields = {
            k: spec for k, spec in fields.items() if spec[0] in info["state_variables"]
        }
        self._configure_routing()

    def _omit(self, states, commands):
        info = self.DRIVER_INFO
        for key in states:
            info["state_variables"].pop(key, None)
            info["device_settings"].pop(key, None)
        for cmd in (
            commands
            | {"read_" + key for key in states}
            | {"set_" + key for key in states}
        ):
            info["commands"].pop(cmd, None)

    def _n24_surface(self, info):
        self._omit(
            {
                "audio_source",
                "audio_sample_rate",
                "dante_av_h_enabled",
                "receive_mode",
                "stream_url",
                "identified",
                "output_resolution",
                "hdcp_status",
                "kvm_host",
                "usb_host",
                "wall_enabled",
                "wall_rows",
                "wall_columns",
                "wall_row",
                "wall_column",
            },
            {"select_audio_source", "set_wall_layout", "identify", "display_cec"},
        )
        for key in ("hdmi_events", "routing_owner", "url_sources"):
            info["config_schema"].pop(key)
        info["source_url"] = (
            "https://www.amx.com/en-US/site_elements/n-series-n2400-direct-control-api"
        )
        info["protocols"] = ["amx-svsi-direct-control"]
        info["help"]["setup"] = (
            "Select N2422A for offline design or simulation. Auto identifies a connected decoder. "
            "Use TCP 50002, or TLS on 50102 with the receiver command password. Only one controller can hold the connection.\n"
            "Name the N2400 video and audio streams under Sources. Audio can follow video or stay on another encoder. "
            "AES67 audio clears the SVSI audio selection. Local playback clears the live video selection. Check incoming resolution and display detection separately.\n"
            "To use the RS-232 connector, bind a serial device on ordinary port 50004 and match its line settings."
        )
        info["help"]["connection"] = (
            "Check the address, control port, TLS setting and command password. For a missing picture, check live playback, incoming resolution, mute and display detection."
        )
        commands = info["commands"]
        self._n24_usb_choices(info)
        self._n24_audio_readings(info)
        for key in (
            "usb_enabled",
            "kvm_enabled",
            "hdmi_audio_mode",
            "hold_last_frame",
            "disable_hdmi_on_loss",
        ):
            info["device_settings"].pop(key)
            commands.pop("set_" + key)
            info["state_variables"][key]["control"] = False
        info["state_variables"]["usb_enabled"]["label"] = (
            "Keyboard and mouse USB enabled"
        )
        commands["route_audio"]["help"] = (
            "Select an independent N2400 audio stream or choose Follow video."
        )
        commands["route_video"]["help"] = (
            "Select the named N2400 stream and return to live playback."
        )
        commands["set_scaler_mode"]["params"]["mode"]["values"] = N24_MODES
        info["device_settings"]["scaler_mode"]["values"] = N24_MODES
        info["actions"] = [a for a in info["actions"] if a["id"] in commands]
        info["quick_actions"] = [
            "route_video",
            "route_audio",
            "route_usb",
            "play_local",
            "refresh_status",
        ]

    def _n24_usb_choices(self, info):
        commands = info["commands"]
        hosts, choices = source_choices(self.config.get("usb_sources"), usb_source)
        self._usb_hosts = hosts
        usb_help = (
            "Moves only keyboard and mouse control. Verify the connected encoder under KVM IP on the decoder’s Settings page. "
            "Direct Control reports USB and KVM enablement only."
        )
        commands["route_usb"] = {
            "label": "Route keyboard and mouse by source",
            "help": usb_help,
            "params": {
                "source": {
                    "type": "enum",
                    "label": "USB encoder",
                    "required": True,
                    "values": choices,
                }
            },
        }
        commands["set_usb_host"] = {
            "label": "Route keyboard and mouse by encoder address",
            "help": usb_help,
            "params": {
                "address": {
                    "type": "string",
                    "label": "Encoder IPv4 address",
                    "required": True,
                }
            },
        }
        info["help"]["setup"] += (
            "\nName keyboard and mouse encoders by their IP addresses. " + usb_help
        )

    def _n24_audio_readings(self, info):
        commands = info["commands"]
        info["state_variables"]["aes67_enabled"] = {
            "type": "boolean",
            "label": "AES67 audio selected",
            "cloud_priority": "high",
        }
        info["state_variables"]["aes67_address"] = {
            "type": "string",
            "label": "Configured AES67 audio source",
        }
        for key in ("aes67_enabled", "aes67_address"):
            commands["read_" + key] = {
                "label": "Read " + info["state_variables"][key]["label"],
                "query_for": key,
            }
        info["routing"]["planes"][1]["label"] = "SVSI audio assignment"
        info["state_variables"]["audio_selection"]["label"] = (
            "SVSI audio assignment (0 follows video)"
        )

    def _n33_surface(self, info):
        self._omit(N26_ONLY, {"set_wall_layout", "set_usb_host"})
        info["source_url"] = (
            "https://www.amx.com/en/site_elements/direct-control-api-n33x2d"
        )
        info["protocols"] = ["amx-svsi-direct-control"]
        info["config_schema"].pop("hdmi_events")
        info["help"]["setup"] = (
            "1. Select N3322D for offline design or simulation, or let Auto identify a live receiver. Use ordinary TCP 50002 or TLS 50102 with its command password.\n"
            "2. Name SVSI stream numbers and H.26x video URLs. A Video Matrix selection recalls live playback and the source format. Check incoming resolution and HDMI detection separately.\n"
            "3. Audio Matrix assigns the numbered SVSI audio stream. Choose Follow video for stream 0. Select audio input type separately for SVSI, Dante audio or H.26x. "
            "Dante AV-H ownership disables OpenAVC route changes. The local Dante AV project setting also reserves routing for Dante.\n"
            "4. Local playback reports its mode. Check the receiver for the playing playlist number. Set the two signal-loss policies under Device Settings.\n"
            "5. Bind a serial display through this decoder only on ordinary TCP 50004. The driver verifies the line settings. Secure downstream serial on 50104 requires an independent connection."
        )
        commands = info["commands"]
        commands["play_local"]["sets"] = {"play_mode": "local"}
        commands["play_local"]["help"] = (
            "Request playlist 1–8. The decoder confirms local mode and does not report the slot."
        )
        for key, values in (
            ("scaler_mode", N33_MODES),
            ("receive_mode", ["SVSI", "H26X"]),
        ):
            commands["set_" + key]["params"]["mode"]["values"] = values
            info["device_settings"][key]["values"] = values
        commands["set_receive_mode"]["help"] = (
            "Choose numbered SVSI video or the configured H.26x URL."
        )
        commands["route_video"]["help"] = (
            "Recall a named stream number or URL and return to live playback. The highlighted source comes from decoder feedback."
        )
        commands["route_audio"]["help"] = (
            "Set the SVSI audio stream assignment. Select the receiver audio input type separately."
        )
        info["routing"]["planes"][1]["label"] = "SVSI audio assignment"
        info["state_variables"]["video_source"]["label"] = "Selected live video source"
        info["state_variables"]["audio_selection"]["label"] = (
            "SVSI audio assignment (0 follows video)"
        )
        info["quick_actions"] = [
            "route_video",
            "route_audio",
            "select_audio_source",
            "play_live",
            "refresh_status",
        ]

    def _configure_routing(self):
        if not self._n24 and self._routes_reserved():
            self._use_dante_routing()
            return
        self._source_ids, streams = source_choices(
            self.config.get("sources"), numbered_source
        )
        urls = []
        if self._n33:
            self._urls, urls = source_choices(
                self.config.get("url_sources"), url_source
            )
        commands = self.DRIVER_INFO["commands"]
        commands["route_video"]["params"]["source"]["values"] = streams + urls
        commands["route_audio"]["params"]["source"]["values"] = [
            {"value": "0", "label": "Follow video"}
        ] + streams

    def _routes_reserved(self):
        return self.config.get("routing_owner") == "Dante AV" or (
            self._n33 and self._ownership
        )

    def _use_dante_routing(self):
        self.DRIVER_INFO.pop("routing")
        for cmd in OWNED_COMMANDS:
            self.DRIVER_INFO["commands"].pop(cmd, None)
        self.DRIVER_INFO["device_settings"].pop("receive_mode")
        self.DRIVER_INFO["quick_actions"] = [
            c for c in self.DRIVER_INFO["quick_actions"] if c not in OWNED_COMMANDS
        ]

    async def _pre_connect(self):
        self._identifying = True
        self._missing_routes.clear()
        port = int(self.config.get("port", 50002))
        self._secure = secure = bool(self.config.get("ssl"))
        wrong_ports = (50001, 50002) if secure else (50101, 50102)
        if port in wrong_ports:
            raise ConnectionFaultError(
                "Match the control port and TLS setting: ordinary 50002 or secure 50102.",
                code="invalid_config",
            )
        if secure:
            self._check_password()

    def _check_password(self):
        password = self.config.get("password") or ""
        if not password or re.search(r"[\r\n\x00]", password):
            raise ConnectionFaultError(
                "Enter the receiver command password without line breaks.",
                code="invalid_config",
            )

    async def _post_connect(self):
        report = await self._request("getStatus", {"identity", "firmware"})
        match = re.match(MODEL_PATTERN, report["identity"], re.I)
        if not match:
            raise ConnectionFaultError(
                "Unsupported AMX decoder identity.", code="invalid_config"
            )
        actual = match[1].upper()
        if self.config.get("decoder_model", "Auto") not in ("Auto", actual):
            raise ConnectionFaultError(
                "The connected decoder differs from the selected model.",
                code="invalid_config",
            )
        if self._model != actual:
            self._model = actual
            self._configure_surface()
        self._identifying = False
        await self._request("getStatus", {"identity", "firmware"})

    async def _initial_sync(self):
        await self._request("getNetStatus", {"mac"}, header=NETWORK)
        if all(
            (
                self._model == "N2622S",
                self.config.get("hdmi_events", True),
                not self._secure,
            )
        ):
            self._clear_hdmi("Direct Control; connecting to HControl")
            self._watch = asyncio.create_task(self._watch_hdmi())
        else:
            self.set_state("hdmi_feedback", "Direct Control")

    async def _close_session(self):
        if self._reply is not None and not self._reply.done():
            self._reply.set_exception(ConnectionError("Decoder connection closed."))
        if self._watch is not None:
            self._watch.cancel()
            await asyncio.gather(self._watch, return_exceptions=True)
            self._watch = None
        self._clear_hdmi("Disconnected")
        self._packet.clear()
        self._missing_routes.clear()
        keys = self._route_keys
        if self._n24:
            keys = set(self.DRIVER_INFO["state_variables"]) - {"hdmi_feedback"}
        for key in keys:
            self.set_state(key, None)

    async def _send(self, wire):
        if self.transport is None:
            raise ConnectionError("Decoder is disconnected.")
        command = (wire + "\r").encode("ascii")
        if not self._secure:
            await self.transport.send(command)
            return
        credential = (self.config["password"] + "\r").encode()
        with get_traffic_recorder().hold(self.device_id, tx=True):
            await self.transport.send(credential + command)
        self.record_traffic(
            "tx",
            command,
            meta={
                "protocol": "Secure Direct Control",
                "password_prefix_withheld": True,
            },
        )

    async def _request(self, wire, keys, header=STATUS):
        async with self._lock:
            self._header = header
            self._in_packet = False
            self._needed = set(keys)
            self._fresh = {}
            self._reply = asyncio.get_running_loop().create_future()
            try:
                await self._send(wire)
                return await asyncio.wait_for(self._reply, 4)
            except TimeoutError:
                # Direct Control has no transaction IDs. A late reply must never
                # satisfy another write on the same session.
                if self._in_packet:
                    self._clear_missing_routes()
                    raise CommandRefusedError(
                        "The decoder answered but omitted a requested status field."
                    ) from None
                if self.connected:
                    self._force_disconnect(
                        message="No complete reply from the decoder."
                    )
                raise ConnectionError(
                    "Decoder did not return the requested status fields."
                ) from None
            finally:
                self._reply.cancel()
                self._reply = None
                self._header = None

    async def on_data_received(self, data):
        for line in data.decode("utf-8", "replace").splitlines():
            wire, value = split_field(line.strip())
            self._receive(wire, value)

    def _receive(self, wire, value):
        if wire in (STATUS, NETWORK):
            self._begin_packet(wire)
        update = self._status_update(wire, value)
        self._missing_routes.difference_update(update)
        for key, reading in update.items():
            self.set_state(key, reading)
        self._track_reply(update)

    def _track_reply(self, update):
        if self._in_packet and self._waiting_for_reply():
            self._fresh.update(update)
            if self._needed.issubset(self._fresh):
                self._reply.set_result(dict(self._fresh))

    def _begin_packet(self, header):
        # CR-delimited fields have no end marker. A new header closes the
        # prior report, so an omitted reading cannot leave a stale crosspoint.
        self._clear_missing_routes()
        self._packet = {}
        if self._n24 and self._header == NETWORK:
            # N2400 uses the same RX header for both replies.
            header = NETWORK
        self._in_packet = header == self._header
        self._fresh = {}
        if header == STATUS and not self._identifying:
            self._missing_routes = self._route_keys.copy()

    def _clear_missing_routes(self):
        if "dante_av_h_enabled" in self._missing_routes:
            self._set_route_ownership("unknown")
        for key in self._missing_routes:
            self.set_state(key, None)
        self._missing_routes.clear()

    def _set_route_ownership(self, owner):
        if self._ownership != owner:
            self._ownership = owner
            self._configure_surface()

    def _status_update(self, wire, value):
        if self._identifying and wire not in (STATUS, "SWVER"):
            return {}
        if wire not in self._fields:
            return {}
        update = read_field(self._fields[wire], value)
        if self._n24:
            self._packet.update(update)
            if update.keys() & N24_ROUTE_INPUTS:
                update.update(n24_selections(self._packet))
            return update
        if self._n33:
            return self._with_routes(update)
        for key in LEGACY_SELECTIONS.keys() & update.keys():
            update[LEGACY_SELECTIONS[key]] = str(update[key])
        return update

    def _with_routes(self, update):
        if update.keys() & ROUTE_READINGS:
            self._packet.update(update)
            update.update(self._n33_routes())
        return update

    def _n33_routes(self):
        packet = self._packet
        owner = packet.get("dante_av_h_enabled")
        if owner is None:
            return {}
        self._set_route_ownership(owner)
        if self._routes_reserved():
            return {"video_source": "", "audio_selection": ""}
        routes = {}
        if "audio_stream" in packet:
            routes["audio_selection"] = str(packet["audio_stream"])
        routes.update(video_selection(packet))
        return routes

    def _waiting_for_reply(self):
        return self._reply is not None and not self._reply.done()

    async def poll(self):
        await self._request("getStatus", {"identity", "firmware"})
        if self._n24 and self._missing_routes:
            # A network packet closes a fragmented status report. Missing
            # readings expire independently, even with polling turned off later.
            await self._request("getNetStatus", {"mac"}, header=NETWORK)

    async def _liveness_probe(self):
        await self._request("getStatus", {"identity"})

    def _clear_hdmi(self, mode):
        self.set_state("hdmi_feedback", mode)
        if self._model in ("Auto", "N2622S"):
            self.set_state("output_resolution", None)
            self.set_state("hdcp_status", None)

    async def _watch_hdmi(self):
        while True:
            try:
                await self._hdmi_session()
            except (OSError, ValueError, TimeoutError, ConnectionError):
                self._clear_hdmi("Direct Control; HControl unavailable")
            await asyncio.sleep(15)

    async def _hdmi_session(self):
        host, port = self.udp_redirect or (self.config["host"], 4197)
        ip = self.local_address(host)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, local_addr=(ip, 0) if ip else None), 3
        )
        try:
            for event in HDMI_EVENTS:
                await self._hc_send(writer, "subscribeevent", event)
            for prop in HDMI_READS:
                await self._hc_send(writer, "get", prop)
            await self._hdmi_messages(reader, writer)
        finally:
            writer.close()
            await writer.wait_closed()

    async def _hc_send(self, writer, verb, prop):
        message = verb + " " + json.dumps({"path": HDMI_ROOT + prop}) + "\n"
        writer.write(message.encode())
        await writer.drain()
        self.record_traffic("tx", message, meta={"protocol": "HControl"})

    async def _hdmi_messages(self, reader, writer):
        subscribed = set()
        while True:
            try:
                line = await asyncio.wait_for(reader.readline(), 30)
            except TimeoutError:
                await self._hc_send(writer, "get", "hdcpStatus")
                line = await asyncio.wait_for(reader.readline(), 4)
            if not line:
                raise ConnectionError("HControl closed.")
            self.record_traffic("rx", line, meta={"protocol": "HControl"})
            await self._hdmi_line(writer, line, subscribed)

    async def _hdmi_line(self, writer, line, subscribed):
        verb, sep, body = line.decode("utf-8", "replace").strip().partition(" ")
        if not sep or verb not in ("@get", "@subscribeevent", "event"):
            return
        packet = valid_hc_packet(body)
        if packet is None:
            return
        prop = packet["path"][len(HDMI_ROOT) :]
        if verb == "@get":
            if prop in HDMI_READS:
                self._apply_hdmi(prop, packet.get("value"))
            return
        await self._hdmi_subscription(writer, verb, prop, subscribed)

    async def _hdmi_subscription(self, writer, verb, prop, subscribed):
        if prop not in HDMI_EVENTS:
            return
        if verb == "event":
            await self._hc_send(writer, "get", HDMI_EVENTS[prop])
            await self._hc_send(writer, "get", "hdcpStatus")
        else:
            subscribed.add(prop)
            if len(subscribed) == len(HDMI_EVENTS):
                self.set_state("hdmi_feedback", "Live HControl events")

    def _apply_hdmi(self, prop, value):
        if prop == "hdmiOutStatus":
            if value in (0, 1):
                self.set_state("hdmi_connected", bool(value))
        elif isinstance(value, str):
            self.set_state(HDMI_READS[prop], value)

    async def _confirm(self, wire, expected):
        fresh = await self._request(wire, set(expected))
        if any(fresh[key] != value for key, value in expected.items()):
            raise CommandRefusedError(
                "The receiver reported different settings. Check its routing mode, permissions and signal format."
            )

    async def send_command(self, command, params=None):
        if self._n24:
            return await self._n24_command(command, params or {})
        if self._n33 and command in OWNED_COMMANDS | {"play_live", "play_local"}:
            async with self._route_lock:
                return await self._n33_command(command, params or {})
        return await self._dispatch(command, params or {})

    async def _n24_command(self, command, params):
        if command in ("route_video", "set_video_stream", "play_live", "play_local"):
            async with self._route_lock:
                return await self._n24_video(command, params)
        handler = {
            "route_usb": self._n24_keyboard,
            "set_usb_host": self._n24_keyboard,
            "route_audio": self._n24_audio,
            "set_audio_stream": self._n24_audio,
            "follow_video": self._n24_audio,
        }.get(command, self._dispatch)
        return await handler(command, params)

    async def _n24_keyboard(self, command, params):
        addr = encoder_address(params.get("source", params.get("address")))
        if command == "route_usb" and addr not in self._usb_hosts:
            raise CommandRefusedError("Choose a configured keyboard and mouse encoder.")
        report = await self._request("getStatus", {"usb_enabled"})
        if not report["usb_enabled"]:
            raise CommandRefusedError(
                "Enable USB on the receiver’s KVM page before switching keyboard and mouse."
            )
        # Omitting the zero would switch video and any following audio, too.
        # USB enable is observable even though the connected host is not.
        return await self._confirm("KVMMasterIP:" + addr + ",0", {"usb_enabled": True})

    async def _n24_audio(self, command, params):
        current = await self._request("getStatus", {"aes67_enabled"})
        if current["aes67_enabled"]:
            raise CommandRefusedError(
                "The decoder is receiving AES67 audio. Select SVSI audio on the decoder before routing a numbered audio stream."
            )
        wire, expected = self._route(command, params)
        return await self._confirm(wire, {**expected, "aes67_enabled": False})

    async def _n24_video(self, command, params):
        if command != "route_video":
            return await self._dispatch(command, params)
        source = str(params.get("source", ""))
        if source not in self._source_ids:
            raise CommandRefusedError("Choose a configured video source.")
        reply = await self._request("set:" + source, {"video_stream", "play_mode"})
        if reply["video_stream"] != int(source):
            raise CommandRefusedError("The decoder reported a different video stream.")
        wire = "getStatus" if reply["play_mode"] == "live" else "live"
        return await self._confirm(wire, {"video_source": source})

    async def _n33_command(self, command, params):
        if command in OWNED_COMMANDS:
            await self._check_owner()
        if command == "route_video":
            return await self._recall_video(str(params.get("source", "")))
        if command == "select_audio_source":
            return await self._select_audio_source(params.get("source"))
        return await self._dispatch(
            command, params, confirm_owner=command in OWNED_COMMANDS
        )

    async def _select_audio_source(self, value):
        if value not in AUDIO_INPUTS:
            raise CommandRefusedError("Choose a listed audio input.")
        report = await self._request(
            "setSettings:audioStream:" + value, {"audio_source", "dante_av_h_enabled"}
        )
        self._require_native_owner(report)
        # AMX does not specify numeric AUDIOSTREAM codes. Preserve them as
        # readings; only a recognized return value can contradict this request.
        actual = report["audio_source"]
        if actual in AUDIO_INPUTS.values() and actual != AUDIO_INPUTS[value]:
            raise CommandRefusedError("The receiver reported a different audio input.")

    async def _check_owner(self):
        report = await self._request("getStatus", {"dante_av_h_enabled"})
        self._require_native_owner(report)
        return report

    def _require_native_owner(self, report):
        if (
            report["dante_av_h_enabled"]
            or self.config.get("routing_owner") == "Dante AV"
        ):
            raise CommandRefusedError(
                "Dante owns this receiver’s audio and video routes."
            )

    async def _recall_video(self, source):
        if source not in self._source_ids | self._urls:
            raise CommandRefusedError("Choose a configured video source.")
        for wire, key, value in video_recall_steps(source, self._urls):
            current = await self._check_owner()
            # A firmware packet can omit an empty URL. Write it whenever this
            # fresh response has not already confirmed the requested value.
            if current.get(key) != value:
                await self._confirm(wire, {key: value, "dante_av_h_enabled": False})
        await self._confirm("getStatus", {"video_source": source})

    async def _dispatch(self, command, params, *, confirm_owner=False):
        spec = self.DRIVER_INFO["commands"].get(command)
        if spec is None:
            raise CommandRefusedError(
                "This control is unavailable for this decoder model or routing owner."
            )
        if command == "refresh_network":
            return await self._request("getNetStatus", {"switch_port"}, header=NETWORK)
        if "query_for" in spec:
            return await self._request("getStatus", {spec["query_for"]})
        wire, expected = self._operation(command, params or {})
        if confirm_owner:
            expected["dante_av_h_enabled"] = False
        if expected:
            return await self._confirm(wire, expected)
        await self._request(wire, {"identity"})

    def _operation(self, command, params):
        key = command.removeprefix("set_")
        if command in ROUTES:
            return self._route(command, params)
        if key in TOGGLES:
            enabled = flag(params["enabled"])
            return TOGGLES[key][enabled], {key: enabled}
        if command in LEVELS:
            level = number(params["level"], 0, 100)
            return f"{LEVELS[command]}:{level}", dict.fromkeys(
                self.DRIVER_INFO["commands"][command]["sets"], level
            )
        if command == "send_saved_ir":
            name = params.get("name", "")
            if not re.fullmatch(r"[ -~]+", name):
                raise CommandRefusedError(
                    "Enter the saved IR name without line breaks."
                )
            return "sendir:" + name, {}
        return self._other_operation(command, params)

    def _route(self, command, params):
        video = "video" in command and command != "follow_video"
        stream = (
            0
            if command == "follow_video"
            else number(params.get("source", params.get("stream")), int(video), 32511)
        )
        if command.startswith("route_") and str(stream) not in self._source_ids | {"0"}:
            raise CommandRefusedError(
                "Choose a configured source or enter a stream number."
            )
        return ("set:" if video else "seta:") + str(stream), {
            "video_stream" if video else "audio_stream": stream
        }

    def _other_operation(self, command, params):
        if command == "set_usb_host":
            addr = encoder_address(params["address"])
            return "KVMUSB20MainIP:" + addr, dict.fromkeys(
                ("kvm_host", "usb_host"), addr
            )
        if command == "set_stream_url":
            url = stream_url(
                params["url"], schemes=N33_SCHEMES if self._n33 else N26_SCHEMES
            )
            return "setSettings:streamURL:" + url, {"stream_url": url}
        if command == "set_wall_layout":
            return wall_layout(params)
        if command == "play_local":
            slot = number(params["playlist"], 1, 8)
            expected = {"play_mode": "local"}
            if not self._n33:
                expected["playlist"] = slot
            return "local:" + str(slot), expected
        if command in ("identify", "play_live"):
            return (
                ("idOn", {"identified": True})
                if command == "identify"
                else ("live", {"play_mode": "live"})
            )
        return self._enum_operation(command, params)

    def _enum_operation(self, command, params):
        param, spec = next(
            iter(self.DRIVER_INFO["commands"][command]["params"].items())
        )
        value = params[param]
        if value not in spec["values"]:
            raise CommandRefusedError("Choose one of the listed values.")
        prefix = {
            "set_hdmi_audio_mode": "setSettings:hdmiAudio:",
            "set_scaler_mode": "modeset:",
            "set_receive_mode": "setSettings:ReceiveSrc:",
            "display_cec": "CECPower:",
        }[command]
        expected = (
            {} if command == "display_cec" else {command.removeprefix("set_"): value}
        )
        return prefix + value, expected

    async def set_device_setting(self, key, value):
        spec = self.DRIVER_INFO["device_settings"].get(key)
        if not spec:
            raise CommandRefusedError("Unknown decoder setting.")
        param = next(iter(self.DRIVER_INFO["commands"]["set_" + key]["params"]))
        await self.send_command("set_" + key, {param: value})

    async def prepare_bridge_port(self, port_id, params):
        if port_id != "serial:1":
            raise CommandRefusedError("Choose the decoder RS-232 connector.")
        async with self._serial_lock:
            try:
                desired = serial_format(params, N24_BAUD if self._n24 else BAUD)
                current = await self._request("getStatus", SERIAL)
                if any(current[k] != v for k, v in desired.items()):
                    await self._confirm(
                        "serSet:" + ",".join(str(desired[k]) for k in SERIAL), desired
                    )
            except (CommandRefusedError, ConnectionError, ValueError) as exc:
                self.set_state("serial_setup", f"Setup failed: {exc}")
                raise
            self.set_state(
                "serial_setup",
                "Line settings confirmed; check the downstream device for a reply.",
            )
