"""AMX SVSI N2612S and N3312D encoders. Python correlates the multi-field Direct Control reply
(including the password-prefixed TLS form) and the separate HControl event
stream. Both physical HDMI inputs have feedback. Policy, selected input and
live/HostPlay are independent. A command never manufactures signal state.
"""

from __future__ import annotations
import asyncio
import json
import re
from copy import deepcopy
from decimal import Decimal
from urllib.parse import urlsplit
from openavc.core.device_traffic import get_traffic_recorder
from openavc.drivers.base import BaseDriver, CommandRefusedError, ConnectionFaultError

ENCODER_MODEL = re.compile(
    r"(?:NMX-ENC-)?((?:N2612S|N3312D)(?:-C)?)(?!-C)(?=\d|[ :\t-]|$)", re.I
)
STATUS = "SVSI_TXGEN2"
NETWORK = "SVSI_NETSTATS"
SERIAL = ("serial_baud", "serial_bits", "serial_parity", "serial_stops")
BAUD = {1200, 1800, 2400, 4800, 9600, 19200, 38400, 57600, 115200}
BIT = {"0": False, "1": True}
CABLE = {"connected": True, "disconnected": False}
POLICY = {"auto": 0, "hdmi1": 1, "hdmi2": 2}
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
    "ID": ("identified", {"0": True, "1": False}),
    "BAUD": ("serial_baud", int),
    "SNUMB": ("serial_bits", int),
    "SP2S": ("serial_stops", int),
    "SPAR": ("serial_parity", str.lower),
    "VSRC": ("selection_mode", {k: k for k in POLICY}),
    "VSTS": ("active_input", {"1": 1, "2": 2}),
    "STREAM": ("stream_number", int),
    "USBRECPORTSTATUS": ("record_port_enabled", BIT),
    "USBSTATUS": ("record_drive_connected", BIT),
    "USBFREE": ("record_free_space", str),
    "AVHENABLE": ("dante_av_h_enabled", BIT),
    "MUTE": ("audio_muted", BIT),
    "VMUTE": ("video_muted", BIT),
    "PLAYMODE": ("play_mode", str.lower),
    "PLAYLIST": ("playlist", int),
    "HDMIAUDIO": ("audio_source", {"on": "HDMI", "off": "Analog"}),
    "LINEGAIN": ("line_gain", int),
    "SAMPLE": ("sample_rate", str),
    "ENCMODE": ("encode_mode", {"1": "SingleStream", "2": "MultiStream"}),
    "H26XMODE": ("codec", str.upper),
    "H26XPROFILE": (
        "profile",
        lambda s: {
            "youtube": "YouTube",
            "panopto": "Panopto",
            "n3000": "N3000",
            "custom": "Custom",
        }.get(s.lower(), s),
    ),
    "H26XSHUTDOWN": ("h26x_shutdown", BIT),
    "KVMENABLESTATUS": ("kvm_enabled", BIT),
    "USB20ENABLESTATUS": ("usb_enabled", BIT),
}
INPUT_FIELDS = {
    "HDMIINPUT": (1, "connected", CABLE),
    "INPUTRES": (1, "resolution", str),
    "HDMI2INPUT": (2, "connected", CABLE),
    "HDMI2INPUTRES": (2, "resolution", str),
}
HC_ROOT = "/encoder/systemStatus/"
HC_INPUTS = {
    f"{HC_ROOT}{token}{n}{suffix}": (n, key)
    for n in (1, 2)
    for token, suffix, key in (
        ("hdmi", "Status", "connected"),
        ("hdmi", "Resolution", "resolution"),
        ("hdcp", "Status", "hdcp"),
    )
}
EVENTS = {
    f"{HC_ROOT}hdmi{n}{event}": (f"{HC_ROOT}hdmi{n}{suffix}", f"{HC_ROOT}hdcp{n}Status")
    for n in (1, 2)
    for event, suffix in (
        ("CableStatusChanged", "Status"),
        ("ResolutionChanged", "Resolution"),
    )
}
HC_FLAGS = {
    "mwc_tx": "/encoder/streamSetup/mwcStream/txEnable",
    "h26x_tx": "/encoder/streamSetup/h.26xStream/txEnable",
    "image_enabled": "/encoder/streamSetup/mwcStream/previewImage",
}
TOGGLES = {
    "audio_muted": ("unmute", "mute"),
    "video_muted": ("vunmute", "vmute"),
    "usb_enabled": ("setSettings:usbEnable:off", "setSettings:usbEnable:on"),
    "kvm_enabled": ("setSettings:kvmEnable:off", "setSettings:kvmEnable:on"),
    "mwc_tx": ("txdisable", "txenable"),
    "h26x_tx": ("setSettings:H26xTxDisable", "setSettings:H26xTxEnable"),
    "image_enabled": ("setSettings:Preview:off", "setSettings:Preview:on"),
}
PREFIX = {
    "selection_mode": "vidsrc:",
    "encode_mode": "setSettings:encodeMode:",
    "codec": "setSettings:H26xMode:",
    "profile": "setSettings:H26xProfile:",
}


RECORDER_FIELDS = {
    "record_port_enabled",
    "record_drive_connected",
    "record_free_space",
    "dante_av_h_enabled",
}
RECORDER_STATES = RECORDER_FIELDS | {
    "record_media_status",
    "last_record_request",
    "record_free_bytes",
}
RECORDER_COMMANDS = {
    "set_record_port_enabled",
    "start_record",
    "stop_record",
    "set_record_basename",
    "read_record_port_enabled",
    "read_record_drive_connected",
    "read_record_free_space",
    "read_dante_av_h_enabled",
    "set_primary_tx",
}
N2612_COMMANDS = {
    "set_mwc_tx",
    "set_usb_enabled",
    "read_usb_enabled",
    "set_kvm_enabled",
    "read_kvm_enabled",
}
N2612_STATES = {
    "mwc_tx",
    "usb_enabled",
    "kvm_enabled",
    "h26x_shutdown",
    "h26x_tx",
    "image_enabled",
}


def valid_basename(name):
    return (
        bool(re.fullmatch(r"[!-~]+", name))
        and not re.search(r"[/\\:]", name)
        and name not in (".", "..")
    )


def space_is_zero(value):
    # The API gives no unit for USBFree. Keep its text for display.
    return bool(
        re.fullmatch(r"0+(?:[.,]0+)?\s*(?:[kmgt]?i?b(?:ytes?)?)?", str(value), re.I)
    )


def free_bytes(value):
    """Normalize explicit decimal or binary units for threshold bindings."""
    match = re.fullmatch(
        r"(\d{1,16}(?:[.,]\d{1,6})?)\s*(bytes?|b|[kmgt]i?b)", str(value), re.I
    )
    if not match:
        return None
    amount, unit = match.groups()
    unit = unit.lower()
    power = {"k": 1, "m": 2, "g": 3, "t": 4}.get(unit[0], 0)
    base = 1024 if "i" in unit else 1000
    return int(Decimal(amount.replace(",", ".")) * base**power)


def media_status(enabled, connected, free):
    if connected is False:
        return "No USB drive"
    if enabled is False:
        return "USB recording port disabled"
    if connected is not True or enabled is not True:
        return "Waiting for USB status"
    if space_is_zero(free):
        return "No free space"
    return "USB drive connected"


def convert(reader, value):
    return reader[value.lower()] if isinstance(reader, dict) else reader(value)


def split_field(line):
    # The N26xxS table prints the TX header joined to the identity. Other
    # N-Series Direct Control examples put a colon between them.
    for header in (STATUS, NETWORK):
        if line.upper().startswith(header):
            return header, line[len(header) :].lstrip(": \t")
    wire, _, value = line.partition(":")
    return wire.upper(), value.strip()


def local_preview(url, hosts):
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "rtsp" or parsed.hostname not in hosts:
            return ""
        if any((parsed.username is not None, parsed.query, parsed.fragment)):
            return ""
        # Validate the port even though the player uses the original address.
        if parsed.port == 0 or not re.fullmatch(r"[!-~]+", url):
            return ""
        return url
    except ValueError:
        return ""


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
        raise CommandRefusedError("Use no flow control on the encoder RS-232 port.")
    return dict(zip(SERIAL, (baud, bits, parity, stops)))


class AMXN2612SDriver(BaseDriver):
    DRIVER_INFO = {
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
        "id": "amx_svsi_n2612s",
        "name": "AMX SVSI N2612S and N3312D Encoders",
        "description": "Select either HDMI input or Auto on the standalone encoder or rack card, monitor both inputs, and control the SVSI and "
        "H.26x streams. N3312D standalone and card encoders also offer USB recording.",
        "tags": [
            "av-over-ip",
            "encoder",
            "svsi",
            "hdmi",
            "h26x",
            "serial-bridge",
            "usb-recording",
            "dante-av-h",
        ],
        "compatible_models": [
            {
                "manufacturer": "AMX",
                "models": ["NMX-ENC-N2612S", "NMX-ENC-N2612S-C"],
                "confidence": "untested",
                "notes": "Standalone AMX-N26E001 and rack card AMX-N26E001C. Both have two HDMI inputs, USB extension and a secondary H.26x stream. Simulator tested. "
                "Physical qualification pending.",
            },
            {
                "manufacturer": "AMX",
                "models": ["NMX-ENC-N3312D", "NMX-ENC-N3312D-C"],
                "confidence": "untested",
                "notes": "AMX-N33E001 and AMX-N33E001C. AMX specifies the same Direct Control API "
                "and recording functions for both. Simulator tested; physical qualification pending.",
            },
        ],
        "default_config": {
            "encoder_model": "Auto",
            "host": "",
            "port": 50002,
            "ssl": False,
            "verify_ssl": True,
            "password": "",
            "hdmi_events": True,
            "poll_interval": 10,
            "routing_owner": "SVSI",
            "web_scheme": "https",
        },
        "config_schema": {
            "encoder_model": {
                "type": "enum",
                "label": "Encoder model",
                "values": ["Auto", "N2612S", "N2612S-C", "N3312D", "N3312D-C"],
                "default": "Auto",
                "help": "Auto reads the hardware identity. Choose a model to build offline or simulate that encoder. Auto simulates an N2612S.",
            },
            "host": {"type": "string", "label": "Encoder address", "required": True},
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
                "label": "Verify encoder certificate",
                "default": True,
                "advanced": True,
            },
            "password": {
                "type": "string",
                "label": "Command password",
                "default": "",
                "secret": True,
                "help": "Required for secure control. Enter the command password "
                "configured on the encoder.",
            },
            "hdmi_events": {
                "type": "boolean",
                "label": "Use HControl feedback on ordinary connections",
                "default": True,
                "help": "On the N2612S, adds cable events, HDCP and transmit-enable readback over "
                "TCP 4197. Secure Direct Control never opens this "
                "connection.",
            },
            "poll_interval": {
                "type": "integer",
                "label": "Status refresh (seconds)",
                "default": 10,
                "min": 0,
                "advanced": True,
            },
            "routing_owner": {
                "type": "enum",
                "label": "Routing configured on encoder",
                "values": ["SVSI", "Dante AV"],
                "default": "SVSI",
                "help": "Match the mode commissioned on this unit. The Dante AV "
                "profile leaves network stream assignment to the Dante "
                "tools.",
            },
            "web_scheme": {
                "type": "enum",
                "label": "Encoder web preview protocol",
                "values": ["https", "http"],
                "default": "https",
                "advanced": True,
                "help": "Match the encoder web service for its snapshot URL. The "
                "driver never changes the web service.",
            },
        },
        "help": {
            "overview": "One encoder represents one source location. HDMI 1 and HDMI 2 show their own "
            "cable and resolution readings. Auto selects the last connected input. The "
            "active input, source selection policy and local playlist are separate "
            "readings.",
            "setup": "1. Enter the IP address or DNS name of the encoder itself, including for a card in the rack chassis. Select its model for offline design or simulation. Use TCP 50002 for "
            "ordinary Direct Control. For secure control enable TLS, use port 50102 and enter "
            "the unit command password. Verify the encoder certificate with a trusted issuer. "
            "One controller can occupy each Direct Control port.\n"
            "2. Match the routing profile to the physical encoder. Use a unique SVSI stream "
            "number for this encoder and enter that number in Sources on the N2622S decoder. "
            "Switching an HDMI input changes the source at this encoder. Selecting a stream "
            "for a room display is performed at its decoder. Dante Controller owns Dante AV "
            "routing.\n"
            "3. Inspect each HDMI input for connection, resolution and selection. On the N2612S, ordinary "
            "control can receive HDMI events over TCP 4197. Direct Control refreshes both "
            "inputs even with HControl disabled. Stream transmit switches have HControl "
            "readback. In secure mode those switches acknowledge delivery only.\n"
            "4. Use Multi Stream for the H.26x output. On N2612S, when HControl confirms transmission and "
            "the encoder reports a local RTSP URL, the Video Panel can offer that stream. A "
            "confirmed web preview exposes the encoder’s /snapshot.jpg address. Access "
            "requires the site’s network and web permissions. On N3312D, Video Panel offers the reported local RTSP address in Multi Stream. Enable H.26x transmission for playback. Simulation supplies status without serving video.\n"
            "5. Connect serial equipment with its own driver using Through a bridge, this "
            "encoder and its RS-232 connector. Use TCP 50004 only where site policy enables "
            "ordinary serial. The encoder applies the downstream baud, data bits, parity and "
            "stop bits. Check Serial line setup, then obtain a reply from the attached device. "
            "AMX documents unsolicited replies from firmware 1.8.8 and binary NUL transmission "
            "from 2.0.8. Secure serial bridging on 50104 remains unavailable. The serial firmware versions above apply to N2600S.\n"
            "6. N3312D and N3312D-C: open the Record page on the encoder to commission storage and transfer. "
            "OpenAVC reads the USB recording port, drive presence and free space via Direct Control. "
            "Start and Stop report the last acknowledged request. The API supplies no active-recording tally. "
            "HDCP-protected sources cannot be recorded. Stopping can trigger the configured file transfer. "
            "Use Dante Controller for routing while Dante AV-H is enabled.",
            "connection": "Check that the selected port is free, TLS and the command password match the "
            "encoder, and the certificate is trusted. For an absent picture, check the "
            "selected connector, its detected resolution, playback mode, mute, transmit "
            "enable and the destination decoder’s selected stream.",
        },
        "discovery": {
            "manufacturer_alias": ["amx", "svsi"],
            "port_open": [50002, 50102],
            "tcp_probe": {
                "port": 50002,
                "send_ascii": "getStatus\r",
                "expect_regex": "(?i)SVSI_TXGEN2\\s*:?\\s*(?:NMX-ENC-)?(?:N2612S|N3312D)(?:-C)?(?!-C)(?=[\\d\\s:\\-]|$)",
                "timeout_ms": 1500,
            },
        },
        "state_variables": {
            "record_port_enabled": {
                "type": "boolean",
                "label": "USB recording port enabled",
                "control": True,
                "cloud_priority": "high",
            },
            "record_drive_connected": {
                "type": "boolean",
                "label": "Recording drive connected",
                "cloud_priority": "high",
            },
            "record_free_space": {
                "type": "string",
                "label": "Recording drive free space",
                "help": "As reported by the encoder, including any unit. Empty when no drive is connected.",
            },
            "record_free_bytes": {
                "type": "integer",
                "label": "Recording drive free bytes",
                "help": "For low-space monitors. Available when the encoder reports free space with a recognized unit.",
            },
            "record_media_status": {
                "type": "string",
                "label": "Recording media",
                "cloud_priority": "high",
                "help": "Summarizes port, drive and free-space readings. HDCP and an active recording are not reported here.",
            },
            "last_record_request": {
                "type": "string",
                "label": "Last recording request acknowledged",
                "help": "OpenAVC's last acknowledged Start or Stop in this connection. Actual recording and file transfer must be checked on the encoder.",
            },
            "dante_av_h_enabled": {
                "type": "boolean",
                "label": "Dante AV-H enabled",
                "cloud_priority": "high",
            },
            "identity": {
                "type": "string",
                "label": "Encoder identifier",
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
            "switch_name": {
                "type": "string",
                "label": "Connected switch",
                "cloud_priority": "low",
            },
            "switch_chassis": {
                "type": "string",
                "label": "Connected switch chassis ID",
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
            "serial_setup": {
                "type": "string",
                "label": "Serial line setup",
                "cloud_priority": "high",
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
            "serial_parity": {
                "type": "string",
                "label": "Serial parity",
                "cloud_priority": "low",
            },
            "serial_stops": {
                "type": "integer",
                "label": "Serial stop bits",
                "cloud_priority": "low",
            },
            "hdmi_feedback": {"type": "string", "label": "HDMI feedback connection"},
            "audio_muted": {"type": "boolean", "label": "Audio muted", "control": True},
            "video_muted": {"type": "boolean", "label": "Video muted", "control": True},
            "play_mode": {"type": "string", "label": "Playback mode"},
            "playlist": {"type": "integer", "label": "Local playlist"},
            "usb_enabled": {
                "type": "boolean",
                "label": "USB 2.0 enabled",
                "control": True,
            },
            "kvm_enabled": {"type": "boolean", "label": "KVM enabled", "control": True},
            "identified": {
                "type": "boolean",
                "label": "Identification active",
                "control": False,
            },
            "selection_mode": {
                "type": "enum",
                "label": "HDMI selection mode",
                "values": ["auto", "hdmi1", "hdmi2"],
                "control": True,
            },
            "preferred_input": {
                "type": "integer",
                "label": "Manual HDMI selection (0 = Auto)",
                "min": 0,
                "max": 2,
            },
            "active_input": {
                "type": "integer",
                "label": "Active HDMI input",
                "min": 1,
                "max": 2,
                "control": True,
                "cloud_priority": "high",
            },
            "stream_number": {
                "type": "integer",
                "label": "MWC video stream number",
                "min": 1,
                "max": 32511,
                "control": True,
            },
            "audio_source": {
                "type": "enum",
                "label": "Audio source",
                "values": ["HDMI", "Analog"],
                "control": True,
            },
            "sample_rate": {"type": "string", "label": "Reported audio sample rate"},
            "line_gain": {
                "type": "integer",
                "label": "Analog input gain",
                "min": -12,
                "max": 12,
                "unit": "dB",
            },
            "encode_mode": {
                "type": "enum",
                "label": "Encoder mode",
                "values": ["SingleStream", "MultiStream"],
                "control": True,
            },
            "codec": {
                "type": "enum",
                "label": "H.26x codec",
                "values": ["H264", "H265"],
                "control": True,
            },
            "profile": {"type": "string", "label": "H.26x profile", "control": True},
            "h26x_shutdown": {"type": "boolean", "label": "H.26x subsystem shut down"},
            "mwc_tx": {
                "type": "boolean",
                "label": "MWC transmit enabled",
                "help": "Reported through HControl on ordinary connections.",
            },
            "h26x_tx": {
                "type": "boolean",
                "label": "H.26x transmit enabled",
                "help": "Reported through HControl on ordinary connections.",
            },
            "image_enabled": {
                "type": "boolean",
                "label": "Web snapshot enabled",
                "help": "Reported through HControl on ordinary connections.",
            },
            "preview_url": {
                "type": "string",
                "label": "Live preview URL",
                "cloud_priority": "low",
            },
            "preview_format": {
                "type": "string",
                "label": "Live preview format",
                "cloud_priority": "low",
            },
            "preview_status": {
                "type": "string",
                "label": "Video Panel availability",
                "cloud_priority": "low",
            },
            "preview_status_detail": {
                "type": "string",
                "label": "Video Panel setup",
                "cloud_priority": "low",
            },
            "snapshot_url": {
                "type": "string",
                "label": "Web snapshot URL",
                "cloud_priority": "low",
            },
        },
        "child_entity_types": {
            "input": {
                "label": "HDMI input",
                "label_plural": "HDMI inputs",
                "id_format": {"type": "integer", "min": 1, "max": 2},
                "instances": {"count": 2},
                "state_variables": {
                    "connected": {
                        "type": "boolean",
                        "label": "Cable detected",
                        "cloud_priority": "high",
                    },
                    "resolution": {"type": "string", "label": "Input resolution"},
                    "selected": {
                        "type": "boolean",
                        "label": "Active input",
                        "cloud_priority": "high",
                    },
                    "hdcp": {"type": "string", "label": "HDCP status (HControl)"},
                },
                "summary_fields": ["connected", "resolution", "selected"],
            }
        },
        "routing": {
            "source_child_type": "input",
            "source_param": "input",
            "planes": [
                {
                    "label": "Encoder HDMI source",
                    "command": "route_input",
                    "route_property": "active_input",
                }
            ],
        },
        "commands": {
            "refresh_status": {
                "label": "Read encoder status",
                "help": "Refresh identity, both inputs, streams, playback, audio and "
                "serial readings.",
                "query_for": "identity",
            },
            "refresh_network": {
                "label": "Read switch port",
                "help": "Read the connected switch and port information provided by "
                "LLDP.",
                "query_for": "switch_port",
            },
            "route_input": {
                "label": "Select HDMI input",
                "help": "Switch to a fixed HDMI input. The Matrix follows the input the "
                "encoder reports as active.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "label": "Input",
                        "child_type": "input",
                        "required": True,
                    }
                },
                "sets": {"preferred_input": "{input}"},
            },
            "set_selection_mode": {
                "label": "Set HDMI selection mode",
                "help": "Choose the last connected input automatically or hold "
                "HDMI 1 or HDMI 2.",
                "params": {
                    "value": {
                        "type": "enum",
                        "label": "Mode",
                        "values": ["auto", "hdmi1", "hdmi2"],
                        "required": True,
                    }
                },
                "sets": {"selection_mode": "{value}"},
            },
            "set_audio_source": {
                "label": "Choose audio source",
                "help": "Choose embedded HDMI audio or the analog stereo connector.",
                "params": {
                    "value": {
                        "type": "enum",
                        "label": "Audio source",
                        "values": ["HDMI", "Analog"],
                        "required": True,
                    }
                },
                "sets": {"audio_source": "{value}"},
            },
            "set_stream_number": {
                "label": "Set MWC stream number",
                "help": "Assign a unique stream to this encoder. Update decoders "
                "that were receiving the previous number.",
                "params": {
                    "value": {
                        "type": "integer",
                        "label": "Stream number",
                        "min": 1,
                        "max": 32511,
                        "required": True,
                    }
                },
                "sets": {"stream_number": "{value}"},
                "confirm": "Changes the stream address used by receiving "
                "decoders. Continue?",
            },
            "set_encode_mode": {
                "label": "Set streaming mode",
                "help": "Single Stream carries the main stream. Multi Stream also "
                "makes the H.26x output available.",
                "params": {
                    "value": {
                        "type": "enum",
                        "label": "Mode",
                        "values": ["SingleStream", "MultiStream"],
                        "required": True,
                    }
                },
                "sets": {"encode_mode": "{value}"},
            },
            "set_codec": {
                "label": "Set H.26x codec",
                "help": "Select H.264 or H.265 for the secondary output.",
                "params": {
                    "value": {
                        "type": "enum",
                        "label": "Codec",
                        "values": ["H264", "H265"],
                        "required": True,
                    }
                },
                "sets": {"codec": "{value}"},
            },
            "set_profile": {
                "label": "Set H.26x profile",
                "help": "Apply a stored encoding profile. Check its destination and "
                "codec in the encoder web interface.",
                "params": {
                    "value": {
                        "type": "enum",
                        "label": "Profile",
                        "values": ["YouTube", "Panopto", "N3000", "Custom"],
                        "required": True,
                    }
                },
                "sets": {"profile": "{value}"},
            },
            "set_audio_muted": {
                "label": "Mute audio",
                "help": "Mute the transmitted audio.",
                "params": {
                    "value": {"type": "boolean", "label": "Enabled", "required": True}
                },
                "sets": {"audio_muted": "{value}"},
            },
            "set_video_muted": {
                "label": "Mute video",
                "help": "Send a black picture on the main video stream.",
                "params": {
                    "value": {"type": "boolean", "label": "Enabled", "required": True}
                },
                "sets": {"video_muted": "{value}"},
            },
            "set_kvm_enabled": {
                "label": "Enable KVM",
                "help": "Enable or disable KVM traffic.",
                "params": {
                    "value": {"type": "boolean", "label": "Enabled", "required": True}
                },
                "sets": {"kvm_enabled": "{value}"},
            },
            "set_usb_enabled": {
                "label": "Enable USB 2.0",
                "help": "Enable or disable USB 2.0 traffic.",
                "params": {
                    "value": {"type": "boolean", "label": "Enabled", "required": True}
                },
                "sets": {"usb_enabled": "{value}"},
            },
            "set_mwc_tx": {
                "label": "Set MWC transmission",
                "help": "Enable or disable MWC transmission. Direct Control acknowledges "
                "the request. HControl reads back the setting where available.",
                "params": {
                    "value": {"type": "boolean", "label": "Enabled", "required": True}
                },
            },
            "set_h26x_tx": {
                "label": "Set H.26x transmission",
                "help": "Enable or disable H.26x transmission. Direct Control "
                "acknowledges the request. HControl reads back the setting "
                "where available.",
                "params": {
                    "value": {"type": "boolean", "label": "Enabled", "required": True}
                },
            },
            "set_image_enabled": {
                "label": "Set Web snapshot",
                "help": "Enable or disable Web snapshot. Direct Control "
                "acknowledges the request. HControl reads back the "
                "setting where available.",
                "params": {
                    "value": {"type": "boolean", "label": "Enabled", "required": True}
                },
            },
            "play_local": {
                "label": "Play local playlist",
                "help": "Select one of the eight host playlists stored on the encoder.",
                "params": {
                    "playlist": {
                        "type": "integer",
                        "label": "Playlist",
                        "required": True,
                        "min": 1,
                        "max": 8,
                    }
                },
                "sets": {"play_mode": "local", "playlist": "{playlist}"},
            },
            "play_live": {
                "label": "Return to live HDMI",
                "help": "Leave HostPlay and encode the selected input.",
                "sets": {"play_mode": "live"},
            },
            "identify": {
                "label": "Identify the encoder",
                "help": "Activate identification on this physical unit.",
                "sets": {"identified": True},
            },
            "send_saved_ir": {
                "label": "Send saved IR command",
                "help": "Send an IR command already stored on this unit. The source "
                "equipment provides no feedback via IR.",
                "params": {
                    "name": {
                        "type": "string",
                        "label": "Saved IR name",
                        "required": True,
                    }
                },
            },
            "read_active_input": {
                "label": "Read Active HDMI input",
                "help": "Read the current value from the encoder.",
                "query_for": "active_input",
            },
            "read_selection_mode": {
                "label": "Read HDMI selection mode",
                "help": "Read the current value from the encoder.",
                "query_for": "selection_mode",
            },
            "read_preferred_input": {
                "label": "Read Manual HDMI selection (0 = Auto)",
                "help": "Read the current value from the encoder.",
                "query_for": "preferred_input",
            },
            "read_stream_number": {
                "label": "Read MWC video stream number",
                "help": "Read the current value from the encoder.",
                "query_for": "stream_number",
            },
            "read_encode_mode": {
                "label": "Read Encoder mode",
                "help": "Read the current value from the encoder.",
                "query_for": "encode_mode",
            },
            "read_audio_source": {
                "label": "Read Audio source",
                "help": "Read the current value from the encoder.",
                "query_for": "audio_source",
            },
            "read_audio_muted": {
                "label": "Read Audio muted",
                "help": "Read the current value from the encoder.",
                "query_for": "audio_muted",
            },
            "read_video_muted": {
                "label": "Read Video muted",
                "help": "Read the current value from the encoder.",
                "query_for": "video_muted",
            },
            "read_kvm_enabled": {
                "label": "Read KVM enabled",
                "help": "Read the current value from the encoder.",
                "query_for": "kvm_enabled",
            },
            "read_usb_enabled": {
                "label": "Read USB 2.0 enabled",
                "help": "Read the current value from the encoder.",
                "query_for": "usb_enabled",
            },
            "read_codec": {
                "label": "Read H.26x codec",
                "help": "Read the current value from the encoder.",
                "query_for": "codec",
            },
            "read_profile": {
                "label": "Read H.26x profile",
                "help": "Read the current value from the encoder.",
                "query_for": "profile",
            },
            "read_playlist": {
                "label": "Read Local playlist",
                "help": "Read the current value from the encoder.",
                "query_for": "playlist",
            },
            "read_play_mode": {
                "label": "Read Playback mode",
                "help": "Read the current value from the encoder.",
                "query_for": "play_mode",
            },
            "read_identified": {
                "label": "Read Identification active",
                "help": "Read the current value from the encoder.",
                "query_for": "identified",
            },
            "set_record_port_enabled": {
                "label": "Enable USB recording port",
                "help": "Allow the encoder to record to an attached USB drive.",
                "params": {
                    "value": {"type": "boolean", "label": "Enabled", "required": True}
                },
                "sets": {"record_port_enabled": "{value}"},
            },
            "start_record": {
                "label": "Start recording",
                "help": "Start recording the encoded source to the USB drive. HDCP-protected video cannot be recorded. The reply acknowledges the request.",
            },
            "stop_record": {
                "label": "Stop recording",
                "help": "Stop the current recording. The encoder may transfer the file according to its settings.",
                "confirm": "Stop the recording and allow the configured file transfer?",
            },
            "set_record_basename": {
                "label": "Set recording file base name",
                "help": "Set the base name for subsequent recordings. The encoder acknowledges the request.",
                "params": {
                    "name": {"type": "string", "label": "Base name", "required": True}
                },
            },
            "read_record_drive_connected": {
                "label": "Check recording drive",
                "query_for": "record_drive_connected",
            },
            "read_record_port_enabled": {
                "label": "Read USB recording port",
                "query_for": "record_port_enabled",
            },
            "read_record_free_space": {
                "label": "Read recording drive free space",
                "query_for": "record_free_space",
            },
            "read_dante_av_h_enabled": {
                "label": "Read Dante AV-H mode",
                "query_for": "dante_av_h_enabled",
            },
            "set_primary_tx": {
                "label": "Set primary stream transmission",
                "help": "Enable or disable the main SVSI stream. Direct Control acknowledges the command.",
                "params": {
                    "value": {"type": "boolean", "label": "Enabled", "required": True}
                },
            },
        },
        "device_settings": {
            "record_port_enabled": {
                "label": "USB recording port",
                "type": "boolean",
                "help": "Allow recording to an attached USB drive.",
                "state_key": "record_port_enabled",
                "setup": False,
            },
            "selection_mode": {
                "type": "enum",
                "label": "HDMI selection mode",
                "values": ["auto", "hdmi1", "hdmi2"],
                "help": "Choose the last connected input automatically or "
                "hold HDMI 1 or HDMI 2.",
                "state_key": "selection_mode",
                "setup": False,
            },
            "audio_source": {
                "type": "enum",
                "label": "Audio source",
                "values": ["HDMI", "Analog"],
                "help": "Choose embedded HDMI audio or the analog stereo connector.",
                "state_key": "audio_source",
                "setup": False,
            },
            "encode_mode": {
                "type": "enum",
                "label": "Encoder mode",
                "values": ["SingleStream", "MultiStream"],
                "help": "Single Stream carries the main stream. Multi Stream "
                "also makes the H.26x output available.",
                "state_key": "encode_mode",
                "setup": False,
            },
            "codec": {
                "type": "enum",
                "label": "H.26x codec",
                "values": ["H264", "H265"],
                "help": "Select H.264 or H.265 for the secondary output.",
                "state_key": "codec",
                "setup": False,
            },
            "profile": {
                "type": "enum",
                "label": "H.26x profile",
                "values": ["YouTube", "Panopto", "N3000", "Custom"],
                "help": "Apply a stored encoding profile. Check its destination and "
                "codec in the encoder web interface.",
                "state_key": "profile",
                "setup": False,
            },
            "usb_enabled": {
                "type": "boolean",
                "label": "USB 2.0 enabled",
                "help": "Enable or disable USB 2.0 traffic.",
                "state_key": "usb_enabled",
                "setup": False,
            },
            "kvm_enabled": {
                "type": "boolean",
                "label": "KVM enabled",
                "help": "Enable or disable KVM traffic.",
                "state_key": "kvm_enabled",
                "setup": False,
            },
        },
        "quick_actions": [
            "route_input",
            "set_selection_mode",
            "set_audio_muted",
            "play_live",
            "refresh_status",
            "identify",
        ],
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._model = self.config.get("encoder_model", "Auto")
        self._record_lock = asyncio.Lock()
        self._reported_space = ""
        self._secure = False
        self._lock = asyncio.Lock()
        self._serial_lock = asyncio.Lock()
        self._hc_lock = asyncio.Lock()
        self._flag_lock = asyncio.Lock()
        self._reply = None
        self._header = None
        self._in_packet = False
        self._fresh = {}
        self._needed = set()
        self._watch = None
        self._event_refresh = None
        self._status_dirty = False
        self._writer = None
        self._hc_reply = None
        self._hc_path = None
        self._rtsp = ""
        self._configure_surface()
        self._register_inputs()

    @property
    def _recorder(self):
        return self._model in ("N3312D", "N3312D-C")

    def _configure_surface(self):
        info = deepcopy(type(self).DRIVER_INFO)
        self.DRIVER_INFO = info
        if self._recorder:
            self._omit_model_features(N2612_COMMANDS, N2612_STATES)
            self._recorder_surface(info)
        else:
            self._omit_model_features(RECORDER_COMMANDS, RECORDER_STATES)
            info["device_settings"].pop("record_port_enabled")
        if self.config.get("routing_owner") == "Dante AV":
            info["commands"].pop("set_stream_number")
        if self._uses_hcontrol():
            self._add_hcontrol_readback(info["commands"])

    def _omit_model_features(self, commands, states):
        for cmd in commands:
            self.DRIVER_INFO["commands"].pop(cmd)
        for key in states:
            self.DRIVER_INFO["state_variables"].pop(key)

    def _uses_hcontrol(self):
        return (
            not self._recorder
            and not self.config.get("ssl")
            and self.config.get("hdmi_events", True)
        )

    def _add_hcontrol_readback(self, commands):
        for key in HC_FLAGS:
            commands["set_" + key]["sets"] = {key: "{value}"}
            commands["read_" + key] = {
                "label": "Read " + self.DRIVER_INFO["state_variables"][key]["label"],
                "query_for": key,
            }

    def _recorder_surface(self, info):
        info["source_url"] = (
            "https://www.amx.com/en-US/site_elements/direct-control-api-n33x2d"
        )
        info["help"]["setup"] = (
            "1. Add the standalone encoder or rack card by its own IP address. Auto identifies the equipment. "
            "Choose N3312D or N3312D-C to use that model in simulation or offline design. "
            "Use TCP 50002 for ordinary control, or TLS 50102 with the encoder command password.\n"
            "2. Check both HDMI inputs on the device page. Source selection, active input, cable detection and "
            "resolution are independent readings. For network routing in Dante AV-H mode, use Dante Controller.\n"
            "3. Enable the USB recording port and attach the drive. Check Recording media and Recording drive "
            "free space. Start and Stop acknowledge the request. The encoder cannot record HDCP-protected video. "
            "Check the saved file on the unit. Stop can initiate an automatic transfer configured in its web interface.\n"
            "4. For Video Panel, enable secondary H.26x transmission in Multi Stream and configure a local RTSP output. "
            "The Open Web UI button also provides access to the encoder's preview and file transfer settings.\n"
            "5. For downstream serial, bind the attached equipment's own driver through the encoder's RS-232 port "
            "on ordinary TCP 50004. The driver checks line settings. Confirm communication with a reply from the attached equipment."
        )
        for key in ("usb_enabled", "kvm_enabled"):
            info["device_settings"].pop(key)
        info["child_entity_types"]["input"]["state_variables"].pop("hdcp")
        info["state_variables"].pop("snapshot_url")
        info["config_schema"].pop("web_scheme")
        info["config_schema"].pop("hdmi_events")
        info["state_variables"]["stream_number"]["label"] = (
            "Primary video stream number"
        )
        info["commands"]["read_stream_number"]["label"] = (
            "Read primary video stream number"
        )
        info["commands"]["set_stream_number"]["label"] = (
            "Set primary video stream number"
        )
        info["quick_actions"] = [
            "route_input",
            "start_record",
            "stop_record",
            "read_record_free_space",
            "play_live",
            "identify",
        ]
        info["commands"]["set_h26x_tx"]["help"] = (
            "Enable or disable secondary transmission. Direct Control acknowledges the command."
        )
        info["commands"]["set_image_enabled"]["help"] = (
            "Enable or disable the encoder web snapshot. Direct Control acknowledges the command."
        )

    def _register_inputs(self):
        for n in (1, 2):
            self.register_child("input", n, {"label": f"HDMI {n}"})

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
                "Enter the encoder command password without line breaks.",
                code="invalid_config",
            )

    async def _post_connect(self):
        self._register_inputs()
        report = await self._request("getStatus", {"identity", "firmware"})
        # The header can append the unit serial directly to the model.
        match = ENCODER_MODEL.match(report["identity"].strip())
        if not match:
            raise ConnectionFaultError(
                "Unsupported AMX encoder identity.", code="invalid_config"
            )
        actual = match.group(1).upper()
        selected = self.config.get("encoder_model", "Auto")
        if selected != "Auto" and actual.removesuffix("-C") != selected.removesuffix(
            "-C"
        ):
            raise ConnectionFaultError(
                "The reported encoder does not match the selected model.",
                code="invalid_config",
            )
        if actual != self._model:
            self._model = actual
            self._configure_surface()

    async def _initial_sync(self):
        await self._request("getNetStatus", {"mac"}, header=NETWORK)
        if self._recorder:
            await self._request(
                "getStatus", {"record_port_enabled", "record_drive_connected"}
            )
        if self._uses_hcontrol():
            self.set_state("hdmi_feedback", "Connecting to HControl")
            self._watch = asyncio.create_task(self._watch_hcontrol())
        else:
            self.set_state("hdmi_feedback", "Direct Control")

    async def _close_session(self):
        if self._waiting_for_reply():
            self._reply.set_exception(ConnectionError("Encoder disconnected."))
        tasks = [
            task for task in (self._watch, self._event_refresh) if task is not None
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._watch = self._event_refresh = None
        self._rtsp = ""
        if self._recorder:
            self._reported_space = ""
            for key in RECORDER_STATES:
                self.set_state(key, None)
        self._clear_hcontrol("Disconnected")

    async def _send(self, wire):
        if self.transport is None:
            raise ConnectionError("Encoder is disconnected.")
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
                        "The encoder answered but omitted a requested status field."
                    ) from None
                if self.connected:
                    self._force_disconnect(
                        message="No complete reply from the encoder."
                    )
                raise ConnectionError(
                    "Encoder did not return the requested status fields."
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
                "The encoder reported different settings. Check its routing mode, permissions and signal format."
            )

    async def on_data_received(self, data):
        for line in data.decode("utf-8", "replace").splitlines():
            wire, value = split_field(line.strip())
            self._receive(wire, value)

    def _receive(self, wire, value):
        if wire in (STATUS, NETWORK):
            self._in_packet = wire == self._header
            self._fresh = {}
        if wire in INPUT_FIELDS:
            self._receive_input(wire, value)
        elif wire == "STREAMURL":
            self._receive_preview(value)
        elif wire in FIELDS:
            self._receive_field(wire, value)

    def _receive_input(self, wire, value):
        n, key, reader = INPUT_FIELDS[wire]
        try:
            reading = convert(reader, value)
            self.set_child_state("input", n, key, reading)
            self._complete_read({(n, key): reading})
        except (ValueError, KeyError):
            return

    def _receive_preview(self, value):
        hosts = {str(self.config.get("host", "")).lower(), self.get_state("ip")}
        self._rtsp = local_preview(value, hosts)
        if value and not self._rtsp:
            self.redact_in_log(value)
        self._publish_preview()

    def _receive_field(self, wire, value):
        key, reader = FIELDS[wire]
        if key not in self.DRIVER_INFO["state_variables"]:
            return
        try:
            reading = convert(reader, value)
        except (KeyError, ValueError):
            return
        if key in RECORDER_FIELDS:
            self._receive_recorder_field(key, reading)
            return
        update = self._source_update(key, reading)
        for prop, val in update.items():
            self.set_state(prop, val)
        if key in ("encode_mode", "h26x_shutdown"):
            self._update_preview_mode(key, reading)
        self._complete_read(update)

    def _source_update(self, key, reading):
        if key == "selection_mode":
            return {key: reading, "preferred_input": POLICY[reading]}
        if key == "active_input":
            for n in (1, 2):
                self.set_child_state("input", n, "selected", n == reading)
        return {key: reading}

    def _update_preview_mode(self, key, reading):
        if key == "encode_mode" and reading == "SingleStream":
            self._rtsp = ""
        self._publish_preview()

    def _receive_recorder_field(self, key, reading):
        if key == "record_free_space":
            self._reported_space = reading
        else:
            self.set_state(key, reading)
        if key != "dante_av_h_enabled":
            self._update_media()
        self._complete_read({key: reading})

    def _update_media(self):
        connected = self.get_state("record_drive_connected")
        if connected is False:
            self._reported_space = ""
        free = self._reported_space if connected else ""
        self.set_state("record_free_space", free)
        self.set_state("record_free_bytes", free_bytes(free))
        self.set_state(
            "record_media_status",
            media_status(
                self.get_state("record_port_enabled"), connected, self._reported_space
            ),
        )

    def _complete_read(self, update):
        if self._in_packet and self._waiting_for_reply():
            self._fresh.update(update)
            if self._fresh.get("record_drive_connected") is False:
                self._fresh["record_free_space"] = ""
            if self._needed.issubset(self._fresh):
                self._reply.set_result(dict(self._fresh))

    def _publish_preview(self):
        status, detail = self._preview_availability()
        url = self._rtsp if status == "ready" else ""
        self.set_state("preview_url", url)
        self.set_state("preview_format", "rtsp" if url else "")
        self.set_state("preview_status", status)
        self.set_state("preview_status_detail", detail)

    def _preview_availability(self):
        if self.udp_redirect is not None:
            return "unavailable", "The simulator supplies status only."
        if self.get_state("encode_mode") != "MultiStream":
            return "needs_setup", "Select Multi Stream to use the H.26x output."
        if self._recorder:
            return self._recorder_preview()
        return self._n2612_preview()

    def _recorder_preview(self):
        if self._rtsp:
            return (
                "ready",
                "Address reported by the encoder. Enable H.26x transmission for playback.",
            )
        return "needs_setup", "Configure a local RTSP output on the encoder."

    def _n2612_preview(self):
        if self.get_state("h26x_shutdown") is not False:
            return (
                "unavailable",
                "The encoder has not reported an available H.26x output.",
            )
        tx = self.get_state("h26x_tx")
        if tx is None:
            return (
                "unavailable",
                "Confirming transmission requires ordinary HControl feedback.",
            )
        if not tx:
            return "needs_setup", "Enable H.26x transmission on this encoder."
        if not self._rtsp:
            return (
                "needs_setup",
                "Configure a local RTSP stream in the encoder web interface.",
            )
        return "ready", ""

    def _snapshot_url(self):
        if not self.get_state("image_enabled") or self.udp_redirect is not None:
            return ""
        host = self.config["host"]
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        scheme = self.config.get("web_scheme", "https")
        return f"{scheme}://{host}/snapshot.jpg"

    async def poll(self):
        await self._request("getStatus", {"identity", "firmware"})

    async def refresh_children(self):
        self._register_inputs()
        keys = {(n, key) for n, key, _ in INPUT_FIELDS.values()}
        await self._request("getStatus", keys | {"active_input"})

    def _clear_hcontrol(self, detail):
        self.set_state("hdmi_feedback", detail)
        if self._recorder:
            self._publish_preview()
            return
        for n in (1, 2):
            self.set_child_state("input", n, "hdcp", None)
        for key in HC_FLAGS:
            self.set_state(key, None)
        self.set_state("snapshot_url", "")
        self._publish_preview()

    async def _watch_hcontrol(self):
        while True:
            try:
                await self._hcontrol_session()
            except (OSError, ValueError, TimeoutError):
                self._clear_hcontrol("Direct Control; HControl unavailable")
            await asyncio.sleep(15)

    async def _hcontrol_session(self):
        host, port = self.udp_redirect or (self.config["host"], 4197)
        ip = self.local_address(host)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, local_addr=(ip, 0) if ip else None), 3
        )
        self._writer = writer
        monitor = asyncio.create_task(self._read_hcontrol(reader, writer))
        try:
            for path in EVENTS:
                await self._hc_send(writer, "subscribeevent", path)
            for path in HC_INPUTS:
                await self._hc_send(writer, "get", path)
            await self._monitor_flags(monitor)
        finally:
            self._writer = None
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            writer.close()
            await writer.wait_closed()

    async def _monitor_flags(self, monitor):
        while not monitor.done():
            for key in HC_FLAGS:
                await self._read_flag(key)
            await asyncio.wait({monitor}, timeout=30)
        await monitor

    async def _hc_send(self, writer, verb, path):
        message = verb + " " + json.dumps({"path": path}) + "\n"
        writer.write(message.encode())
        await writer.drain()
        self.record_traffic("tx", message, meta={"protocol": "HControl"})

    async def _read_flag(self, key):
        async with self._hc_lock:
            writer = self._writer
            if writer is None:
                raise ConnectionError("HControl feedback is unavailable.")
            self._hc_path = HC_FLAGS[key]
            future = asyncio.get_running_loop().create_future()
            self._hc_reply = future
            try:
                await self._hc_send(writer, "get", self._hc_path)
                return await asyncio.wait_for(future, 3)
            except TimeoutError:
                writer.close()
                raise
            finally:
                self._hc_reply = None
                future.cancel()

    async def _read_hcontrol(self, reader, writer):
        subscribed = set()
        while line := await reader.readline():
            self.record_traffic("rx", line, meta={"protocol": "HControl"})
            verb, _, body = line.decode("utf8", "replace").strip().partition(" ")
            if verb in ("@get", "event", "@subscribeevent"):
                await self._hc_message(writer, verb, json.loads(body), subscribed)
        raise ConnectionError("HControl connection closed.")

    async def _hc_message(self, writer, verb, packet, subscribed):
        if not isinstance(packet, dict):
            return
        path = packet.get("path")
        if not isinstance(path, str):
            return
        if verb == "@get":
            self._hc_value(path, packet.get("value"))
        elif path in EVENTS:
            await self._hc_event(writer, verb, path, subscribed)

    async def _hc_event(self, writer, verb, path, subscribed):
        if verb == "@subscribeevent":
            subscribed.add(path)
            if len(subscribed) == len(EVENTS):
                self.set_state("hdmi_feedback", "Live HDMI events")
            return
        for prop in EVENTS[path]:
            await self._hc_send(writer, "get", prop)
        self._queue_status()

    def _queue_status(self):
        # A cable event can switch the active input in Auto. Direct Control
        # reports the actual selection, including changes made by another user.
        self._status_dirty = True
        if self._event_refresh is None or self._event_refresh.done():
            self._event_refresh = asyncio.create_task(self._refresh_on_event())

    async def _refresh_on_event(self):
        try:
            while self._status_dirty:
                self._status_dirty = False
                await self.poll()
        except (OSError, CommandRefusedError):
            return

    def _hc_value(self, path, value):
        if path in HC_INPUTS:
            n, key = HC_INPUTS[path]
            if key == "connected" and value in (0, 1):
                self.set_child_state("input", n, key, bool(value))
            elif key != "connected" and isinstance(value, str):
                self.set_child_state("input", n, key, value)
            return
        if path == self._hc_path and isinstance(value, bool):
            self._hc_flag(path, value)

    def _hc_flag(self, path, value):
        key = next(k for k, p in HC_FLAGS.items() if p == path)
        self.set_state(key, value)
        if key == "image_enabled":
            self.set_state("snapshot_url", self._snapshot_url())
        else:
            self._publish_preview()
        if self._hc_reply is not None and not self._hc_reply.done():
            self._hc_reply.set_result(value)

    async def send_command(self, command, params=None):
        spec = self.DRIVER_INFO["commands"].get(command)
        if not spec:
            raise CommandRefusedError("Unavailable for this encoder configuration.")
        if "query_for" in spec:
            return await self._query(spec["query_for"])
        if command in ("start_record", "stop_record", "set_record_port_enabled"):
            async with self._record_lock:
                return await self._record_command(command, params or {})
        return await self._send_control(command, params or {}, spec)

    async def _send_control(self, command, params, spec):
        if command == "set_stream_number":
            await self._check_routing_owner()
        key = command.removeprefix("set_")
        if key in HC_FLAGS:
            async with self._flag_lock:
                return await self._set_flag(key, flag((params or {})["value"]), spec)
        wire, expected = self._operation(command, params or {})
        if expected:
            return await self._confirm(wire, expected)
        await self._request(wire, {"identity"})

    async def _check_routing_owner(self):
        if self._recorder:
            reply = await self._request("getStatus", {"dante_av_h_enabled"})
            if reply["dante_av_h_enabled"]:
                raise CommandRefusedError(
                    "Dante AV-H is enabled. Manage network routing in Dante Controller."
                )

    async def _record_command(self, command, params):
        if command == "set_record_port_enabled":
            enabled = flag(params["value"])
            return await self._confirm(
                "setSettings:usbEnable:" + ("on" if enabled else "off"),
                {"record_port_enabled": enabled},
            )
        if command == "start_record":
            report = await self._record_media()
            reason = media_status(
                report["record_port_enabled"],
                report["record_drive_connected"],
                report.get("record_free_space"),
            )
            if reason != "USB drive connected":
                raise CommandRefusedError(reason)
        wire = "startRecord" if command == "start_record" else "stopRecord"
        self.set_state("last_record_request", None)
        await self._request(wire, {"identity"})
        self.set_state(
            "last_record_request",
            "Start acknowledged" if command == "start_record" else "Stop acknowledged",
        )

    async def _record_media(self):
        keys = {"record_port_enabled", "record_drive_connected"}
        try:
            return await self._request("getStatus", keys | {"record_free_space"})
        except CommandRefusedError:
            # A firmware reply may omit USBFree. The port and drive still tell
            # us whether media is present, without assigning a capacity to it.
            self._reported_space = ""
            self._update_media()
            return await self._request("getStatus", keys)

    async def _query(self, key):
        if key == "record_free_space":
            return await self._record_media()
        if key == "switch_port":
            return await self._request("getNetStatus", {key}, header=NETWORK)
        if key in HC_FLAGS:
            return await self._read_flag(key)
        return await self._request("getStatus", {key})

    async def _set_flag(self, key, value, spec):
        verify = "sets" in spec
        if verify and self._writer is None:
            raise CommandRefusedError(
                "HControl readback is unavailable. Check the encoder feedback connection."
            )
        await self._request(TOGGLES[key][value], {"identity"})
        if verify and await self._read_flag(key) != value:
            raise CommandRefusedError(
                "The encoder reported a different transmit or preview setting."
            )

    def _operation(self, command, params):
        if command in ("set_primary_tx", "set_record_basename"):
            return self._recorder_setting(command, params), {}
        if command == "route_input":
            n = number(params["input"], 1, 2)
            return f"vidsrc:hdmi{n}", {"preferred_input": n}
        if command == "play_local":
            n = number(params["playlist"], 1, 8)
            return f"local:{n}", {"play_mode": "local", "playlist": n}
        if command in ("play_live", "identify"):
            return (
                ("live", {"play_mode": "live"})
                if command == "play_live"
                else ("idOn", {"identified": True})
            )
        if command == "send_saved_ir":
            name = params.get("name", "")
            if not re.fullmatch(r"[ -~]+", name):
                raise CommandRefusedError("Enter a saved IR name without line breaks.")
            return "sendir:" + name, {}
        return self._setting(command, params["value"])

    def _recorder_setting(self, command, params):
        if command == "set_primary_tx":
            return "txenable" if flag(params["value"]) else "txdisable"
        name = params.get("name", "")
        if not valid_basename(name):
            raise CommandRefusedError(
                "Enter a file base name without spaces, paths or line breaks."
            )
        return "setSettings:recordBaseName:" + name

    def _setting(self, command, value):
        key = command.removeprefix("set_")
        if key in TOGGLES:
            value = flag(value)
            return TOGGLES[key][value], {key: value}
        if key == "stream_number":
            n = number(value, 1, 32511)
            return f"setSettings:setStream:{n}", {key: n}
        choices = self.DRIVER_INFO["commands"][command]["params"]["value"]["values"]
        if value not in choices:
            raise CommandRefusedError("Choose one of the listed values.")
        if key == "audio_source":
            return "setSettings:hdmiAudio:" + ("on" if value == "HDMI" else "off"), {
                key: value
            }
        return PREFIX[key] + value, {key: value}

    async def set_device_setting(self, key, value):
        if key not in self.DRIVER_INFO["device_settings"]:
            raise CommandRefusedError("Unknown encoder setting.")
        await self.send_command("set_" + key, {"value": value})

    async def prepare_bridge_port(self, port_id, params):
        if port_id != "serial:1":
            raise CommandRefusedError("Choose the encoder RS-232 connector.")
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
