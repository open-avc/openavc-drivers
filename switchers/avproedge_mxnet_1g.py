"""AVPro Edge MXNet 1G — AV-over-IP ecosystem controlled through the MXNet Control Box.

The CBOX is the head end: encoders (TX) and decoders (RX) never take control
connections of their own, so the CBOX is the device you add and every endpoint
appears beneath it as a child entity.

Python rather than YAML for three reasons, all of which ConfigurableDriver
cannot express today:

  1. The endpoint roster is enumerated FROM the device (`config get devicelist`),
     not declared by the integrator. YAML's `instances:` roster covers fixed and
     config-driven rosters only.
  2. Replies are JSON objects whose `info` member fans one response out into N
     children x M properties (one `config get device status ALL` reply carries the
     AV status of every endpoint). YAML response rules are regex-per-line and
     have no JSON-path routing into children.
  3. Encoders and decoders are different shapes (an encoder has an EDID and a
     channel; a decoder has five per-stream routes, a scaler and a stream gate),
     so the roster splits into two child types on a field of the reply.

Protocol
    Telnet-style ASCII on TCP 24 with no login. Every request is answered by a
    JSON object; a request is one line, and requests are serialized (the CBOX
    answers in order). Replies carry `code` (0 = OK, -1 = error with an `error`
    member) and echo `cmd`. The API document does not state a line terminator
    for either direction, so the driver frames replies by brace-balancing the
    JSON rather than trusting a delimiter, and terminates its own sends with
    CRLF (the API is documented as a PuTTY/Telnet session, where a command is
    submitted with Enter).

Push vs poll
    Poll. The API document has no subscription, notification or event section:
    routes, endpoint status and the roster are all request/response. The one
    exception is RS-232 passthrough — serial bytes arriving at an endpoint's
    serial port are emitted unsolicited on the control connection as a frame
    with an empty `cmd` and `"source":"rs232"`. That is async data on the
    established connection, not a separate push channel, so the driver parses it
    inline and lands it on the endpoint's `serial_data`.

    Three broad queries cover the whole system, so poll cost is flat in the size
    of the install: `config get devicelist` (roster + config), `config get device
    status ALL` (AV status of every endpoint) and `config get device routes vaurs
    ALLRX` (every decoder's five routes).

Source: AC-MXNET-CBOX / -B / -HA API Command List v1.26.1 (AVPro Global Holdings),
published at https://support.avproglobal.com/portal/en/kb/articles/mxnet-api
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
from typing import Any

from server.drivers.base import BaseDriver
from server.transport.frame_parsers import CallableFrameParser
from server.transport.tcp import TCPTransport
from server.utils.logger import get_logger

log = get_logger(__name__)

REQUEST_TIMEOUT_S = 6.0
ROSTER_TIMEOUT_S = 15.0
MAX_POLL_MISSES = 2
FULL_REFRESH_EVERY = 6

# `matrix aset :<code>` stream selectors, from the API doc's aset table.
STREAM_CODES = {
    "all": "z",
    "video": "v",
    "audio": "a",
    "usb": "u",
    "infrared": "r",
    "serial": "s",
}

# The per-stream de-route commands. `matrix aset` has no "unroute" form, so
# clearing a decoder means calling each stream's own disable command.
UNROUTE_COMMANDS = {
    "video": "videopathdisable",
    "audio": "audiopathdisable",
    "usb": "usbpathdisable",
    "infrared": "irpathdisable",
    "serial": "rs232pathdisable",
}

ROUTE_COMMANDS = {
    "video": "videopath",
    "audio": "audiopath",
    "usb": "usbpath",
    "infrared": "irpath",
    "serial": "rs232path",
}

# Encoder EDID presets. Indices 1-15 are the same across the 1G encoder family
# (AC-MXNET-1G-E / -AVDM-E / -EWP / -DANTE-E) per the API doc.
EDID_PRESETS = [
    ("1", "1080p, 6-channel audio"),
    ("2", "1080p 3D, 2-channel audio"),
    ("3", "1080p 3D, 6-channel audio"),
    ("4", "4K30 3D, 2-channel audio"),
    ("5", "4K30 3D, 6-channel audio"),
    ("6", "4K30 3D, 8-channel audio"),
    ("7", "1080p, 2-channel audio, HDR"),
    ("8", "1080p, 6-channel audio, HDR"),
    ("9", "1080p 3D, 2-channel audio, HDR"),
    ("10", "1080p 3D, 6-channel audio, HDR"),
    ("11", "4K30 3D, 2-channel audio, HDR"),
    ("12", "4K30 3D, 6-channel audio, HDR"),
    ("13", "4K30 3D, 8-channel audio, HDR"),
    ("14", "Copy from connected display"),
    ("15", "User EDID"),
]

# RS-232 feedback encapsulation (`config set device rs232responsetype`).
SERIAL_FORMATS = {"base64": "1", "ascii": "2", "hex": "3"}

_RE_MAC = re.compile(r"^[0-9A-Fa-f]{12}$")


def _json_frame(buf: bytes) -> tuple[bytes | None, bytes]:
    """Frame the CBOX's reply stream into whole JSON objects.

    The API document never states a reply terminator, so brace-balancing (with
    string/escape awareness) is the only framing that is correct regardless of
    whether a given firmware terminates with CRLF, LF or nothing at all.

    Per the CallableFrameParser contract, garbage before the first `{` is
    consumed by returning an EMPTY frame with the trimmed remainder — returning
    (None, trimmed) would discard the trim and wedge the stream.
    """
    start = buf.find(b"{")
    if start < 0:
        # Nothing but noise so far (a banner, stray newlines). Drop it.
        return (b"", b"") if buf else (None, buf)
    if start > 0:
        return b"", buf[start:]

    depth = 0
    in_string = False
    escape = False
    for i, byte in enumerate(buf):
        if escape:
            escape = False
            continue
        if in_string:
            if byte == 0x5C:  # backslash
                escape = True
            elif byte == 0x22:  # "
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte == 0x7B:  # {
            depth += 1
        elif byte == 0x7D:  # }
            depth -= 1
            if depth == 0:
                return buf[: i + 1], buf[i + 1 :]
    return None, buf


def _txt(value: Any) -> str:
    """Coerce a JSON member to a trimmed string ('' for absent/null)."""
    if value is None:
        return ""
    return str(value).strip()


def _flag(value: Any, token: str) -> bool:
    """MXNet reports several booleans as a token plus a digit: HDR1 / HPD0."""
    return _txt(value).upper() == f"{token}1"


def _has_signal(video: Any) -> bool:
    """A signal-less port reports an empty, 'none' or zero-by-zero timing."""
    text = _txt(video).lower()
    if not text or text in ("none", "no signal"):
        return False
    return not text.startswith("0x0")


class AVProEdgeMXNet1GDriver(BaseDriver):
    """AVPro Edge MXNet 1G ecosystem, via the AC-MXNET-CBOX control box."""

    DRIVER_INFO = {
        "id": "avproedge_mxnet_1g",
        "name": "AVPro Edge MXNet 1G",
        "manufacturer": "AVPro Edge",
        "category": "switcher",
        "version": "1.0.0",
        "min_platform_version": "0.19.4",
        "author": "OpenAVC",
        "description": (
            "Controls an AVPro Edge MXNet 1G AV-over-IP system through its MXNet "
            "Control Box (CBOX). Encoders and decoders appear as child entities with "
            "per-stream routing, EDID, scaling, CEC/IR/RS-232 passthrough, and video "
            "wall, scene, KVM and matrix preset recall."
        ),
        "source_url": "https://support.avproglobal.com/portal/en/kb/articles/mxnet-api",
        "tags": [
            "av-over-ip",
            "mxnet",
            "matrix-switcher",
            "video-wall",
            "kvm",
            "encoder",
            "decoder",
        ],
        "verified": False,
        "simulated": True,
        "protocols": ["mxnet_api"],
        "ports": [24],
        "transport": "tcp",
        "discovery": {
            "port_open": [24],
            "manufacturer_alias": ["avpro edge", "avpro global", "avproedge"],
            # The CBOX answers `config get name` with its model, e.g.
            # {"gid":200000,"cmd":"config get name","info":"AC-MXNET-CBOX","code":0}
            "tcp_probe": {
                "port": 24,
                "send_ascii": "config get name\r\n",
                "expect_regex": r'"info"\s*:\s*"AC-MXNET[^"]*"',
                "extract_manufacturer": "AVPro Edge",
                "extract": {
                    "model": {"regex": r'"info"\s*:\s*"(AC-MXNET[^"]*)"'},
                },
                "timeout_ms": 2000,
            },
        },
        "compatible_models": [
            {
                "manufacturer": "AVPro Edge",
                "models": [
                    "AC-MXNET-CBOX",
                    "AC-MXNET-CBOX-B",
                    "AC-MXNET-CBOX-HA",
                ],
                "confidence": "untested",
                "notes": (
                    "Add the CONTROL BOX as the device — the encoders and decoders it "
                    "manages appear as children. Endpoints covered: AC-MXNET-1G-E / -EV2 / "
                    "-EWP / -AVDM-E / -DANTE-E encoders and AC-MXNET-1G-D / -DV2 / -AVDM-D / "
                    "-DANTE-D decoders. The CBOX-HA's high-availability pairing and the "
                    "MV41V2 multiview companion are configured in MENTOR, not here."
                ),
            },
        ],
        "help": {
            "overview": (
                "The MXNet Control Box is the head end of an MXNet 1G system. Add the CBOX "
                "(not the individual encoders/decoders) and every endpoint it knows about "
                "shows up as a child you can route, scale and pass CEC/IR/RS-232 through.\n\n"
                "Routing works per stream: video, audio, USB, IR and RS-232 can each follow a "
                "different source, or move together with the Route command's Media setting of "
                "'All'. Video walls, KVM layouts, scenes and matrix presets built in MENTOR are "
                "recalled by name."
            ),
            "setup": (
                "1. Connect to the CBOX's MENTOR/LAN port (the port used for the web GUI and "
                "third-party control) — not the AV network port.\n"
                "2. Enter that port's IP address. The API listens on TCP 24 and needs no login.\n"
                "3. Commission the system in MENTOR first (endpoints adopted, names assigned). "
                "The driver reads the roster from the CBOX; it does not adopt endpoints.\n"
                "4. Endpoints are listed by their MENTOR custom name. Renaming one in MENTOR "
                "changes its label here on the next poll; its identity is its MAC address.\n"
                "5. RS-232 passthrough requires the endpoint to be in RS-232 mode 2 (set in "
                "MENTOR). On connect the driver normalizes the serial feedback encapsulation of "
                "every endpoint to the format chosen in Serial Feedback Format, so inbound serial "
                "data can be decoded onto each endpoint's Serial Data state."
            ),
        },
        "default_config": {
            "host": "",
            "port": 24,
            "poll_interval": 10,
            "serial_feedback_format": "ascii",
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
                "description": "IP address of the CBOX's MENTOR/LAN port.",
            },
            "port": {
                "type": "integer",
                "required": True,
                "default": 24,
                "label": "Port",
                "description": "MXNet API port. Fixed at 24 on all CBOX models.",
            },
            "poll_interval": {
                "type": "integer",
                "default": 10,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "Refreshes the endpoint roster, every decoder's routes, and each "
                    "endpoint's AV status. The MXNet API has no notifications, so this is "
                    "the only source of state. 0 disables polling."
                ),
            },
            "serial_feedback_format": {
                "type": "enum",
                "default": "ascii",
                "values": ["ascii", "hex", "base64"],
                "label": "Serial Feedback Format",
                "description": (
                    "Encapsulation the CBOX uses for RS-232 data coming back from an "
                    "endpoint. Applied to all endpoints on connect so inbound serial can be "
                    "decoded. The device's own default is Base64."
                ),
            },
        },
        "state_variables": {
            "connected": {"type": "boolean", "label": "Connected"},
            "model": {"type": "string", "label": "Model", "cloud_priority": "low"},
            "firmware": {"type": "string", "label": "Firmware", "cloud_priority": "low"},
            "av_ip": {
                "type": "string",
                "label": "AV Network IP",
                "help": "Address of the port that manages the MXNet endpoints.",
                "cloud_priority": "low",
            },
            "lan_ip": {
                "type": "string",
                "label": "Control Network IP",
                "help": "Address of the MENTOR/LAN port this driver talks to.",
                "cloud_priority": "low",
            },
            "encoder_count": {
                "type": "integer",
                "label": "Encoders",
                "cloud_priority": "low",
            },
            "decoder_count": {
                "type": "integer",
                "label": "Decoders",
                "cloud_priority": "low",
            },
            "offline_endpoints": {
                "type": "integer",
                "label": "Endpoints Offline",
                "help": "Endpoints in the CBOX database that are not currently reachable.",
                "cloud_priority": "high",
            },
            "previews": {
                "type": "boolean",
                "label": "MENTOR Previews",
                "help": "Preview images and image caching for MENTOR's Auto-Matrix tab.",
                "cloud_priority": "low",
            },
            "rs232_alias": {
                "type": "boolean",
                "label": "CBOX Serial Port",
                "help": "Whether the CBOX's own RS-232 port accepts API commands.",
                "cloud_priority": "low",
            },
            "timezone": {"type": "string", "label": "Timezone", "cloud_priority": "low"},
            "ntp_servers": {
                "type": "string",
                "label": "NTP Servers",
                "cloud_priority": "low",
            },
            "encoder_options": {
                "type": "string",
                "label": "Encoder Options",
                "help": "JSON list of encoders — feeds the source dropdowns.",
                "cloud_priority": "low",
            },
            "decoder_options": {
                "type": "string",
                "label": "Decoder Options",
                "help": "JSON list of decoders — feeds the display dropdowns.",
                "cloud_priority": "low",
            },
            "endpoint_options": {
                "type": "string",
                "label": "Endpoint Options",
                "help": "JSON list of every endpoint — feeds the endpoint dropdowns.",
                "cloud_priority": "low",
            },
            "preset_options": {
                "type": "string",
                "label": "Matrix Preset Options",
                "cloud_priority": "low",
            },
            "scene_options": {"type": "string", "label": "Scene Options", "cloud_priority": "low"},
            "videowall_options": {
                "type": "string",
                "label": "Video Wall Options",
                "cloud_priority": "low",
            },
            "kvm_options": {"type": "string", "label": "KVM Options", "cloud_priority": "low"},
        },
        "child_entity_types": {
            "encoder": {
                "label": "Encoder",
                "label_plural": "Encoders",
                "id_format": {"type": "string", "max_length": 12},
                "state_variables": {
                    "name": {"type": "string", "label": "Name", "cloud_priority": "low"},
                    "online": {
                        "type": "boolean",
                        "label": "Online",
                        "help": "Endpoint is reachable on the AV network.",
                        "cloud_priority": "high",
                    },
                    "signal_present": {
                        "type": "boolean",
                        "label": "Signal",
                        "help": "A source is connected and sending a valid signal.",
                        "cloud_priority": "high",
                    },
                    "resolution": {
                        "type": "string",
                        "label": "Input Resolution",
                        "cloud_priority": "high",
                    },
                    "source_name": {
                        "type": "string",
                        "label": "Connected Source",
                        "help": "Name the attached source reports over HDMI.",
                        "cloud_priority": "low",
                    },
                    "audio_format": {
                        "type": "string",
                        "label": "Audio Format",
                        "cloud_priority": "low",
                    },
                    "hdcp": {"type": "string", "label": "HDCP", "cloud_priority": "low"},
                    "hdr": {"type": "boolean", "label": "HDR", "cloud_priority": "low"},
                    "chroma": {"type": "string", "label": "Chroma", "cloud_priority": "low"},
                    "color_depth": {
                        "type": "string",
                        "label": "Color Depth",
                        "cloud_priority": "low",
                    },
                    "edid": {
                        "type": "string",
                        "label": "EDID",
                        "help": "Active EDID preset presented to the source.",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "channel": {
                        "type": "string",
                        "label": "Stream Channel",
                        "help": "Multicast channel decoders subscribe to for this encoder.",
                        "cloud_priority": "low",
                    },
                    "audio_volume": {
                        "type": "integer",
                        "label": "Analog Audio Volume",
                        "help": "Analog (extracted) audio output level on the encoder.",
                        "min": 0,
                        "max": 100,
                        "step": 1,
                        "unit": "%",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "serial_data": {
                        "type": "string",
                        "label": "Serial Data",
                        "help": "Most recent RS-232 data received on this endpoint's serial port.",
                        "cloud_priority": "low",
                    },
                    "mac": {"type": "string", "label": "MAC", "cloud_priority": "low"},
                    "ip": {"type": "string", "label": "IP Address", "cloud_priority": "low"},
                    "model": {"type": "string", "label": "Model", "cloud_priority": "low"},
                    "firmware": {"type": "string", "label": "Firmware", "cloud_priority": "low"},
                    "link_speed": {
                        "type": "string",
                        "label": "Link Speed",
                        "cloud_priority": "low",
                    },
                    "switch_ip": {
                        "type": "string",
                        "label": "Network Switch",
                        "help": "Switch this endpoint is patched into, as reported by the CBOX.",
                        "cloud_priority": "low",
                    },
                    "switch_port": {
                        "type": "string",
                        "label": "Switch Port",
                        "cloud_priority": "low",
                    },
                },
                "summary_fields": ["online", "signal_present", "resolution"],
                "label_field": "name",
            },
            "decoder": {
                "label": "Decoder",
                "label_plural": "Decoders",
                "id_format": {"type": "string", "max_length": 12},
                "state_variables": {
                    "name": {"type": "string", "label": "Name", "cloud_priority": "low"},
                    "online": {
                        "type": "boolean",
                        "label": "Online",
                        "help": "Endpoint is reachable on the AV network.",
                        "cloud_priority": "high",
                    },
                    "source_video": {
                        "type": "string",
                        "label": "Video Source",
                        "help": "Encoder whose video this decoder is showing; empty when unrouted.",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "source_audio": {
                        "type": "string",
                        "label": "Audio Source",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "source_usb": {
                        "type": "string",
                        "label": "USB Source",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "source_infrared": {
                        "type": "string",
                        "label": "IR Source",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "source_serial": {
                        "type": "string",
                        "label": "Serial Source",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "stream": {
                        "type": "boolean",
                        "label": "Output Stream",
                        "help": "Decoder's HDMI output is live. Off also drops the output's 5V.",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "blackout": {
                        "type": "boolean",
                        "label": "Blackout",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "display_name": {
                        "type": "string",
                        "label": "Connected Display",
                        "help": "Name the attached display reports over HDMI.",
                        "cloud_priority": "low",
                    },
                    "display_connected": {
                        "type": "boolean",
                        "label": "Display Connected",
                        "help": "Hot-plug detect from the attached display.",
                        "cloud_priority": "high",
                    },
                    "resolution": {
                        "type": "string",
                        "label": "Output Resolution",
                        "cloud_priority": "high",
                    },
                    "audio_format": {
                        "type": "string",
                        "label": "Audio Format",
                        "cloud_priority": "low",
                    },
                    "hdcp": {"type": "string", "label": "HDCP", "cloud_priority": "low"},
                    "hdr": {"type": "boolean", "label": "HDR", "cloud_priority": "low"},
                    "chroma": {"type": "string", "label": "Chroma", "cloud_priority": "low"},
                    "color_depth": {
                        "type": "string",
                        "label": "Color Depth",
                        "cloud_priority": "low",
                    },
                    "rotate": {
                        "type": "string",
                        "label": "Rotation",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "stretch": {
                        "type": "string",
                        "label": "Scaling",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "test_pattern": {
                        "type": "string",
                        "label": "Test Pattern",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "serial_data": {
                        "type": "string",
                        "label": "Serial Data",
                        "help": "Most recent RS-232 data received on this endpoint's serial port.",
                        "cloud_priority": "low",
                    },
                    "mac": {"type": "string", "label": "MAC", "cloud_priority": "low"},
                    "ip": {"type": "string", "label": "IP Address", "cloud_priority": "low"},
                    "model": {"type": "string", "label": "Model", "cloud_priority": "low"},
                    "firmware": {"type": "string", "label": "Firmware", "cloud_priority": "low"},
                    "link_speed": {
                        "type": "string",
                        "label": "Link Speed",
                        "cloud_priority": "low",
                    },
                    "switch_ip": {
                        "type": "string",
                        "label": "Network Switch",
                        "help": "Switch this endpoint is patched into, as reported by the CBOX.",
                        "cloud_priority": "low",
                    },
                    "switch_port": {
                        "type": "string",
                        "label": "Switch Port",
                        "cloud_priority": "low",
                    },
                },
                "summary_fields": ["online", "source_video", "display_connected"],
                "label_field": "name",
            },
        },
        "device_settings": {
            "previews": {
                "type": "boolean",
                "label": "MENTOR Previews",
                "help": (
                    "Preview images and image caching for MENTOR's Auto-Matrix tab. "
                    "Requires CBOX firmware 2.34 or later."
                ),
                "state_key": "previews",
                "default": True,
                "setup": False,
            },
            "rs232_alias": {
                "type": "boolean",
                "label": "CBOX Serial Port",
                "help": "Let the CBOX's own RS-232 port accept API commands.",
                "state_key": "rs232_alias",
                "default": True,
                "setup": False,
            },
            "timezone": {
                "type": "string",
                "label": "Timezone",
                "help": "UTC offset, e.g. UTC-5. Range UTC-12 to UTC+12.",
                "state_key": "timezone",
                "default": "UTC+0",
                "setup": False,
            },
            "ntp_servers": {
                "type": "string",
                "label": "NTP Servers",
                "help": "Up to five NTP servers, separated by spaces.",
                "state_key": "ntp_servers",
                "default": "",
                "setup": False,
            },
        },
        "quick_actions": ["route", "route_off", "recall_preset", "refresh"],
        "actions": [
            {"id": "route", "kind": "command", "icon": "route"},
            {"id": "route_off", "kind": "command", "icon": "circle-off"},
            {"id": "recall_preset", "kind": "command", "icon": "bookmark"},
            {"id": "recall_videowall", "kind": "command", "icon": "layout-grid"},
            {"id": "identify", "kind": "command", "icon": "lightbulb"},
            {"id": "refresh", "kind": "command", "icon": "refresh-cw"},
            {"id": "reboot_cbox", "kind": "command", "icon": "power", "confirm": True},
            {
                "id": "test_connection",
                "kind": "setup",
                "label": "Test Connection",
                "icon": "plug-zap",
                "availability": "always",
            },
        ],
        "commands": {
            # ── Routing ────────────────────────────────────────────────
            "route": {
                "label": "Route Source to Display",
                "params": {
                    "tx": {
                        "type": "child_id",
                        "child_type": "encoder",
                        "required": True,
                        "label": "Source (Encoder)",
                    },
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                    "stream": {
                        "type": "enum",
                        "required": False,
                        "label": "Media",
                        "default": "all",
                        "values": ["all", "video", "audio", "usb", "infrared", "serial"],
                        "labels": [
                            "All",
                            "Video",
                            "Audio",
                            "USB",
                            "Infrared",
                            "RS-232",
                        ],
                        "help": "Route one stream on its own, or all of them together.",
                    },
                },
                "help": "Subscribe a decoder to an encoder's stream. Takes effect immediately.",
            },
            "route_off": {
                "label": "Clear Route",
                "params": {
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                    "stream": {
                        "type": "enum",
                        "required": False,
                        "label": "Media",
                        "default": "all",
                        "values": ["all", "video", "audio", "usb", "infrared", "serial"],
                        "labels": ["All", "Video", "Audio", "USB", "Infrared", "RS-232"],
                    },
                },
                "help": "Drop a decoder's incoming stream subscription.",
            },
            "recall_preset": {
                "label": "Recall Matrix Preset",
                "params": {
                    "name": {
                        "type": "string",
                        "required": True,
                        "label": "Preset",
                        "options_state": "preset_options",
                    },
                    "force": {
                        "type": "boolean",
                        "required": False,
                        "label": "Force Resubscribe",
                        "default": False,
                        "help": "Re-apply every route even where the CBOX thinks it already matches.",
                    },
                },
                "help": "Apply a matrix preset saved in MENTOR.",
            },
            "recall_scene": {
                "label": "Recall Scene",
                "params": {
                    "name": {
                        "type": "string",
                        "required": True,
                        "label": "Scene",
                        "options_state": "scene_options",
                    },
                    "force": {
                        "type": "boolean",
                        "required": False,
                        "label": "Force Resubscribe",
                        "default": False,
                    },
                },
                "help": "Apply a scene (a saved combination of video walls and matrices).",
            },
            "recall_videowall": {
                "label": "Recall Video Wall",
                "params": {
                    "name": {
                        "type": "string",
                        "required": True,
                        "label": "Video Wall",
                        "options_state": "videowall_options",
                    },
                    "force": {
                        "type": "boolean",
                        "required": False,
                        "label": "Force Resubscribe",
                        "default": False,
                    },
                },
                "help": "Activate a video wall built in MENTOR.",
            },
            "recall_kvm": {
                "label": "Recall KVM Layout",
                "params": {
                    "name": {
                        "type": "string",
                        "required": True,
                        "label": "KVM Layout",
                        "options_state": "kvm_options",
                    },
                },
                "help": "Activate a KVM layout built in MENTOR.",
            },
            # ── Decoder (display side) ────────────────────────────────
            "set_stream": {
                "label": "Display Output",
                "params": {
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                    "state": {
                        "type": "enum",
                        "required": True,
                        "label": "Output",
                        "values": ["on", "off"],
                        "labels": ["On", "Off"],
                    },
                },
                "help": (
                    "Turn the decoder's HDMI output on or off. Off also drops the output's 5V, "
                    "which stops CEC reaching the display — use Blackout to keep the link alive."
                ),
            },
            "set_blackout": {
                "label": "Blackout",
                "params": {
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                    "state": {
                        "type": "enum",
                        "required": True,
                        "label": "Blackout",
                        "values": ["on", "off"],
                        "labels": ["On", "Off"],
                    },
                },
                "help": "Blank the decoder's output while keeping the HDMI link up.",
            },
            "set_volume": {
                "label": "Set Display Analog Volume",
                "params": {
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                    "level": {
                        "type": "integer",
                        "required": True,
                        "label": "Volume",
                        "min": -1,
                        "max": 100,
                        "unit": "%",
                        "help": "0-100 percent. -1 hands volume back to the decoder's own tracking.",
                    },
                },
                "help": (
                    "Analog audio output level on a decoder. The MXNet API has no matching query, "
                    "so this value is not mirrored back into state."
                ),
            },
            "set_audio_delay": {
                "label": "Set Audio Mute Time",
                "params": {
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                    "delay": {
                        "type": "integer",
                        "required": True,
                        "label": "Mute Time",
                        "min": 0,
                        "max": 10000,
                        "unit": "ms",
                        "default": 2000,
                    },
                },
                "help": (
                    "How long the decoder mutes its HDMI audio while the format changes. Raise it "
                    "if a display pops or crackles on a format switch. Default 2000 ms."
                ),
            },
            "set_rotate": {
                "label": "Rotate Image",
                "params": {
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                    "rotation": {
                        "type": "enum",
                        "required": True,
                        "label": "Rotation",
                        "values": ["0", "5", "3", "6"],
                        "labels": ["None", "90 degrees", "180 degrees", "270 degrees"],
                    },
                },
                "help": "Rotate a decoder's output image.",
            },
            "set_stretch": {
                "label": "Set Scaling",
                "params": {
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "label": "Scaling",
                        "values": ["1", "2"],
                        "labels": ["Stretch to fit", "Keep aspect ratio"],
                    },
                },
            },
            "set_test_pattern": {
                "label": "Test Pattern",
                "params": {
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                    "pattern": {
                        "type": "enum",
                        "required": True,
                        "label": "Pattern",
                        "values": ["0", "1", "2"],
                        "labels": ["Off", "Pattern 1", "Pattern 2"],
                    },
                },
                "help": "Show a built-in test pattern on the display — useful when commissioning.",
            },
            "set_hdcp": {
                "label": "Set HDCP Mode",
                "params": {
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "label": "HDCP",
                        "values": ["2", "3", "4"],
                        "labels": ["Off", "HDCP 1.4", "HDCP 2.2"],
                    },
                },
                "help": "Force the HDCP version the decoder presents to the display.",
            },
            "set_osd": {
                "label": "On-Screen Display",
                "params": {
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                    "state": {
                        "type": "enum",
                        "required": True,
                        "label": "OSD",
                        "values": ["on", "off"],
                        "labels": ["On", "Off"],
                    },
                },
                "help": "Show or hide the decoder's on-screen overlay (source name, status).",
            },
            # ── Encoder (source side) ─────────────────────────────────
            "set_edid": {
                "label": "Set Encoder EDID",
                "params": {
                    "tx": {
                        "type": "child_id",
                        "child_type": "encoder",
                        "required": True,
                        "label": "Source (Encoder)",
                    },
                    "edid": {
                        "type": "enum",
                        "required": True,
                        "label": "EDID",
                        "values": [value for value, _label in EDID_PRESETS],
                        "labels": [label for _value, label in EDID_PRESETS],
                    },
                },
                "help": (
                    "Choose the EDID the encoder presents to its source. The encoder must be in "
                    "RS-232 mode 2 for EDID selection to work."
                ),
            },
            "copy_edid": {
                "label": "Copy Display EDID to Encoder",
                "params": {
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Copy From (Decoder)",
                    },
                    "tx": {
                        "type": "child_id",
                        "child_type": "encoder",
                        "required": True,
                        "label": "Copy To (Encoder)",
                    },
                },
                "help": "Read the EDID of the display on a decoder and present it on an encoder.",
            },
            "set_encoder_volume": {
                "label": "Set Encoder Analog Volume",
                "params": {
                    "tx": {
                        "type": "child_id",
                        "child_type": "encoder",
                        "required": True,
                        "label": "Source (Encoder)",
                    },
                    "level": {
                        "type": "integer",
                        "required": True,
                        "label": "Volume",
                        "min": 0,
                        "max": 100,
                        "unit": "%",
                    },
                },
                "help": "Analog (extracted) audio output level on an encoder.",
            },
            "set_audio_input": {
                "label": "Set Encoder Audio Input",
                "params": {
                    "tx": {
                        "type": "child_id",
                        "child_type": "encoder",
                        "required": True,
                        "label": "Source (Encoder)",
                    },
                    "source": {
                        "type": "enum",
                        "required": True,
                        "label": "Audio Input",
                        "values": ["hdmi", "analog", "auto"],
                        "labels": ["HDMI", "Analog", "Auto"],
                    },
                },
                "help": "Pick which audio input the encoder embeds into its stream.",
            },
            # ── Any endpoint ──────────────────────────────────────────
            "identify": {
                "label": "Identify Endpoint",
                "params": {
                    "endpoint": {
                        "type": "string",
                        "required": True,
                        "label": "Endpoint",
                        "options_state": "endpoint_options",
                    },
                    "mode": {
                        "type": "enum",
                        "required": False,
                        "label": "LED",
                        "default": "flash",
                        "values": ["flash", "on", "off"],
                        "labels": ["Flash", "On", "Off"],
                    },
                },
                "help": "Flash an endpoint's front LED so you can find it in the rack.",
            },
            "reboot_endpoint": {
                "label": "Reboot Endpoint",
                "params": {
                    "endpoint": {
                        "type": "string",
                        "required": True,
                        "label": "Endpoint",
                        "options_state": "endpoint_options",
                    },
                },
                "help": "Reboot a single encoder or decoder.",
            },
            "rename_endpoint": {
                "label": "Rename Endpoint",
                "params": {
                    "endpoint": {
                        "type": "string",
                        "required": True,
                        "label": "Endpoint",
                        "options_state": "endpoint_options",
                    },
                    "name": {
                        "type": "string",
                        "required": True,
                        "label": "New Name",
                        "pattern": "^[A-Za-z0-9_-]{1,32}$",
                    },
                },
                "help": "Change an endpoint's custom name. The name is also its MENTOR label.",
            },
            "hpd_reset": {
                "label": "Reset Hot Plug",
                "params": {
                    "endpoint": {
                        "type": "string",
                        "required": True,
                        "label": "Endpoint",
                        "options_state": "endpoint_options",
                    },
                },
                "help": "Re-assert hot-plug detect — the usual fix for a source or display that "
                "handshakes but shows nothing.",
            },
            "send_cec": {
                "label": "Send CEC",
                "params": {
                    "endpoint": {
                        "type": "string",
                        "required": True,
                        "label": "Endpoint",
                        "options_state": "endpoint_options",
                    },
                    "data": {
                        "type": "string",
                        "required": True,
                        "label": "CEC Bytes (hex)",
                        "pattern": "^[0-9A-Fa-f]{2,}(,[0-9A-Fa-f]{2,})*$",
                        "help": "Hex bytes, e.g. 40 04 to wake a display. Comma-separate several "
                        "messages to send them in order.",
                    },
                },
                "help": "Send a CEC message out of an endpoint's HDMI port.",
            },
            "send_ir": {
                "label": "Send IR",
                "params": {
                    "endpoint": {
                        "type": "string",
                        "required": True,
                        "label": "Endpoint",
                        "options_state": "endpoint_options",
                    },
                    "code": {
                        "type": "string",
                        "required": True,
                        "label": "IR Code",
                        "trim": False,
                        "help": "Pronto hex or Global Cache format, exactly as the code set gives it.",
                    },
                },
                "help": "Send an IR code out of an endpoint's IR emitter port.",
            },
            "send_serial": {
                "label": "Send Serial",
                "params": {
                    "endpoint": {
                        "type": "string",
                        "required": True,
                        "label": "Endpoint",
                        "options_state": "endpoint_options",
                    },
                    "data": {
                        "type": "string",
                        "required": True,
                        "label": "Data",
                        "trim": False,
                        "help": "ASCII text, or space-separated hex bytes when Format is Hex.",
                    },
                    "format": {
                        "type": "enum",
                        "required": False,
                        "label": "Format",
                        "default": "ascii",
                        "values": ["ascii", "hex"],
                        "labels": ["ASCII", "Hex"],
                    },
                    "append_cr": {
                        "type": "boolean",
                        "required": False,
                        "label": "Append CR",
                        "default": False,
                        "help": "Add a carriage return — most serial devices need one.",
                    },
                },
                "help": (
                    "Send RS-232 data out of an endpoint's serial port. The endpoint must be in "
                    "RS-232 mode 2. Replies land on that endpoint's Serial Data state."
                ),
            },
            "set_serial_settings": {
                "label": "Set Serial Port Settings",
                "params": {
                    "endpoint": {
                        "type": "string",
                        "required": True,
                        "label": "Endpoint",
                        "options_state": "endpoint_options",
                    },
                    "baud": {
                        "type": "enum",
                        "required": True,
                        "label": "Baud Rate",
                        "default": "9600",
                        "values": [
                            "300",
                            "600",
                            "1200",
                            "2400",
                            "4800",
                            "9600",
                            "19200",
                            "38400",
                            "57600",
                            "115200",
                        ],
                    },
                    "data_bits": {
                        "type": "enum",
                        "required": False,
                        "label": "Data Bits",
                        "default": "8",
                        "values": ["7", "8"],
                    },
                    "parity": {
                        "type": "enum",
                        "required": False,
                        "label": "Parity",
                        "default": "0",
                        "values": ["0", "1", "2"],
                        "labels": ["None", "Even", "Odd"],
                    },
                },
                "help": "Configure an endpoint's RS-232 port. Stop bits (1) and flow control "
                "(none) are fixed by the hardware.",
            },
            "set_serial_feedback": {
                "label": "Set Serial Feedback Format",
                "params": {
                    "endpoint": {
                        "type": "string",
                        "required": True,
                        "label": "Endpoint",
                        "options_state": "endpoint_options",
                    },
                    "format": {
                        "type": "enum",
                        "required": True,
                        "label": "Format",
                        "values": ["ascii", "hex", "base64"],
                        "labels": ["ASCII", "Hex", "Base64"],
                    },
                },
                "help": (
                    "Encapsulation the CBOX uses for serial data coming back from this endpoint. "
                    "Changing it away from the driver's configured format stops Serial Data from "
                    "being decoded correctly."
                ),
            },
            # ── System ────────────────────────────────────────────────
            "reboot_cbox": {
                "label": "Reboot Control Box",
                "params": {},
                "help": "Reboot the CBOX. The MXNet system keeps passing video while it restarts; "
                "control is unavailable for about a minute.",
            },
            "refresh": {
                "label": "Refresh",
                "params": {},
                "help": "Re-read the endpoint roster, routes and status now.",
            },
            "raw_command": {
                "label": "Raw API Command",
                "params": {
                    "command": {
                        "type": "string",
                        "required": True,
                        "label": "Command",
                        "trim": False,
                        "help": "A raw MXNet API line, e.g. 'config get devicelist'.",
                    },
                },
                "help": "Send any MXNet API command and return the CBOX's reply. For "
                "commissioning and troubleshooting.",
            },
        },
    }

    def __init__(self, device_id: str, config: dict, state: Any, events: Any) -> None:
        # Roster bookkeeping. Children are keyed by MAC (stable and unique);
        # the CBOX's routes reply keys by custom name, so we keep both maps.
        self._known: dict[str, set[str]] = {"encoder": set(), "decoder": set()}
        self._mac_by_name: dict[str, str] = {}
        self._type_by_mac: dict[str, str] = {}
        self._name_by_mac: dict[str, str] = {}

        self._request_lock = asyncio.Lock()
        self._pending: asyncio.Future | None = None
        self._poll_cycle = 0
        self._poll_misses = 0
        super().__init__(device_id, config, state, events)

    # ── Transport ────────────────────────────────────────────────────

    def _create_frame_parser(self) -> CallableFrameParser:
        return CallableFrameParser(_json_frame)

    async def connect(self) -> None:
        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", 24))
        if not host:
            raise ValueError("No IP address configured")

        self._poll_cycle = 0
        self._poll_misses = 0
        self._pending = None

        self.transport = await TCPTransport.create(
            host=host,
            port=port,
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            frame_parser=self._create_frame_parser(),
            timeout=5.0,
            name=self.device_id,
        )

        try:
            doc = await self._request("config get name")
            if doc is None:
                raise ConnectionError(
                    f"[{self.device_id}] No answer from the MXNet API on {host}:{port} — "
                    f"check that this is the CBOX's MENTOR/LAN port"
                )
            self.set_state("model", _txt(doc.get("info")))
            await self._enumerate_roster()
            await self._normalize_serial_feedback()
        except Exception:
            await self._teardown_transport()
            raise

        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        log.info(f"[{self.device_id}] Connected to MXNet CBOX at {host}:{port}")

        await self.poll()
        poll_interval = int(self.config.get("poll_interval", 10))
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def _request(self, line: str, timeout: float | None = None) -> dict[str, Any] | None:
        """Send one API line and await its JSON reply.

        Requests are serialized: the CBOX answers in order, and its `cmd` echo is
        unreliable for correlation (the published examples show the echo carrying
        a different endpoint name than the request), so the reply is matched by
        order, not by echo. Returns the parsed object, or None on timeout.
        """
        if timeout is None:
            timeout = REQUEST_TIMEOUT_S
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        async with self._request_lock:
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending = fut
            try:
                await self.transport.send((line + "\r\n").encode("utf-8"))
                try:
                    return await asyncio.wait_for(fut, timeout)
                except asyncio.TimeoutError:
                    log.warning(f"[{self.device_id}] No reply to: {line}")
                    return None
            finally:
                if self._pending is fut:
                    self._pending = None

    async def _write(self, line: str) -> bool:
        """Send a write command and report whether the CBOX accepted it."""
        doc = await self._request(line)
        if doc is None:
            raise ConnectionError(f"[{self.device_id}] No reply to: {line}")
        if int(doc.get("code", 0)) != 0:
            detail = _txt(doc.get("error")) or _txt(doc.get("info")) or "rejected"
            raise ValueError(f"The CBOX rejected '{line}': {detail}")
        return True

    async def on_data_received(self, data: bytes) -> None:
        # The frame parser emits an empty frame when it discards inter-object
        # noise (see _json_frame); there is nothing to parse in one.
        if not data.strip():
            return
        try:
            doc = json.loads(data.decode("utf-8", errors="replace"))
        except (ValueError, TypeError) as exc:
            log.warning(f"[{self.device_id}] Unparseable reply: {exc}")
            return
        if not isinstance(doc, dict):
            return

        # RS-232 passthrough data arrives unsolicited, with an empty `cmd`.
        if _txt(doc.get("source")) == "rs232" and not _txt(doc.get("cmd")):
            self._apply_serial(doc)
            return

        pending = self._pending
        if pending is not None and not pending.done():
            pending.set_result(doc)

    # ── Roster ───────────────────────────────────────────────────────

    async def _enumerate_roster(self) -> None:
        """Read the CBOX's device database and reconcile the child roster."""
        doc = await self._request("config get devicelist", timeout=ROSTER_TIMEOUT_S)
        if doc is None:
            raise ConnectionError(
                f"[{self.device_id}] The CBOX did not answer the endpoint roster query"
            )
        info = doc.get("info")
        if not isinstance(info, dict):
            raise ConnectionError(
                f"[{self.device_id}] The CBOX returned no endpoint list — commission the "
                f"system in MENTOR before adding it here"
            )
        self._apply_devicelist(info)

    def _apply_devicelist(self, info: dict[str, Any]) -> None:
        found: dict[str, set[str]] = {"encoder": set(), "decoder": set()}
        updates: list[tuple[str, str, dict[str, Any]]] = []
        offline = 0

        for key, entry in info.items():
            if not isinstance(entry, dict):
                continue
            mac = _txt(entry.get("mac")) or _txt(key)
            if not _RE_MAC.match(mac):
                log.debug(f"[{self.device_id}] Skipping roster entry with no MAC: {key}")
                continue
            mac = mac.upper()

            # An encoder is the host of its own stream channel; a decoder
            # subscribes to one. `is_host` is the CBOX's own discriminator.
            ctype = "encoder" if str(entry.get("is_host", "")) == "1" else "decoder"
            name = _txt(entry.get("id")) or mac
            # `online` is a heartbeat timestamp; `state` is the service state.
            online = _txt(entry.get("state")).lower() == "s_srv_on"
            if not online:
                offline += 1

            found[ctype].add(mac)
            self._type_by_mac[mac] = ctype
            self._name_by_mac[mac] = name
            self._mac_by_name[name.lower()] = mac

            common: dict[str, Any] = {
                "name": name,
                "online": online,
                "mac": mac,
                "ip": _txt(entry.get("ip")),
                "model": _txt(entry.get("dtype")),
                "firmware": _txt(entry.get("version")),
            }
            if ctype == "encoder":
                common["channel"] = _txt(entry.get("ch"))
                if "edid" in entry:
                    common["edid"] = _txt(entry.get("edid"))
                if "exaudiovolume" in entry:
                    common["audio_volume"] = self._as_int(entry.get("exaudiovolume"), 0, 100)
            else:
                if "stream" in entry:
                    common["stream"] = _txt(entry.get("stream")).lower() == "on"
                if "blackout" in entry:
                    common["blackout"] = _txt(entry.get("blackout")).lower() == "on"
                if "rotate" in entry:
                    common["rotate"] = _txt(entry.get("rotate"))
                if "stretch" in entry:
                    common["stretch"] = _txt(entry.get("stretch"))
                if "pattern" in entry:
                    common["test_pattern"] = _txt(entry.get("pattern"))

            if mac not in self._known[ctype]:
                self.register_child(ctype, mac, initial_state={"name": name, "mac": mac})
                self._known[ctype].add(mac)
            updates.append((ctype, mac, common))

        if updates:
            self.set_children_state_batch(updates)

        # Endpoints removed from the CBOX database disappear entirely. An
        # endpoint that is merely unplugged stays in the list with state != on,
        # so it keeps its child and just goes online=False.
        for ctype in ("encoder", "decoder"):
            for mac in list(self._known[ctype] - found[ctype]):
                self.deregister_child(ctype, mac)
                self._known[ctype].discard(mac)
                self._type_by_mac.pop(mac, None)
                name = self._name_by_mac.pop(mac, "")
                self._mac_by_name.pop(name.lower(), None)

        self.set_states(
            {
                "encoder_count": len(found["encoder"]),
                "decoder_count": len(found["decoder"]),
                "offline_endpoints": offline,
            }
        )
        self._publish_endpoint_options()

    def _publish_endpoint_options(self) -> None:
        encoders = [
            {"value": mac, "label": self._name_by_mac.get(mac, mac)}
            for mac in sorted(self._known["encoder"], key=lambda m: self._name_by_mac.get(m, m))
        ]
        decoders = [
            {"value": mac, "label": self._name_by_mac.get(mac, mac)}
            for mac in sorted(self._known["decoder"], key=lambda m: self._name_by_mac.get(m, m))
        ]
        everything = [{**opt, "label": f"{opt['label']} (Encoder)"} for opt in encoders] + [
            {**opt, "label": f"{opt['label']} (Decoder)"} for opt in decoders
        ]
        self.set_states(
            {
                "encoder_options": json.dumps(encoders),
                "decoder_options": json.dumps(decoders),
                "endpoint_options": json.dumps(everything),
            }
        )

    async def refresh_children(self) -> dict[str, Any]:
        await self._enumerate_roster()
        self._poll_cycle = 0
        await self.poll()
        return {
            "encoders": len(self._known["encoder"]),
            "decoders": len(self._known["decoder"]),
        }

    # ── Response fan-out ─────────────────────────────────────────────

    def _apply_status(self, info: dict[str, Any]) -> None:
        """`config get device status ALL` — one reply, every endpoint's AV status."""
        updates: list[tuple[str, str, dict[str, Any]]] = []
        for key, entry in info.items():
            if not isinstance(entry, dict):
                continue
            mac = self._resolve(key)
            if mac is None:
                continue
            ctype = self._type_by_mac[mac]

            common: dict[str, Any] = {
                "resolution": _txt(entry.get("video")),
                "audio_format": _txt(entry.get("audio")),
                "hdcp": _txt(entry.get("hdcp")),
                "hdr": _flag(entry.get("hdr"), "HDR"),
                "chroma": _txt(entry.get("chroma")),
                "color_depth": _txt(entry.get("colordepth")),
                "link_speed": _txt(entry.get("speed")),
                "switch_ip": _txt(entry.get("switchip")),
                "switch_port": _txt(entry.get("switchport")),
            }
            if ctype == "encoder":
                common["signal_present"] = _has_signal(entry.get("video"))
                common["source_name"] = _txt(entry.get("connectedname"))
            else:
                common["display_connected"] = _flag(entry.get("hpd"), "HPD")
                common["display_name"] = _txt(entry.get("connectedname"))
            updates.append((ctype, mac, common))

        if updates:
            self.set_children_state_batch(updates)

    def _apply_routes(self, info: dict[str, Any]) -> None:
        """`config get device routes vaurs ALLRX` — every decoder's five routes.

        The CBOX answers with source NAMES; state stores the source encoder's
        child id (its MAC) so a UI binding can hop straight to the encoder.
        """
        updates: list[tuple[str, str, dict[str, Any]]] = []
        wire_to_prop = {
            "video": "source_video",
            "audio": "source_audio",
            "usb": "source_usb",
            "ir": "source_infrared",
            "rs232": "source_serial",
        }
        for key, entry in info.items():
            if not isinstance(entry, dict):
                continue
            mac = self._resolve(key)
            if mac is None or self._type_by_mac.get(mac) != "decoder":
                continue
            routes: dict[str, Any] = {}
            for wire, prop in wire_to_prop.items():
                if wire not in entry:
                    continue
                source = _txt(entry.get(wire))
                if not source or source.lower() == "none":
                    routes[prop] = ""
                    continue
                routes[prop] = self._resolve(source) or source
            if routes:
                updates.append(("decoder", mac, routes))

        if updates:
            self.set_children_state_batch(updates)

    def _apply_serial(self, doc: dict[str, Any]) -> None:
        """An endpoint received RS-232 data and the CBOX relayed it to us."""
        mac = self._resolve(_txt(doc.get("mac")) or _txt(doc.get("id")))
        if mac is None:
            return
        payload = _txt(doc.get("info"))
        fmt = str(self.config.get("serial_feedback_format", "ascii")).lower()
        if fmt == "base64" and payload:
            try:
                payload = base64.b64decode(payload, validate=True).decode(
                    "utf-8", errors="replace"
                )
            except (binascii.Error, ValueError):
                log.debug(f"[{self.device_id}] Serial payload is not valid base64; kept raw")
        self.set_child_state(self._type_by_mac[mac], mac, "serial_data", payload)

    def _resolve(self, token: str) -> str | None:
        """Map a MAC or a custom name (either case) to a known child id."""
        token = _txt(token)
        if not token:
            return None
        upper = token.upper()
        if upper in self._type_by_mac:
            return upper
        return self._mac_by_name.get(token.lower())

    @staticmethod
    def _as_int(value: Any, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(str(value).strip())))
        except (TypeError, ValueError):
            return low

    # ── Polling ──────────────────────────────────────────────────────

    async def poll(self) -> None:
        """Refresh the roster, AV status and routes; system info on a slow cadence.

        Three broad queries cover the whole install regardless of endpoint count.
        Transport errors propagate so the platform's watchdog can flip the device
        offline (poll() contract).
        """
        doc = await self._request("config get devicelist", timeout=ROSTER_TIMEOUT_S)
        if doc is None:
            self._poll_misses += 1
            if self._poll_misses >= MAX_POLL_MISSES:
                raise ConnectionError(
                    f"[{self.device_id}] The CBOX stopped answering API queries "
                    f"({self._poll_misses} consecutive misses)"
                )
            return
        self._poll_misses = 0
        if isinstance(doc.get("info"), dict):
            self._apply_devicelist(doc["info"])

        doc = await self._request("config get device status ALL", timeout=ROSTER_TIMEOUT_S)
        if doc is not None and isinstance(doc.get("info"), dict):
            self._apply_status(doc["info"])

        if self._known["decoder"]:
            doc = await self._request(
                "config get device routes vaurs ALLRX", timeout=ROSTER_TIMEOUT_S
            )
            if doc is not None and isinstance(doc.get("info"), dict):
                self._apply_routes(doc["info"])

        if self._poll_cycle % FULL_REFRESH_EVERY == 0:
            await self._poll_system()
        self._poll_cycle += 1

    async def _poll_system(self) -> None:
        doc = await self._request("config get version")
        if doc is not None:
            self.set_state("firmware", _txt(doc.get("info")))

        doc = await self._request("config get ipsetting")
        if doc is not None:
            self.set_state("av_ip", self._ip_of(doc.get("info")))

        doc = await self._request("config get ipsetting2")
        if doc is not None:
            self.set_state("lan_ip", self._ip_of(doc.get("info")))

        doc = await self._request("config get previews")
        if doc is not None:
            self.set_state("previews", _txt(doc.get("info")) in ("1", "on", "true"))

        doc = await self._request("config get rs-232 alias")
        if doc is not None:
            self.set_state("rs232_alias", _txt(doc.get("info")).lower() == "on")

        doc = await self._request("config get timezone")
        if doc is not None:
            self.set_state("timezone", _txt(doc.get("info")))

        doc = await self._request("config get ntp")
        if doc is not None:
            servers = [s for s in _txt(doc.get("info")).split("/") if s]
            self.set_state("ntp_servers", " ".join(servers))

        await self._refresh_named_lists()

    async def _refresh_named_lists(self) -> None:
        """Populate the preset / scene / video wall / KVM pickers."""
        for query, key in (
            ("matrix preset list", "preset_options"),
            ("scene list", "scene_options"),
            ("vw list", "videowall_options"),
            ("kvm list", "kvm_options"),
        ):
            doc = await self._request(query)
            if doc is None:
                continue
            names = self._names_of(doc.get("info"))
            self.set_state(key, json.dumps([{"value": n, "label": n} for n in names]))

    @staticmethod
    def _names_of(info: Any) -> list[str]:
        """List replies are either a name->detail map or a bare list of names."""
        if isinstance(info, dict):
            return sorted(str(name) for name in info)
        if isinstance(info, list):
            return sorted(str(name) for name in info)
        return []

    @staticmethod
    def _ip_of(info: Any) -> str:
        """ipsetting replies are '<mode>/<ip>/<mask>/<gateway>'."""
        parts = _txt(info).split("/")
        return parts[1] if len(parts) > 1 else ""

    async def _normalize_serial_feedback(self) -> None:
        """Put every endpoint on the serial encapsulation this driver decodes."""
        fmt = str(self.config.get("serial_feedback_format", "ascii")).lower()
        code = SERIAL_FORMATS.get(fmt)
        if code is None:
            return
        doc = await self._request(f"config set device rs232responsetype {code} ALL")
        if doc is None or int(doc.get("code", 0)) != 0:
            log.warning(
                f"[{self.device_id}] Could not set the serial feedback format to {fmt}; "
                f"inbound serial data may not decode"
            )

    # ── Device settings ──────────────────────────────────────────────

    async def set_device_setting(self, setting: str, value: Any) -> None:
        if setting == "previews":
            await self._write(f"config set previews {'on' if value else 'off'}")
            self.set_state("previews", bool(value))
            return
        if setting == "rs232_alias":
            await self._write(f"config set rs-232 alias {'on' if value else 'off'}")
            self.set_state("rs232_alias", bool(value))
            return
        if setting == "timezone":
            zone = str(value).strip().upper()
            if not re.match(r"^UTC[+-](?:[0-9]|1[0-2])$", zone):
                raise ValueError(f"Timezone must look like UTC-5 (UTC-12 to UTC+12), not {value!r}")
            await self._write(f"config set timezone {zone}")
            self.set_state("timezone", zone)
            return
        if setting == "ntp_servers":
            servers = str(value).replace("/", " ").split()
            if not servers:
                raise ValueError("Enter at least one NTP server")
            if len(servers) > 5:
                raise ValueError("The CBOX accepts at most five NTP servers")
            await self._write("config set ntp " + " ".join(servers))
            self.set_state("ntp_servers", " ".join(servers))
            return
        raise ValueError(f"Unknown device setting: {setting}")

    # ── Commands ─────────────────────────────────────────────────────

    def _child(self, ctype: str, value: Any) -> str:
        """Resolve a child_id param to the wire identifier (the endpoint's MAC)."""
        mac = self._resolve(str(value))
        if mac is None or self._type_by_mac.get(mac) != ctype:
            label = "encoder" if ctype == "encoder" else "decoder"
            raise ValueError(
                f"Unknown {label} {value!r} — pick one from the dropdown "
                f"(Refresh from Device re-reads the roster)"
            )
        return mac

    def _endpoint(self, value: Any) -> str:
        mac = self._resolve(str(value))
        if mac is None:
            raise ValueError(
                f"Unknown endpoint {value!r} — pick one from the dropdown "
                f"(Refresh from Device re-reads the roster)"
            )
        return mac

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}

        # ── Routing ──
        if command == "route":
            tx = self._child("encoder", params["tx"])
            rx = self._child("decoder", params["rx"])
            stream = str(params.get("stream", "all")).lower()
            if stream == "all":
                return await self._write(f"matrix aset :{STREAM_CODES['all']} {tx} {rx}")
            return await self._write(f"config set device {ROUTE_COMMANDS[stream]} {tx} {rx}")

        if command == "route_off":
            rx = self._child("decoder", params["rx"])
            stream = str(params.get("stream", "all")).lower()
            targets = list(UNROUTE_COMMANDS) if stream == "all" else [stream]
            for name in targets:
                await self._write(f"config set device {UNROUTE_COMMANDS[name]} {rx}")
            return True

        recalls = {
            "recall_preset": "matrix preset active",
            "recall_scene": "scene active",
            "recall_videowall": "vw active",
        }
        if command in recalls:
            name = str(params["name"]).strip()
            force = " force" if params.get("force") else ""
            return await self._write(f"{recalls[command]} {name}{force}")

        if command == "recall_kvm":
            return await self._write(f"kvm active {str(params['name']).strip()}")

        # ── Decoder ──
        if command == "set_stream":
            rx = self._child("decoder", params["rx"])
            state = str(params["state"]).lower()
            await self._write(f"config set device stream {state} {rx}")
            self.set_child_state("decoder", rx, "stream", state == "on")
            return True

        if command == "set_blackout":
            rx = self._child("decoder", params["rx"])
            state = str(params["state"]).lower()
            await self._write(f"config set device blackout {state} {rx}")
            self.set_child_state("decoder", rx, "blackout", state == "on")
            return True

        if command == "set_volume":
            rx = self._child("decoder", params["rx"])
            return await self._write(
                f"config set device audio volume {int(params['level'])} {rx}"
            )

        if command == "set_audio_delay":
            rx = self._child("decoder", params["rx"])
            return await self._write(f"config set device audiomute {int(params['delay'])} {rx}")

        if command == "set_rotate":
            rx = self._child("decoder", params["rx"])
            rotation = str(params["rotation"])
            await self._write(f"config set device rotate {rotation} {rx}")
            self.set_child_state("decoder", rx, "rotate", rotation)
            return True

        if command == "set_stretch":
            rx = self._child("decoder", params["rx"])
            mode = str(params["mode"])
            await self._write(f"config set device stretch {mode} {rx}")
            self.set_child_state("decoder", rx, "stretch", mode)
            return True

        if command == "set_test_pattern":
            rx = self._child("decoder", params["rx"])
            pattern = str(params["pattern"])
            await self._write(f"config set device pattern {pattern} {rx}")
            self.set_child_state("decoder", rx, "test_pattern", pattern)
            return True

        if command == "set_hdcp":
            rx = self._child("decoder", params["rx"])
            return await self._write(f"config set device hdcp {str(params['mode'])} {rx}")

        if command == "set_osd":
            rx = self._child("decoder", params["rx"])
            return await self._write(f"config set device osd {str(params['state']).lower()} {rx}")

        # ── Encoder ──
        if command == "set_edid":
            tx = self._child("encoder", params["tx"])
            edid = str(params["edid"])
            await self._write(f"config set device edid {edid} {tx}")
            self.set_child_state("encoder", tx, "edid", edid)
            return True

        if command == "copy_edid":
            rx = self._child("decoder", params["rx"])
            tx = self._child("encoder", params["tx"])
            return await self._write(f"config set device copyedid {rx} {tx}")

        if command == "set_encoder_volume":
            tx = self._child("encoder", params["tx"])
            level = int(params["level"])
            await self._write(f"config set device exaudio volume {level} {tx}")
            self.set_child_state("encoder", tx, "audio_volume", level)
            return True

        if command == "set_audio_input":
            tx = self._child("encoder", params["tx"])
            return await self._write(
                f"config set device audio input type {str(params['source']).lower()} {tx}"
            )

        # ── Any endpoint ──
        if command == "identify":
            mac = self._endpoint(params["endpoint"])
            return await self._write(
                f"config set device light {str(params.get('mode', 'flash')).lower()} {mac}"
            )

        if command == "reboot_endpoint":
            mac = self._endpoint(params["endpoint"])
            return await self._write(f"config set device reboot {mac}")

        if command == "rename_endpoint":
            mac = self._endpoint(params["endpoint"])
            name = str(params["name"]).strip()
            await self._write(f"config set device id {name} {mac}")
            await self._enumerate_roster()
            return True

        if command == "hpd_reset":
            mac = self._endpoint(params["endpoint"])
            return await self._write(f"config set device hpdrst {mac}")

        if command == "send_cec":
            mac = self._endpoint(params["endpoint"])
            data = str(params["data"]).replace(" ", "")
            return await self._write(f"config set device cec {data} {mac}")

        if command == "send_ir":
            mac = self._endpoint(params["endpoint"])
            return await self._write(f"config set device ir {str(params['code']).strip()} {mac}")

        if command == "send_serial":
            mac = self._endpoint(params["endpoint"])
            data = str(params["data"])
            if params.get("append_cr"):
                data += "\\r"
            data_type = "2" if str(params.get("format", "ascii")).lower() == "hex" else "1"
            return await self._write(f"config set device rs232 {data_type} {data} {mac}")

        if command == "set_serial_settings":
            mac = self._endpoint(params["endpoint"])
            baud = str(params["baud"])
            bits = str(params.get("data_bits", "8"))
            parity = str(params.get("parity", "0"))
            # Stop bits and flow control are fixed by the hardware (1 / none).
            return await self._write(
                f"config set device rs232setting {baud} {bits} {parity} 1 0 {mac}"
            )

        if command == "set_serial_feedback":
            mac = self._endpoint(params["endpoint"])
            code = SERIAL_FORMATS[str(params["format"]).lower()]
            return await self._write(f"config set device rs232responsetype {code} {mac}")

        # ── System ──
        if command == "reboot_cbox":
            return await self._write("config set reboot")

        if command == "refresh":
            await self.refresh_children()
            return True

        if command == "raw_command":
            doc = await self._request(str(params["command"]).strip())
            if doc is None:
                raise ConnectionError(f"[{self.device_id}] No reply from the CBOX")
            return json.dumps(doc)

        log.warning(f"[{self.device_id}] Unknown command: {command}")
        return None

    # ── Setup action ─────────────────────────────────────────────────

    async def run_setup_action(
        self, action_id: str, params: dict[str, Any], progress: Any
    ) -> dict[str, Any]:
        if action_id != "test_connection":
            raise ValueError(f"Unknown setup action: {action_id}")

        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", 24))
        if not host:
            raise ValueError("No IP address configured")

        await progress(f"Connecting to {host}:{port}…", 20)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=5.0
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise ConnectionError(
                f"Could not reach an MXNet API on {host}:{port} ({exc}). Check that this is "
                f"the CBOX's MENTOR/LAN port."
            ) from exc

        try:
            await progress("Reading the control box…", 55)
            model = await self._probe(reader, writer, "config get name")
            if not model:
                raise ConnectionError(
                    f"{host}:{port} accepted the connection but did not answer the MXNet API."
                )
            firmware = await self._probe(reader, writer, "config get version")

            await progress("Counting endpoints…", 85)
            doc = await self._probe_json(reader, writer, "config get devicelist")
            endpoints = len(doc.get("info", {})) if isinstance(doc.get("info"), dict) else 0

            summary = f"Found {model}"
            if firmware:
                summary += f" (firmware {firmware})"
            summary += f" with {endpoints} endpoint{'' if endpoints == 1 else 's'}"
            if endpoints == 0:
                summary += " — commission the system in MENTOR first"
            return {"success": True, "message": summary}
        finally:
            writer.close()

    async def _probe_json(self, reader: Any, writer: Any, line: str) -> dict[str, Any]:
        writer.write((line + "\r\n").encode())
        await writer.drain()
        buf = b""
        deadline = REQUEST_TIMEOUT_S
        while True:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=deadline)
            if not chunk:
                return {}
            buf += chunk
            frame, buf = _json_frame(buf)
            if frame:
                try:
                    doc = json.loads(frame.decode("utf-8", errors="replace"))
                except (ValueError, TypeError):
                    return {}
                return doc if isinstance(doc, dict) else {}

    async def _probe(self, reader: Any, writer: Any, line: str) -> str:
        doc = await self._probe_json(reader, writer, line)
        return _txt(doc.get("info"))
