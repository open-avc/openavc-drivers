"""AMX N2625 wallplate decoders. Direct Control correlates multi-field
readbacks with named Matrix recalls and separately authenticated TLS writes.
An optional HControl session observes the HDMI sink and verifies properties
absent from ordinary status packets.
"""

from __future__ import annotations
import asyncio
from copy import deepcopy
from ipaddress import IPv4Address
import json
import re
from openavc.core.device_traffic import get_traffic_recorder
from openavc.drivers.base import BaseDriver, CommandRefusedError, ConnectionFaultError

STATUS = "SVSI_RXGEN2"

NETWORK = "SVSI_NETSTATS"

SERIAL = ("serial_baud", "serial_bits", "serial_parity", "serial_stops")

BAUD = {1200, 1800, 2400, 4800, 9600, 19200, 38400, 57600, 115200}

ON_OFF = {"off": False, "on": True}

HDMI_ROOT = "/decoder/systemStatus/"

HC_EVENTS = {
    HDMI_ROOT + "hdmiOutCableStatusChanged",
    HDMI_ROOT + "hdmiOutResolutionChanged",
}

LEVELS = {
    "set_left_volume": "lovolleft",
    "set_right_volume": "lovolright",
    "set_stereo_volume": "lovol",
}
MODEL = re.compile(r"(?:NMX-DEC-)?N2625D?-?WP(?:-(?:NA|EK))?(?=\d|[\s:]|-\d|$)", re.I)
FIELDS = {
    STATUS: ("identity", str),
    "HDMIAUDIO": ("hdmi_audio_mode", str.lower),
    "LIVEAUDIOLP": ("local_uses_stream_audio", ON_OFF),
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
    "HDMIOFF": ("hdmi_enabled", {"on": False, "off": True}),
    "HDMISTATUS": ("hdmi_connected", {"connected": True, "disconnected": False}),
    "INPUTRES": ("input_resolution", str),
    "PLAYMODE": ("play_mode", {"live": "live", "local": "local"}),
    "PLAYLIST": ("playlist", int),
    "LINEOUTVOL_L": ("left_volume", int),
    "LINEOUTVOL_R": ("right_volume", int),
    "SCALERBYPASS": ("scaler_enabled", {"no": True, "yes": False}),
    "MODE": ("scaler_mode", lambda s: s.lower().removesuffix(".mode")),
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
TOGGLES = {
    "disable_hdmi_on_loss": (
        "setSettings:disableHDMILostStream:off",
        "setSettings:disableHDMILostStream:on",
    ),
    "audio_muted": ("unmute", "mute"),
    "hdmi_enabled": ("hdmiOff", "hdmiOn"),
    "scaler_enabled": ("scalerdisable", "scalerenable"),
    "wall_enabled": ("setSettings:wallEnable:off", "setSettings:wallEnable:on"),
    "usb_enabled": ("setSettings:usbEnable:off", "setSettings:usbEnable:on"),
    "kvm_enabled": ("setSettings:kvmEnable:off", "setSettings:kvmEnable:on"),
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


def serial_format(params):
    baud = number(params.get("baudrate", 9600), 1200, 115200)
    bits = number(params.get("bytesize", 8), 7, 8)
    parity = {"N": "none", "O": "odd", "E": "even"}.get(
        str(params.get("parity", "N")).upper()
    )
    stops = {"1": 1, "1.0": 1, "2": 2, "2.0": 2}.get(str(params.get("stopbits", 1)))
    if not all((baud in BAUD, parity, stops)):
        raise CommandRefusedError(
            "Choose a supported baud rate, N/O/E parity and one or two stop bits."
        )
    if any(params.get(k) for k in ("rtscts", "xonxoff", "dsrdtr")):
        raise CommandRefusedError("Use no flow control on the decoder RS-232 port.")
    return dict(zip(SERIAL, (baud, bits, parity, stops)))


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


def split_field(line):
    for header in (STATUS, NETWORK):
        if line.upper().startswith(header):
            return header, line[len(header) :].lstrip(": \t")
    wire, _, value = line.partition(":")
    return wire.upper(), value.strip()


def read_field(spec, value):
    key, convert = spec
    try:
        parsed = convert[value.lower()] if isinstance(convert, dict) else convert(value)
    except (ValueError, KeyError):
        return {}
    return {key: parsed}


def numbered_source(row, name):
    stream = str(number(row.get("stream"), 1, 32511))
    return stream, f"{name} ({stream})"


AV_COMMANDS = {
    "route_video",
    "route_audio",
    "set_video_stream",
    "set_audio_stream",
    "follow_video",
}
AV_FEEDBACK = {"video_source", "audio_selection", "effective_audio_stream"}
ROUTE_FIELDS = {
    "video_stream",
    "play_mode",
    "audio_stream",
    "local_uses_stream_audio",
    "kvm_host",
    "usb_host",
}
HC_PROPERTIES = {
    "output_resolution": (HDMI_ROOT + "hdmiOutResolution",),
    "hdmi_connected": (HDMI_ROOT + "hdmiOutStatus",),
    "hdcp_status": (HDMI_ROOT + "hdcpStatus",),
    "video_muted": ("/decoder/hdmiOut/videoMute",),
    # AMX prints the first path in its table and the second in its example.
    "image_enabled": (
        "/decoder/streamSetup/previewImage",
        "/decoder/streamSetup/mwcStream/previewImage",
    ),
}
EXTRA_FLAGS = {
    "video_muted": ("vunmute", "vmute"),
    "image_enabled": ("setSettings:Preview:off", "setSettings:Preview:on"),
}


def stored_command(command, params):
    name = params.get("name", "")
    if not isinstance(name, str) or not re.fullmatch(r"[ -~]+", name):
        raise CommandRefusedError("Enter a saved command name without line breaks.")
    prefix = "sendir:" if command == "send_saved_ir" else "sendSer:"
    return prefix + name, {}


def hc_value(key, raw):
    if key == "hdmi_connected":
        return {0: False, 1: True}.get(raw) if isinstance(raw, int) else None
    if key in EXTRA_FLAGS:
        return raw if isinstance(raw, bool) else None
    return raw if isinstance(raw, str) else None


def encoder_address(value):
    try:
        addr = IPv4Address(str(value))
    except ValueError:
        raise CommandRefusedError("Enter the encoder’s IPv4 address.") from None
    if any((addr.is_multicast, addr.is_unspecified, int(addr) == 0xFFFFFFFF)):
        raise CommandRefusedError("Enter the encoder’s unicast IPv4 address.")
    return str(addr)


def host_source(row, name):
    addr = encoder_address(row.get("address"))
    return addr, f"{name} ({addr})"


def source_choices(rows, parse):
    choices = {}
    for row in rows or ():
        name = str(row.get("name", "")).strip()
        value, label = parse(row, name)
        if not name or value in choices:
            raise ValueError(
                "Give every source a name and a unique stream number or encoder address."
            )
        choices[value] = label
    return [{"value": k, "label": v} for k, v in choices.items()]


def video_route(values):
    mode = values.get("play_mode")
    if mode == "local":
        return {"video_source": None}
    if mode == "live" and "video_stream" in values:
        return {"video_source": str(values["video_stream"])}
    return {}


def audio_route(values):
    if "audio_stream" not in values:
        return {}
    audio = values["audio_stream"]
    result = {"audio_selection": str(audio) if audio else "follow"}
    mode = values.get("play_mode")
    uses_stream = True if mode == "live" else values.get("local_uses_stream_audio")
    if mode is not None:
        result.update(effective_audio(values, uses_stream))
    return result


def effective_audio(values, uses_stream):
    if uses_stream is False:
        return {"effective_audio_stream": None}
    if uses_stream:
        stream = values["audio_stream"] or values.get("video_stream")
        if stream is not None:
            return {"effective_audio_stream": stream}
    return {}


def usb_route(values):
    if not {"kvm_host", "usb_host"}.issubset(values):
        return {}
    addr = values["usb_host"]
    if addr != values["kvm_host"]:
        return {"usb_selection": None}
    try:
        return {"usb_selection": encoder_address(addr)}
    except CommandRefusedError:
        return {"usb_selection": None}


class AMXN2625WPDriver(BaseDriver):
    DRIVER_INFO = {
        "id": "amx_svsi_n2625_wp",
        "name": "AMX SVSI N2625 Wallplate Decoders",
        "manufacturer": "AMX",
        "category": "switcher",
        "author": "OpenAVC",
        "version": "1.0.0",
        "transport": "tcp",
        "delimiter": "\r",
        "ports": [50002, 50102],
        "protocols": ["amx-svsi-direct-control", "hcontrol"],
        "simulated": True,
        "verified": False,
        "min_platform_version": "0.37.2",
        "source_url": "https://www.amx.com/en-US/products/nmx-dec-n2625d-wp",
        "description": "Route MWC video, audio and USB/KVM to a wallplate. Read the selected streams, HDMI "
        "connection, incoming signal and display timing.",
        "tags": ["av-over-ip", "wallplate", "decoder", "svsi", "dante-av-a"],
        "compatible_models": [
            {
                "manufacturer": "AMX",
                "models": [
                    "NMX-DEC-N2625D-WP-NA",
                    "NMX-DEC-N2625D-WP-EK",
                    "NMX-DEC-N2625-WP-NA",
                    "NMX-DEC-N2625-WP-EK",
                ],
                "confidence": "untested",
                "notes": "D and non-D wallplates share hardware and firmware. Simulation checked on "
                "all four models. Physical qualification pending.",
            }
        ],
        "help": {
            "overview": "One wallplate is one destination. Route video, break audio away or follow video, and "
            "select the USB/KVM encoder independently. Incoming resolution, HDMI cable detection, "
            "and actual HDMI output timing help trace a missing picture.",
            "setup": "1. Connect to the wallplate address on TCP 50002, or use TLS on 50102 with its command "
            "password and a trusted certificate. Each Direct Control port accepts one controller at a "
            "time.\n"
            "2. Match the routing owner to the commissioned mode. Name the video/audio streams under "
            "SVSI sources, and the encoder addresses under USB/KVM encoder hosts. The D model "
            "identifies a license, not the currently active routing mode.\n"
            "3. A Video Matrix recall returns from local playback to live. Audio shows the selected "
            "independent stream or Follow video. Effective network audio shows which stream supplies "
            "audio when stream audio is in use. USB/KVM shows a selected host when both returned host "
            "addresses agree. Enable USB or KVM to carry its traffic.\n"
            "4. With ordinary control, allow outbound TCP 4197 for live display timing and HDCP. "
            "Readings also refresh after writes and on the status timer. In secure mode, video mute and "
            "preview writes are acknowledged without independent readback.\n"
            "5. To control a serial display on a site with ordinary TCP 50004 enabled, choose Through a "
            "bridge on the display driver and select this decoder’s RS-232 connector. Match its baud, "
            "data bits, parity and stop bits. Secure serial passthrough on 50104 requires support "
            "beyond this driver. Use the site’s commissioned security settings.",
            "connection": "Confirm the address, port, TLS and password. Check the selected stream, playback "
            "mode, incoming timing, HDMI detection, mutes and display timing separately.",
        },
        "default_config": {
            "host": "",
            "port": 50002,
            "ssl": False,
            "verify_ssl": True,
            "password": "",
            "routing_owner": "SVSI",
            "sources": [],
            "hdmi_events": True,
            "poll_interval": 10,
            "simulation_variant": "NMX-DEC-N2625D-WP-NA",
            "web_scheme": "https",
            "usb_sources": [],
        },
        "config_schema": {
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
                "help": "Required for secure control. Enter the command password configured on "
                "the receiver.",
            },
            "routing_owner": {
                "type": "enum",
                "label": "Routing configured on receiver",
                "values": ["SVSI", "Dante AV-A"],
                "default": "SVSI",
                "help": "Match the routing mode enabled on the wallplate. In Dante AV-A "
                "mode, route audio and video in Dante Controller. USB/KVM host "
                "selection is independent.",
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
                "label": "Listen for HDMI output changes",
                "default": True,
                "help": "Use ordinary HControl on TCP 4197 for cable and timing events, "
                "HDCP, video mute and preview feedback. Secure connections use "
                "Direct Control.",
            },
            "poll_interval": {
                "type": "integer",
                "label": "Status refresh (seconds)",
                "default": 10,
                "min": 0,
                "advanced": True,
            },
            "simulation_variant": {
                "type": "enum",
                "label": "Wallplate model for simulation",
                "values": [
                    "NMX-DEC-N2625D-WP-NA",
                    "NMX-DEC-N2625D-WP-EK",
                    "NMX-DEC-N2625-WP-NA",
                    "NMX-DEC-N2625-WP-EK",
                ],
                "default": "NMX-DEC-N2625D-WP-NA",
                "advanced": True,
                "help": "Choose a training model. Live connections identify the "
                "N2625 wallplate from its own response.",
            },
            "web_scheme": {
                "type": "enum",
                "label": "Decoder web protocol",
                "values": ["https", "http"],
                "default": "https",
                "advanced": True,
                "help": "Match the wallplate web service for its web interface and preview "
                "image.",
            },
            "usb_sources": {
                "type": "table",
                "label": "USB/KVM encoder hosts",
                "row_label": "Host",
                "default": [],
                "columns": {
                    "name": {"type": "string", "label": "Host name", "required": True},
                    "address": {
                        "type": "string",
                        "label": "Encoder IPv4 address",
                        "required": True,
                    },
                },
            },
        },
        "discovery": {
            "port_open": [50002, 50102],
            "manufacturer_alias": ["amx", "svsi"],
            "tcp_probe": {
                "port": 50002,
                "send_ascii": "getStatus\r",
                "expect_regex": "(?i)SVSI_RXGEN2\\s*:?\\s*(?:NMX-DEC-)?N2625D?-?WP(?:-(?:NA|EK))?(?=\\d|[\\s:]|-\\d|$)",
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
                {
                    "label": "USB / KVM",
                    "route_property": "usb_selection",
                    "command": "route_usb",
                },
            ],
        },
        "state_variables": {
            "local_uses_stream_audio": {
                "type": "boolean",
                "label": "Local playback uses stream audio",
            },
            "hdmi_audio_mode": {
                "type": "string",
                "label": "HDMI audio output",
                "control": True,
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
            "kvm_host": {"type": "string", "label": "KVM host"},
            "usb_host": {"type": "string", "label": "USB 2.0 host"},
            "serial_parity": {
                "type": "string",
                "label": "Serial parity",
                "cloud_priority": "low",
            },
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
            "usb_selection": {
                "type": "string",
                "label": "Selected USB/KVM encoder",
                "cloud_priority": "high",
            },
            "effective_audio_stream": {
                "type": "integer",
                "label": "Effective network audio stream",
                "cloud_priority": "high",
            },
            "image_enabled": {"type": "boolean", "label": "Web preview enabled"},
            "snapshot_url": {"type": "string", "label": "Wallplate snapshot URL"},
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
                "sets": {"video_source": "{source}", "play_mode": "live"},
                "help": "Choose a configured stream and return to live playback. The route is "
                "confirmed from the reported play mode and stream.",
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
                "help": "Select the SVSI audio stream or Follow video. Local playback uses the "
                "selected stream only when the decoder reports that stream audio is "
                "enabled for local playback.",
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
                "help": "Sets the audio stream to 0. Subsequent video route changes carry the "
                "audio selection with them.",
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
                "help": "Sets both analog outputs to the same level. Read the left and "
                "right channels separately.",
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
                "help": "Sets the output timing used while scaling is enabled. The receiver "
                "supports downscaling 4K60 to 1080p.",
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
                "help": "Sets the wall dimensions and the position of this receiver. Video "
                "wall enable is separate.",
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
                "help": "Sends HDMI CEC power to the attached display. The receiver does not "
                "confirm display power.",
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
                "help": "Reads the connected switch and port advertised to the receiver. "
                "Switch-provided labels may be empty.",
            },
            "refresh_status": {
                "label": "Read decoder status",
                "query_for": "identity",
                "help": "Read the decoder identity, routing, playback, HDMI, audio, USB, "
                "video wall and serial settings.",
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
                "help": "Auto sends audio when the display supports it. Off suppresses "
                "HDMI audio.",
                "sets": {"hdmi_audio_mode": "{mode}"},
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
            "route_usb": {
                "label": "Route USB/KVM by host",
                "params": {
                    "source": {
                        "type": "enum",
                        "label": "Encoder host",
                        "required": True,
                        "values": [],
                    }
                },
                "sets": {"usb_selection": "{source}"},
                "help": "Assigns both KVM and USB 2.0 to the selected encoder. Traffic enable is "
                "controlled separately.",
            },
            "set_image_enabled": {
                "label": "Enable web preview",
                "params": {
                    "enabled": {"type": "boolean", "required": True, "label": "Enabled"}
                },
                "sets": {"image_enabled": "{enabled}"},
            },
            "read_effective_audio_stream": {
                "label": "Read effective network audio stream",
                "query_for": "effective_audio_stream",
            },
            "read_usb_selection": {
                "label": "Read selected usb/kvm encoder",
                "query_for": "usb_selection",
            },
            "read_image_enabled": {
                "label": "Read web preview enabled",
                "query_for": "image_enabled",
            },
            "read_input_resolution": {
                "label": "Read incoming video resolution",
                "query_for": "input_resolution",
            },
            "read_output_resolution": {
                "label": "Read hdmi output resolution (hcontrol)",
                "query_for": "output_resolution",
            },
            "read_hdcp_status": {
                "label": "Read hdcp output status (hcontrol)",
                "query_for": "hdcp_status",
            },
            "send_saved_serial": {
                "label": "Send stored serial command",
                "params": {
                    "name": {
                        "type": "string",
                        "label": "Command name on receiver",
                        "required": True,
                    }
                },
                "help": "Recall a serial command stored on the wallplate. Acknowledgement "
                "confirms that the decoder answered.",
            },
        },
        "device_settings": {
            "hdmi_audio_mode": {
                "type": "enum",
                "label": "HDMI audio output",
                "values": ["auto", "off"],
                "state_key": "hdmi_audio_mode",
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
            "route_audio",
            "route_usb",
            "set_audio_muted",
            "refresh_status",
        ],
        "web_ui": "{web_scheme}://{host}",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._secure = False
        self._identifying = True
        self._lock = asyncio.Lock()
        self._serial_lock = asyncio.Lock()
        self._route_lock = asyncio.Lock()
        self._hc_lock = asyncio.Lock()
        self._header = self._reply = None
        self._fresh = {}
        self._needed = set()
        self._in_packet = False
        self._packet = {}
        self._missing_routes = set()
        self._watch = self._writer = None
        self._hc_reply = self._hc_path = None
        self._learned = {}
        self._display_changed = asyncio.Event()
        self.DRIVER_INFO = deepcopy(type(self).DRIVER_INFO)
        self._dante = self.config.get("routing_owner") == "Dante AV-A"
        self._configure_routes()
        if not self._uses_hcontrol():
            for key in ("video_muted", "image_enabled"):
                self.DRIVER_INFO["commands"]["set_" + key].pop("sets")
            for key in (
                "video_muted",
                "image_enabled",
                "hdcp_status",
                "output_resolution",
            ):
                self.DRIVER_INFO["commands"].pop("read_" + key, None)

    def _uses_hcontrol(self):
        return not self.config.get("ssl") and self.config.get("hdmi_events", True)

    def _configure_routes(self):
        commands = self.DRIVER_INFO["commands"]
        hosts = source_choices(self.config.get("usb_sources"), host_source)
        commands["route_usb"]["params"]["source"]["values"] = hosts
        self._hosts = {h["value"] for h in hosts}
        if self._dante:
            for name in AV_COMMANDS:
                commands.pop(name)
            for key in AV_FEEDBACK:
                commands.pop("read_" + key, None)
            self.DRIVER_INFO["routing"]["planes"] = [
                p
                for p in self.DRIVER_INFO["routing"]["planes"]
                if p["command"] == "route_usb"
            ]
            self.DRIVER_INFO["quick_actions"] = [
                c for c in self.DRIVER_INFO["quick_actions"] if c in commands
            ]
            return
        sources = source_choices(self.config.get("sources"), numbered_source)
        self._streams = {s["value"] for s in sources}
        commands["route_video"]["params"]["source"]["values"] = sources
        commands["route_audio"]["params"]["source"]["values"] = [
            {"value": "follow", "label": "Follow video"},
            *sources,
        ]

    async def _post_connect(self):
        report = await self._request("getStatus", {"identity", "firmware"})
        if not MODEL.match(report["identity"].strip()):
            raise ConnectionFaultError(
                "Device did not identify as an N2625 wallplate decoder.",
                code="invalid_config",
            )
        self._identifying = False
        await self.poll()

    async def _initial_sync(self):
        await self._request("getNetStatus", {"mac"}, header=NETWORK)
        self.set_state("hdmi_feedback", "Direct Control")
        if self._uses_hcontrol():
            self._watch = asyncio.create_task(self._watch_hdmi())

    async def _close_session(self):
        if self._waiting_for_reply():
            self._reply.set_exception(ConnectionError("Wallplate disconnected."))
        if self._watch is not None:
            self._watch.cancel()
            await asyncio.gather(self._watch, return_exceptions=True)
            self._watch = None
        self._clear_hdmi("Disconnected")
        self._missing_routes.clear()
        self._packet.clear()
        for key in (
            AV_FEEDBACK
            | ROUTE_FIELDS
            | {"usb_selection", "input_resolution", "hdmi_connected"}
        ):
            self.set_state(key, None)

    async def poll(self):
        await self._request("getStatus", {"identity", "firmware"})

    def _begin_packet(self, wire):
        # The new header closes the preceding packet. A missing field must
        # not keep a prior Matrix crosspoint lit.
        self._clear_missing_routes()
        self._packet = {}
        self._in_packet = wire == self._header
        self._fresh = {}
        if wire == STATUS and not self._identifying:
            self._missing_routes = AV_FEEDBACK | ROUTE_FIELDS | {"usb_selection"}

    def _clear_missing_routes(self):
        for key in self._missing_routes:
            self.set_state(key, None)
        self._missing_routes.clear()

    def _receive(self, wire, value):
        if wire in (STATUS, NETWORK):
            self._begin_packet(wire)
        if self._identifying and wire not in (STATUS, "SWVER"):
            return
        if wire not in FIELDS:
            return
        update = read_field(FIELDS[wire], value)
        if update.keys() & ROUTE_FIELDS:
            update.update(self._route_feedback(update))
        self._missing_routes.difference_update(update)
        for key, reading in update.items():
            self.set_state(key, reading)
        self._track_reply(update)

    def _route_feedback(self, update):
        self._packet.update(update)
        result = usb_route(self._packet)
        if self._dante:
            result.update(dict.fromkeys(AV_FEEDBACK))
        else:
            result.update(video_route(self._packet))
            result.update(audio_route(self._packet))
        return result

    def _clear_hdmi(self, detail):
        self.set_state("hdmi_feedback", detail)
        for key in ("output_resolution", "hdcp_status", "video_muted", "image_enabled"):
            self.set_state(key, None)
        self.set_state("snapshot_url", "")
        self._learned.clear()

    async def _watch_hdmi(self):
        while True:
            try:
                await self._hdmi_session()
            except (OSError, ValueError, TimeoutError):
                self._clear_hdmi("Direct Control; HControl unavailable")
            await asyncio.sleep(15)

    async def _hdmi_session(self):
        host, port = self.udp_redirect or (self.config["host"], 4197)
        ip = self.local_address(host)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, local_addr=(ip, 0) if ip else None), 3
        )
        self._writer = writer
        monitor = asyncio.create_task(self._hdmi_messages(reader))
        try:
            for path in sorted(HC_EVENTS):
                await self._hc_send(writer, "subscribeevent", path)
            while not monitor.done():
                self._display_changed.clear()
                await self._refresh_hdmi()
                await self._wait_for_display(monitor)
            await monitor
        finally:
            self._writer = None
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            writer.close()
            await writer.wait_closed()

    async def _wait_for_display(self, monitor):
        changed = asyncio.create_task(self._display_changed.wait())
        try:
            await asyncio.wait(
                {changed, monitor}, timeout=30, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            changed.cancel()
            await asyncio.gather(changed, return_exceptions=True)

    async def _refresh_hdmi(self):
        for key in HC_PROPERTIES:
            await self._refresh_property(key)

    async def _refresh_property(self, key):
        try:
            await self._read_property(key)
        except CommandRefusedError:
            if key == "hdmi_connected":
                raise ConnectionError(
                    "The HDMI feedback connection stopped answering."
                ) from None
            self.set_state(key, None)
            if key == "image_enabled":
                self.set_state("snapshot_url", "")

    async def _hc_send(self, writer, verb, path):
        message = verb + " " + json.dumps({"path": path}) + "\n"
        writer.write(message.encode())
        await writer.drain()
        self.record_traffic("tx", message, meta={"protocol": "HControl"})

    async def _hdmi_messages(self, reader):
        subscribed = set()
        while line := await reader.readline():
            self.record_traffic("rx", line, meta={"protocol": "HControl"})
            verb, _, body = line.decode("utf8", "replace").strip().partition(" ")
            if verb in ("@get", "@subscribeevent", "event"):
                self._hc_message(verb, json.loads(body), subscribed)
        raise ConnectionError("HControl connection closed.")

    def _hc_message(self, verb, packet, subscribed):
        if not isinstance(packet, dict):
            return
        path = packet.get("path")
        if verb == "@get":
            self._hc_reading(path, packet.get("value"))
        elif isinstance(path, str) and path in HC_EVENTS:
            self._hc_event(verb, path, subscribed)

    def _hc_event(self, verb, path, subscribed):
        if verb == "event":
            self._display_changed.set()
            return
        subscribed.add(path)
        if subscribed == HC_EVENTS:
            self.set_state("hdmi_feedback", "Live HDMI output events")

    def _hc_reading(self, path, value):
        if path != self._hc_path or self._hc_reply is None:
            return
        if not self._hc_reply.done():
            self._hc_reply.set_result(value)

    async def _read_property(self, key):
        async with self._hc_lock:
            writer = self._writer
            if writer is None:
                raise CommandRefusedError(
                    "The decoder’s HControl feedback is unavailable."
                )
            for path in self._property_paths(key):
                raw = await self._read_property_path(writer, path)
                value = hc_value(key, raw)
                if value is not None:
                    self._learned[key] = path
                    self.set_state(key, value)
                    if key == "image_enabled":
                        self.set_state("snapshot_url", self._snapshot_url())
                    return value
            raise CommandRefusedError(
                "The decoder did not report this HControl property."
            )

    def _property_paths(self, key):
        first = self._learned.get(key)
        return (first,) if first else HC_PROPERTIES[key]

    async def _read_property_path(self, writer, path):
        self._hc_path = path
        future = self._hc_reply = asyncio.get_running_loop().create_future()
        try:
            await self._hc_send(writer, "get", path)
            return await asyncio.wait_for(future, 1.5)
        except TimeoutError:
            # A late response for an established path must not verify a
            # later write on this session. Probe unknown alternate paths
            # individually when the manual gives two spellings.
            if path in self._learned.values():
                writer.close()
                raise
            return None
        finally:
            self._hc_reply = self._hc_path = None
            future.cancel()

    async def send_command(self, command, params=None):
        spec = self.DRIVER_INFO["commands"].get(command)
        if not spec:
            raise CommandRefusedError(
                "This command is unavailable in the configured routing or feedback mode."
            )
        if "query_for" in spec:
            return await self._query(spec["query_for"])
        async with self._route_lock:
            return await self._control(command, params or {}, spec)

    async def _query(self, key):
        if key == "switch_port":
            return await self._request("getNetStatus", {key}, header=NETWORK)
        if key in HC_PROPERTIES and key != "hdmi_connected":
            return await self._read_property(key)
        return await self._request("getStatus", {key})

    async def _control(self, command, params, spec):
        if command == "route_video":
            return await self._recall_video(params.get("source"))
        key = command.removeprefix("set_")
        if key in EXTRA_FLAGS:
            return await self._set_flag(key, flag(params["enabled"]), spec)
        wire, expected = self._operation(command, key, params)
        if expected:
            return await self._confirm(wire, expected)
        await self._request(wire, {"identity"})

    async def _recall_video(self, source):
        if source not in self._streams:
            raise CommandRefusedError("Choose a configured video source.")
        stream = int(source)
        first = await self._request("set:" + source, {"video_stream", "play_mode"})
        if first["video_stream"] != stream:
            raise CommandRefusedError("The decoder reported a different video stream.")
        wire = "getStatus" if first["play_mode"] == "live" else "live"
        await self._confirm(wire, {"video_source": source, "play_mode": "live"})

    async def _set_flag(self, key, value, spec):
        verify = "sets" in spec
        if verify:
            # Confirm readback is available before changing the device.
            await self._read_property(key)
        await self._request(EXTRA_FLAGS[key][value], {"identity"})
        if verify and await self._read_property(key) != value:
            raise CommandRefusedError(
                "The decoder reported a different mute or preview setting."
            )

    def _operation(self, command, key, params):
        if command in AV_COMMANDS:
            return self._stream_operation(command, params)
        if command in ("route_usb", "set_usb_host"):
            address = encoder_address(params.get("source", params.get("address")))
            if command == "route_usb" and address not in self._hosts:
                raise CommandRefusedError("Choose a configured USB/KVM encoder.")
            return "KVMUSB20MainIP:" + address, {
                "kvm_host": address,
                "usb_host": address,
            }
        if key in TOGGLES:
            enable = flag(params["enabled"])
            return TOGGLES[key][enable], {key: enable}
        if command in LEVELS:
            level = number(params["level"], 0, 100)
            keys = self.DRIVER_INFO["commands"][command]["sets"]
            return f"{LEVELS[command]}:{level}", dict.fromkeys(keys, level)
        return self._other_operation(command, params)

    def _stream_operation(self, command, params):
        if command == "follow_video":
            return "seta:0", {"audio_stream": 0}
        if command == "set_video_stream":
            stream = number(params.get("stream"), 1, 32511)
            return f"set:{stream}", {"video_stream": stream}
        value = params.get("source", params.get("stream"))
        if command == "route_audio" and value == "follow":
            return "seta:0", {"audio_stream": 0}
        stream = number(value, 0, 32511)
        if command == "route_audio" and str(stream) not in self._streams:
            raise CommandRefusedError(
                "Choose a configured audio source or Follow video."
            )
        return f"seta:{stream}", {"audio_stream": stream}

    def _other_operation(self, command, params):
        if command == "set_wall_layout":
            return wall_layout(params)
        if command == "play_local":
            slot = number(params.get("playlist"), 1, 8)
            return f"local:{slot}", {"play_mode": "local", "playlist": slot}
        if command == "identify":
            return "idOn", {"identified": True}
        if command == "play_live":
            return "live", {"play_mode": "live"}
        if command in ("send_saved_ir", "send_saved_serial"):
            return stored_command(command, params)
        return self._enum_operation(command, params)

    def _enum_operation(self, command, params):
        param, spec = next(
            iter(self.DRIVER_INFO["commands"][command]["params"].items())
        )
        value = params.get(param)
        if value not in spec["values"]:
            raise CommandRefusedError("Choose one of the listed values.")
        prefix = {
            "set_hdmi_audio_mode": "setSettings:hdmiAudio:",
            "set_scaler_mode": "modeset:",
            "display_cec": "CECPower:",
        }[command]
        return prefix + value, (
            {} if command == "display_cec" else {command.removeprefix("set_"): value}
        )

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

    def _waiting_for_reply(self):
        return self._reply is not None and not self._reply.done()

    async def _liveness_probe(self):
        await self._request("getStatus", {"identity"})

    async def _confirm(self, wire, expected):
        fresh = await self._request(wire, set(expected))
        if any(fresh[key] != value for key, value in expected.items()):
            raise CommandRefusedError(
                "The receiver reported different settings. Check its routing mode, permissions and signal format."
            )

    async def on_data_received(self, data):
        for line in data.decode("utf-8", "replace").splitlines():
            wire, value = split_field(line.strip())
            self._receive(wire, value)

    def _track_reply(self, update):
        if self._in_packet and self._waiting_for_reply():
            self._fresh.update(update)
            if self._needed.issubset(self._fresh):
                self._reply.set_result(dict(self._fresh))

    async def prepare_bridge_port(self, port_id, params):
        if port_id != "serial:1":
            raise CommandRefusedError("Choose the decoder RS-232 connector.")
        async with self._serial_lock:
            try:
                desired = serial_format(params)
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

    async def set_device_setting(self, key, value):
        spec = self.DRIVER_INFO["device_settings"].get(key)
        if not spec:
            raise CommandRefusedError("Unknown decoder setting.")
        param = next(iter(self.DRIVER_INFO["commands"]["set_" + key]["params"]))
        await self.send_command("set_" + key, {param: value})

    def _snapshot_url(self):
        if not self.get_state("image_enabled") or self.udp_redirect is not None:
            return ""
        host = self.config["host"]
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        scheme = self.config.get("web_scheme", "https")
        return f"{scheme}://{host}/snapshot.jpg"
