"""AMX NMX-DEC-N2622S. Direct Control v1.3 reports a CR-separated status
packet even for writes. Python correlates fresh fields across those packets,
prefixes secure commands with the receiver's command password, and subscribes
to HDMI events on the separate HControl connection at ordinary sites.
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


def stream_url(value):
    value = str(value)
    if not re.fullmatch(r"[!-~]+", value):
        raise CommandRefusedError(
            "Use an ASCII stream URL without spaces or line breaks."
        )
    url = urlsplit(value)
    if url.scheme not in ("rtsp", "rtp", "udp", "http", "https") or not url.hostname:
        raise CommandRefusedError("Enter a complete H.26x stream URL.")
    if any((url.username is not None, url.query, url.fragment)):
        raise CommandRefusedError(
            "Use the decoder web page for URLs with credentials, query strings or fragments."
        )
    return value


def report_url(value):
    # Site-configured URLs can contain secrets. Publish only credential-free addresses.
    try:
        return stream_url(value) if value else ""
    except (CommandRefusedError, ValueError):
        return "Private stream URL"


def encoder_address(value):
    addr = IPv4Address(value)
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


def read_field(wire, value):
    key, convert = FIELDS[wire]
    try:
        parsed = convert[value.lower()] if isinstance(convert, dict) else convert(value)
    except (ValueError, KeyError):
        return {}
    update = {key: parsed}
    if key in ("video_stream", "audio_stream"):
        update["video_source" if key == "video_stream" else "audio_selection"] = str(
            parsed
        )
    return update


FIELDS["STREAMURL"] = ("stream_url", report_url)


class AMXN2622SDriver(BaseDriver):
    DRIVER_INFO = {
        "id": "amx_svsi_n2622s",
        "name": "AMX SVSI NMX-DEC-N2622S",
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
        "source_url": "https://www.amx.com/da/site_elements/amx-direct-control-api-n26xxs",
        "description": "Routes SVSI video and audio to one N2622S receiver. Controls local playback, scaling, "
        "USB/KVM and line levels, and reports HDMI, serial and network status.",
        "tags": ["av-over-ip", "decoder", "svsi", "4k", "serial-bridge", "cec"],
        "compatible_models": [
            {
                "manufacturer": "AMX",
                "models": ["NMX-DEC-N2622S"],
                "confidence": "untested",
                "notes": "Standalone decoder. Derived from AMX Direct Control API v1.3 and the "
                "N2600S HControl guide. Hardware verification pending.",
            }
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
            "3. On an ordinary connection, allow outbound TCP 4197 for live HDMI events and "
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
                "label": "Listen for live HDMI changes",
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
                "expect_regex": "(?i)SVSI_RXGEN2\\s*:\\s*(?:NMX-DEC-)?N2622S(?:\\b|-)",
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
        self.DRIVER_INFO = deepcopy(type(self).DRIVER_INFO)
        self._secure = False
        self._lock = asyncio.Lock()
        self._serial_lock = asyncio.Lock()
        self._reply = None
        self._header = None
        self._in_packet = False
        self._needed = set()
        self._fresh = {}
        self._watch = None
        self._source_ids = set()
        self._configure_routing()

    def _configure_routing(self):
        if self.config.get("routing_owner", "SVSI") == "Dante AV":
            self._use_dante_routing()
            return
        choices = self._source_choices()
        for plane in ("audio", "video"):
            options = (
                [{"value": "0", "label": "Follow video"}] if plane == "audio" else []
            ) + choices
            self.DRIVER_INFO["commands"]["route_" + plane]["params"]["source"][
                "values"
            ] = options

    def _use_dante_routing(self):
        self.DRIVER_INFO.pop("routing")
        for cmd in ROUTES | {"set_stream_url", "set_receive_mode"}:
            self.DRIVER_INFO["commands"].pop(cmd)
        self.DRIVER_INFO["device_settings"].pop("receive_mode")
        self.DRIVER_INFO["quick_actions"] = [
            c for c in self.DRIVER_INFO["quick_actions"] if c not in ROUTES
        ]

    def _source_choices(self):
        choices = []
        for row in self.config.get("sources", []) or []:
            stream = str(number(row.get("stream"), 1, 32511))
            label = str(row.get("name", "")).strip()
            if not label or stream in self._source_ids:
                raise ValueError("Each source needs a name and a unique stream number.")
            self._source_ids.add(stream)
            choices.append({"value": stream, "label": f"{label} ({stream})"})
        return choices

    async def _pre_connect(self):
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
        model = re.search(
            r"\bN[1-9][0-9]{3}[A-Z]*(?=[-\s]|$)", report["identity"].upper()
        )
        if model and model.group() != "N2622S":
            raise ConnectionFaultError(
                "The connected decoder identifies as "
                + model.group()
                + "; this driver is for N2622S.",
                code="invalid_config",
            )

    async def _initial_sync(self):
        await self._request("getNetStatus", {"mac"}, header=NETWORK)
        if self.config.get("hdmi_events", True) and not self._secure:
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

    async def on_data_received(self, data):
        for line in data.decode("utf-8", "replace").splitlines():
            wire, sep, value = line.strip().partition(":")
            if sep:
                self._receive(wire.upper(), value.strip())

    def _receive(self, wire, value):
        if wire in (STATUS, NETWORK):
            self._in_packet = wire == self._header
            self._fresh = {}
        if wire not in FIELDS:
            return
        update = read_field(wire, value)
        for key, reading in update.items():
            self.set_state(key, reading)
        if self._in_packet and self._waiting_for_reply():
            self._fresh.update(update)
            if self._needed.issubset(self._fresh):
                self._reply.set_result(dict(self._fresh))

    def _waiting_for_reply(self):
        return self._reply is not None and not self._reply.done()

    async def poll(self):
        await self._request("getStatus", {"identity", "firmware"})

    async def _liveness_probe(self):
        await self._request("getStatus", {"identity"})

    def _clear_hdmi(self, mode):
        self.set_state("hdmi_feedback", mode)
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
        spec = self.DRIVER_INFO["commands"].get(command)
        if spec is None:
            raise CommandRefusedError(
                "This control is unavailable for the configured routing owner."
            )
        if command == "refresh_network":
            return await self._request("getNetStatus", {"switch_port"}, header=NETWORK)
        if "query_for" in spec:
            return await self._request("getStatus", {spec["query_for"]})
        wire, expected = self._operation(command, params or {})
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
            url = stream_url(params["url"])
            return "setSettings:streamURL:" + url, {"stream_url": url}
        if command == "set_wall_layout":
            return wall_layout(params)
        if command == "play_local":
            slot = number(params["playlist"], 1, 8)
            return "local:" + str(slot), {"playlist": slot, "play_mode": "local"}
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
