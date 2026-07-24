"""
OpenAVC WyreStorm NetworkHD Driver.

Controls a WyreStorm NetworkHD AV-over-IP system through its NHD-000-CTL or
NHD-CTL-PRO controller — plain text on Telnet TCP port 23. The controller is
the single API ingress for every NetworkHD endpoint (100/110/140, 200/210/
220/250, 400, 500, and 600 series encoders and decoders); a control system
never talks to a TX or RX directly.

Protocol source: "NetworkHD API" v6.5 (document 211207), WyreStorm
Technologies. Commands are LF-terminated lines; every reply line from the
CTL ends CRLF.

Push vs poll: HYBRID. The CTL sends documented unsolicited notifications
(endpoint online/offline, TX/RX video lost/found, CEC / infrared / RS-232
data received) which the driver parses live. Matrix assignments and endpoint
status have no notification in the documented API, so those refresh on the
poll cycle; the poll also doubles as a liveness check (two consecutive
unanswered roster queries raise a typed no-response fault).

Why Python (not .avcdriver YAML):
  * The endpoint roster is device-enumerated: `config get devicejsonstring`
    answers a pretty-printed multi-line JSON array from which TX and RX
    children are registered (dynamic string ids from the Console-assigned
    alias). YAML rosters are config-driven only.
  * JSON replies span dozens of CRLF-terminated lines; the driver
    accumulates a block until its brackets balance, then fans one document
    out into every endpoint child's state. YAML response rules are
    per-line.
  * `notify serialinfo` is counted framing — the header carries the length
    of the data that follows, which may itself contain line endings.
  * Requests are serialized with per-request completion matching (mirror
    echo, list header, or balanced JSON block) so replies, list blocks, and
    interleaved notifications stay untangled on one session.

The CTL's RS-232 port (9600 8N1) speaks the same API but is out of scope —
the controller of an AV-over-IP system always has the network path, and the
platform models one transport per driver instance (tvone_coriomatrix
precedent).

Telnet security note: the CTL's optional Telnet-over-TLS (992) and SSH
(10022) channels are not supported; the driver uses the factory-default
plain Telnet channel (no credentials).
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Callable

from server.drivers.base import BaseDriver
from server.utils.logger import get_logger

log = get_logger(__name__)

# Serialized request/response: how long to wait for one request's
# completion line/block before giving up on it.
REQUEST_TIMEOUT_S = 5.0

# Consecutive unanswered roster queries before poll() raises (never-offline
# guard for a silently dead CTL — the TCP socket can stay open).
MAX_POLL_MISSES = 2

# Safety cap for a counted `notify serialinfo` consume, in case the
# declared length never arrives (a lying or truncated notification).
SERIALINFO_MAX_CHARS = 8192

# How many poll cycles between full refreshes (device info, breakaway
# matrix views, scene/layout lists). Cycle 0 always runs a full refresh.
FULL_REFRESH_EVERY = 6

_RE_HOSTNAME_MODEL = re.compile(r"(?i)^(NHD-[A-Z0-9-]+?)-[0-9A-F]{12}$")

# Response headers that open a multi-line block.
_JSON_HEADERS = {
    "device json string:": "devicejson",
    "devices json info:": "deviceinfo",
    "devices status info:": "devicestatus",
}
_LIST_HEADERS = {
    "matrix information:": "av",
    "matrix video information:": "video",
    "matrix audio information:": "audio",
    "matrix audio2 information:": "audio2",
    "matrix audio3 information:": "audio3",
    "matrix usb information:": "usb",
    "matrix infrared information:": "infrared",
    "matrix serial information:": "serial",
    "matrix infrared2 information:": "infrared2",
    "matrix serial2 information:": "serial2",
    "scene list:": "scenes",
    "wscene2 list:": "wscenes",
    "mscene list:": "mscenes",
    "video wall information:": "vw",
    "mview information:": "mview",
}

_RE_PAIR = re.compile(r"^(\S+)\s+(\S+)$")
_RE_MODE2 = re.compile(r"^(\S+)\s+(single|api|all|null)(?:\s+(\S+))?$", re.IGNORECASE)
_RE_API_VERSION = re.compile(r"(?i)^API version:\s*v?(.+?)\s*$")
_RE_SYS_VERSION = re.compile(r"(?i)^System version:\s*v?(.+?)\s*$")
_RE_NOTIFY_ENDPOINT = re.compile(r"^notify endpoint\s+([+\-–])\s+(\S+)\s*$")
_RE_NOTIFY_VIDEO = re.compile(r"(?i)^notify video\s+(lost|found)\s+(\S+)(?:\s+(\S+))?\s*$")
_RE_NOTIFY_CEC = re.compile(r'^notify cecinfo\s+(\S+)\s+"(.*)"\s*$')
_RE_NOTIFY_IR = re.compile(r'^notify irinfo\s+(\S+)\s+"(.*)"\s*$')
_RE_NOTIFY_SERIAL = re.compile(
    r"(?i)^notify serialinfo\s+(\S+)\s+(hex|ascii)\s+(\d+):\s*(.*)$"
)

_ENDPOINT_PARAM_HELP = (
    "Endpoint alias (or hostname). The dropdown lists every endpoint the "
    "controller knows about."
)


def _sanitize_id(name: str) -> str:
    """Wire name (alias/hostname) -> child-safe local id."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", str(name).strip()).strip("_")
    return (cleaned or "endpoint").lower()[:40]


def _model_from_hostname(hostname: str) -> str:
    """'NHD-500-TX-E4CE02F34D67' -> 'NHD-500-TX'."""
    m = _RE_HOSTNAME_MODEL.match(str(hostname).strip())
    return m.group(1).upper() if m else ""


def _on_off(value: Any) -> str:
    return "on" if value in (True, "true", "True", "on", 1, "1") else "off"


class WyrestormNetworkHDDriver(BaseDriver):
    """WyreStorm NetworkHD system via NHD-000-CTL / NHD-CTL-PRO (Telnet 23)."""

    DRIVER_INFO = {
        "id": "wyrestorm_networkhd",
        "name": "WyreStorm NetworkHD",
        "manufacturer": "WyreStorm",
        "category": "switcher",
        "version": "1.0.1",
        # The connection lifecycle hooks this driver overrides landed in 0.24.0.
        "min_platform_version": "0.24.0",
        "author": "OpenAVC",
        "description": (
            "Controls a WyreStorm NetworkHD AV-over-IP system through its "
            "NHD-000-CTL or NHD-CTL-PRO controller. Every encoder (TX) and "
            "decoder (RX) the controller knows about appears as a child "
            "entity with live online, routing, and signal state. Covers "
            "matrix switching with per-stream breakaway (video, audio, USB, "
            "IR, RS-232), video wall scenes and windows, multiview layouts, "
            "display power proxy commands, and CEC / IR / RS-232 command "
            "injection to connected devices."
        ),
        "source_url": "https://wyrestorm.com/download/networkhd-api/",
        "tags": [
            "av-over-ip", "networkhd", "matrix-switcher", "video-wall",
            "multiview", "encoder", "decoder",
        ],
        "verified": False,
        "simulated": True,
        "protocols": ["networkhd_api"],
        "ports": [23],
        "transport": "tcp",
        "discovery": {
            # The API channel is plain Telnet on the shared port 23 with no
            # documented banner or safe identifying probe, so evidence is
            # hint-only (ome_ps62 precedent: no blind probes on port 23).
            "port_open": [23],
            "manufacturer_alias": ["wyrestorm"],
        },
        "compatible_models": [
            {
                "manufacturer": "WyreStorm",
                "models": ["NHD-000-CTL", "NHD-CTL-PRO"],
                "confidence": "untested",
                "notes": (
                    "Add the CONTROLLER as the device — endpoints appear as "
                    "children. Controls NetworkHD 100/110/140, 200/210/220/"
                    "250, 400, 500, and 600 series endpoints per the v6.5 "
                    "API; series-specific commands answer 'unknown command' "
                    "or are ignored on endpoints that lack the feature. "
                    "Factory CONTROL port IP is 192.168.11.243."
                ),
            },
        ],
        "help": {
            "overview": (
                "NetworkHD is WyreStorm's AV-over-IP platform: encoders "
                "(TX) capture sources and decoders (RX) feed displays over "
                "the network, with the NHD-CTL controller as the single "
                "point of control. This driver connects to the controller "
                "and models every endpoint as a child entity — decoders "
                "carry their routed source and output status, encoders "
                "their input signal. Routing, video wall scenes, multiview "
                "layouts, display power, and CEC/IR/RS-232 injection are "
                "all available to macros and panels."
            ),
            "setup": (
                "1. Add the NHD-CTL controller by its IP address (factory "
                "CONTROL port default is 192.168.11.243), port 23.\n"
                "2. Give endpoints meaningful aliases in the NetworkHD "
                "Console software first — children are named by alias, and "
                "aliases survive hardware swaps (hostnames do not).\n"
                "3. Leave the controller's API channel on the default "
                "Telnet setting (the optional TLS/SSH channels are not "
                "supported).\n"
                "4. Video wall scenes, window scenes, and multiview "
                "layouts are authored in NetworkHD Console; the driver "
                "recalls them by name from live dropdowns.\n"
                "5. Use Refresh from Device after adding or removing "
                "endpoints."
            ),
        },
        "default_config": {
            "host": "",
            "port": 23,
            "poll_interval": 10,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address",
                     "description": "IP address of the NHD-CTL controller (not an endpoint)."},
            "port": {
                "type": "integer",
                "default": 23,
                "label": "TCP Port",
                "description": "Telnet API port — 23 on every controller.",
            },
            "poll_interval": {
                "type": "integer",
                "default": 10,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "Refreshes the endpoint roster, matrix assignments, and "
                    "endpoint status. Notifications arrive live regardless; "
                    "polling also detects a silently dead controller. Leave "
                    "above 0."
                ),
            },
        },
        "state_variables": {
            "api_version": {"type": "string", "label": "API Version"},
            "system_version": {"type": "string", "label": "System Version"},
            "tx_count": {"type": "integer", "label": "Encoders",
                         "help": "Encoders (TX) known to the controller.",
                         "cloud_priority": "low"},
            "tx_online": {"type": "integer", "label": "Encoders Online",
                          "cloud_priority": "low"},
            "rx_count": {"type": "integer", "label": "Decoders",
                         "help": "Decoders (RX) known to the controller.",
                         "cloud_priority": "low"},
            "rx_online": {"type": "integer", "label": "Decoders Online",
                          "cloud_priority": "low"},
            "endpoint_options": {
                "type": "string", "label": "Endpoint Options",
                "help": "JSON list of every endpoint — feeds the endpoint dropdowns.",
                "cloud_priority": "low",
            },
            "scene_options": {
                "type": "string", "label": "Video Wall Scene Options",
                "help": "JSON list of the standard video wall scenes stored on the controller.",
                "cloud_priority": "low",
            },
            "wscene_options": {
                "type": "string", "label": "Window Scene Options",
                "help": "JSON list of the 'video wall within a wall' scenes stored on the controller.",
                "cloud_priority": "low",
            },
            "vw_options": {
                "type": "string", "label": "Logical Screen Options",
                "help": "JSON list of videowall-scene_LogicalScreen references from the controller.",
                "cloud_priority": "low",
            },
            "mscene_options": {
                "type": "string", "label": "Multiview Layout Options",
                "help": "JSON list of every preset multiview layout name (see each decoder's Layouts for what it supports).",
                "cloud_priority": "low",
            },
        },
        "child_entity_types": {
            "tx": {
                "label": "Encoder",
                "label_plural": "Encoders",
                "id_format": {"type": "string", "max_length": 40},
                "state_variables": {
                    "online": {"type": "boolean", "label": "Online",
                               "help": "Endpoint online status from the controller (pushed live).",
                               "cloud_priority": "high"},
                    "signal": {"type": "boolean", "label": "Input Signal",
                               "help": "Valid video at the encoder's input (polled; 400/500 series also push it live).",
                               "control": True, "cloud_priority": "high"},
                    "resolution": {"type": "string", "label": "Input Resolution"},
                    "frame_rate": {"type": "string", "label": "Input Frame Rate",
                                   "cloud_priority": "low"},
                    "audio_input_format": {"type": "string", "label": "Audio Input Format",
                                           "cloud_priority": "low"},
                    "video_port": {"type": "string", "label": "Video Input Port",
                                   "help": "600 series input port selection (auto/hdmi/dp).",
                                   "cloud_priority": "low"},
                    "name": {"type": "string", "label": "Alias", "cloud_priority": "low"},
                    "hostname": {"type": "string", "label": "Hostname", "cloud_priority": "low"},
                    "model": {"type": "string", "label": "Model", "cloud_priority": "low"},
                    "ip": {"type": "string", "label": "IP Address", "cloud_priority": "low"},
                    "firmware": {"type": "string", "label": "Firmware", "cloud_priority": "low"},
                    "cec_data": {"type": "string", "label": "CEC Data Received",
                                 "help": "Last CEC data pushed by this endpoint (enable with cec_notify).",
                                 "cloud_priority": "low"},
                    "ir_data": {"type": "string", "label": "IR Data Received",
                                "help": "Last infrared data pushed by this endpoint (600 series: route its IR receiver to 'api').",
                                "cloud_priority": "low"},
                    "serial_data": {"type": "string", "label": "RS-232 Data Received",
                                    "help": "Last RS-232 data pushed by this endpoint (automatic on 100/200/400; 600 series: route its RS-232 to 'api').",
                                    "cloud_priority": "low"},
                },
                "summary_fields": ["online", "signal"],
                "label_field": "name",
            },
            "rx": {
                "label": "Decoder",
                "label_plural": "Decoders",
                "id_format": {"type": "string", "max_length": 40},
                "state_variables": {
                    "online": {"type": "boolean", "label": "Online",
                               "help": "Endpoint online status from the controller (pushed live).",
                               "cloud_priority": "high"},
                    "source": {"type": "string", "label": "Source",
                               "help": "Encoder assigned to this decoder (all-media view); empty when unassigned.",
                               "control": True, "cloud_priority": "high"},
                    "video_source": {"type": "string", "label": "Video Source",
                                     "help": "Video-stream breakaway assignment.",
                                     "cloud_priority": "low"},
                    "audio_source": {"type": "string", "label": "Audio Source",
                                     "help": "HDMI-audio-stream breakaway assignment.",
                                     "cloud_priority": "low"},
                    "usb_source": {"type": "string", "label": "USB Source",
                                   "help": "USB routing assignment.",
                                   "cloud_priority": "low"},
                    "hdmi_out_active": {"type": "boolean", "label": "Output Active",
                                        "help": "Valid HDMI output at the decoder (polled).",
                                        "cloud_priority": "high"},
                    "hdmi_out_resolution": {"type": "string", "label": "Output Resolution"},
                    "hdmi_out_frame_rate": {"type": "string", "label": "Output Frame Rate",
                                            "cloud_priority": "low"},
                    "hdcp": {"type": "string", "label": "HDCP", "cloud_priority": "low"},
                    "stream_active": {"type": "boolean", "label": "Stream Active",
                                      "help": "AV-over-IP stream state pushed by 400/500 series decoders (notify video).",
                                      "cloud_priority": "low"},
                    "layouts": {"type": "string", "label": "Layouts",
                                "help": "JSON list of the multiview layouts stored for this decoder.",
                                "cloud_priority": "low"},
                    "name": {"type": "string", "label": "Alias", "cloud_priority": "low"},
                    "hostname": {"type": "string", "label": "Hostname", "cloud_priority": "low"},
                    "model": {"type": "string", "label": "Model", "cloud_priority": "low"},
                    "ip": {"type": "string", "label": "IP Address", "cloud_priority": "low"},
                    "firmware": {"type": "string", "label": "Firmware", "cloud_priority": "low"},
                    "cec_data": {"type": "string", "label": "CEC Data Received",
                                 "help": "Last CEC data pushed by this endpoint (enable with cec_notify).",
                                 "cloud_priority": "low"},
                    "ir_data": {"type": "string", "label": "IR Data Received",
                                "help": "Last infrared data pushed by this endpoint (600 series: route its IR receiver to 'api').",
                                "cloud_priority": "low"},
                    "serial_data": {"type": "string", "label": "RS-232 Data Received",
                                    "help": "Last RS-232 data pushed by this endpoint (automatic on 100/200/400; 600 series: route its RS-232 to 'api').",
                                    "cloud_priority": "low"},
                },
                "summary_fields": ["online", "source"],
                "label_field": "name",
            },
        },
        "quick_actions": ["route", "route_off", "scene_recall", "refresh"],
        "actions": [
            {"id": "route", "kind": "command", "icon": "route"},
            {"id": "route_off", "kind": "command", "icon": "circle-off"},
            {"id": "scene_recall", "kind": "command", "icon": "layout-grid"},
            {"id": "refresh", "kind": "command", "icon": "refresh-cw"},
            {"id": "reboot_controller", "kind": "command", "icon": "power",
             "confirm": True},
        ],
        "commands": {
            # ── Matrix switching (section 6) ──
            "route": {
                "label": "Route Source to Display",
                "params": {
                    "tx": {"type": "child_id", "child_type": "tx",
                           "required": True, "label": "Source (TX)"},
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Display (RX)"},
                },
                "help": "Assign all of an encoder's media streams (video, audio, USB, IR, RS-232 where fitted) to a decoder.",
            },
            "route_off": {
                "label": "Unassign Display",
                "params": {
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Display (RX)"},
                },
                "help": "Remove all media stream assignments from a decoder (matrix set null).",
            },
            "route_video": {
                "label": "Route Video Only",
                "params": {
                    "tx": {"type": "child_id", "child_type": "tx",
                           "required": True, "label": "Source (TX)"},
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Display (RX)"},
                },
                "help": "Breakaway: switch only the video stream, leaving other media assignments untouched.",
            },
            "route_video_off": {
                "label": "Unassign Video",
                "params": {
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Display (RX)"},
                },
            },
            "route_audio": {
                "label": "Route HDMI Audio Only",
                "params": {
                    "tx": {"type": "child_id", "child_type": "tx",
                           "required": True, "label": "Source (TX)"},
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Display (RX)"},
                },
                "help": "Breakaway: switch only the HDMI audio stream.",
            },
            "route_audio_off": {
                "label": "Unassign HDMI Audio",
                "params": {
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Display (RX)"},
                },
            },
            "route_audio_analog": {
                "label": "Route Analog Audio Only",
                "params": {
                    "tx": {"type": "child_id", "child_type": "tx",
                           "required": True, "label": "Source (TX)"},
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Display (RX)"},
                },
                "help": "600 series breakaway: assign a decoder's analog audio output to an encoder's analog audio stream (matrix audio2 set). The decoder must have its analog output set to 'analog'.",
            },
            "route_audio_analog_off": {
                "label": "Unassign Analog Audio",
                "params": {
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Display (RX)"},
                },
            },
            "route_arc": {
                "label": "Route ARC Return",
                "params": {
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Display (RX)"},
                    "tx": {"type": "child_id", "child_type": "tx",
                           "required": True, "label": "To Source (TX)"},
                },
                "help": "500 series: assign a decoder's ARC (display audio return) stream to an encoder (matrix audio3 set).",
            },
            "route_usb": {
                "label": "Route USB",
                "params": {
                    "tx": {"type": "child_id", "child_type": "tx",
                           "required": True, "label": "Host (TX)"},
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Device Side (RX)"},
                },
                "help": "Breakaway: link a decoder's USB ports to an encoder's USB host port (110/400/500/600 with USB).",
            },
            "route_usb_off": {
                "label": "Unassign USB",
                "params": {
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Display (RX)"},
                },
            },
            "route_infrared": {
                "label": "Route Infrared",
                "params": {
                    "tx": {"type": "child_id", "child_type": "tx",
                           "required": True, "label": "Source (TX)"},
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Display (RX)"},
                },
                "help": "Breakaway: link IR both ways between an encoder and decoder(s) (110/400/500 series).",
            },
            "route_infrared_off": {
                "label": "Unassign Infrared",
                "params": {
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Display (RX)"},
                },
            },
            "route_serial": {
                "label": "Route RS-232",
                "params": {
                    "tx": {"type": "child_id", "child_type": "tx",
                           "required": True, "label": "Source (TX)"},
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Display (RX)"},
                },
                "help": "Breakaway: link the RS-232 data path between an encoder and decoder(s) (100/110/200/400/500 series).",
            },
            "route_serial_off": {
                "label": "Unassign RS-232",
                "params": {
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Display (RX)"},
                },
            },
            "set_infrared_mode": {
                "label": "Set IR Receiver Mode (600)",
                "params": {
                    "endpoint": {"type": "string", "required": True,
                                 "label": "Endpoint",
                                 "options_state": "endpoint_options",
                                 "help": _ENDPOINT_PARAM_HELP},
                    "mode": {"type": "enum", "required": True, "label": "Mode",
                             "values": ["single", "api", "all", "null"],
                             "help": "single = to one endpoint's IR emitter; api = to API notifications; all = to every endpoint; null = unassigned."},
                    "target": {"type": "string", "required": False,
                               "label": "Target (single mode)",
                               "options_state": "endpoint_options"},
                },
                "help": "600 series: assign an endpoint's IR receiver port (matrix infrared2 set).",
            },
            "set_serial_mode": {
                "label": "Set RS-232 Mode (600)",
                "params": {
                    "endpoint": {"type": "string", "required": True,
                                 "label": "Endpoint",
                                 "options_state": "endpoint_options",
                                 "help": _ENDPOINT_PARAM_HELP},
                    "mode": {"type": "enum", "required": True, "label": "Mode",
                             "values": ["single", "api", "all", "null"],
                             "help": "single = to one endpoint's RS-232 port; api = to API notifications; all = to every endpoint; null = unassigned."},
                    "target": {"type": "string", "required": False,
                               "label": "Target (single mode)",
                               "options_state": "endpoint_options"},
                },
                "help": "600 series: assign an endpoint's RS-232 receive path (matrix serial2 set).",
            },
            # ── Device port switching (section 7) ──
            "set_video_port": {
                "label": "Set Encoder Video Input Port (600)",
                "params": {
                    "tx": {"type": "child_id", "child_type": "tx",
                           "required": True, "label": "Source (TX)"},
                    "port": {"type": "enum", "required": True, "label": "Port",
                             "values": ["auto", "hdmi", "dp"]},
                },
                "help": "600 series encoders with both HDMI and DisplayPort inputs.",
            },
            "set_audio_source": {
                "label": "Set Decoder HDMI Audio Source (600)",
                "params": {
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Display (RX)"},
                    "source": {"type": "enum", "required": True, "label": "Source",
                               "values": ["hdmi", "dmix", "analog"]},
                },
                "help": "Which of the assigned encoder's audio streams feeds the decoder's HDMI output.",
            },
            "set_analog_audio_source": {
                "label": "Set Decoder Analog Audio Source (600)",
                "params": {
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Display (RX)"},
                    "source": {"type": "enum", "required": True, "label": "Source",
                               "values": ["analog", "dmix"]},
                },
                "help": "analog = follow the assigned encoder's analog stream; dmix = downmix of the decoder's own HDMI audio output.",
            },
            "set_audio_input_type": {
                "label": "Set Encoder Audio Input (500-DNT)",
                "params": {
                    "tx": {"type": "child_id", "child_type": "tx",
                           "required": True, "label": "Source (TX)"},
                    "source": {"type": "enum", "required": True, "label": "Input",
                               "values": ["auto", "hdmi", "analog"]},
                },
                "help": "NHD-500-DNT-TX: audio input port for the main AV stream.",
            },
            "set_dante_audio_input": {
                "label": "Set Encoder Dante Input (500-DNT)",
                "params": {
                    "tx": {"type": "child_id", "child_type": "tx",
                           "required": True, "label": "Source (TX)"},
                    "source": {"type": "enum", "required": True, "label": "Input",
                               "values": ["hdmi", "analog"]},
                },
                "help": "NHD-500-DNT-TX: audio input port for the Dante stream.",
            },
            "set_usb_mode": {
                "label": "Set USB Working Mode (400)",
                "params": {
                    "endpoint": {"type": "string", "required": True,
                                 "label": "Endpoint",
                                 "options_state": "endpoint_options",
                                 "help": _ENDPOINT_PARAM_HELP},
                    "kmoip": {"type": "boolean", "required": True,
                              "label": "Automatic KMoIP Mode"},
                },
                "help": "400 series: on = automatic KMoIP for HID devices (default); off = USBoIP mode.",
            },
            # ── Connected device control (section 8) ──
            "sink_power": {
                "label": "Display Power (Proxy)",
                "params": {
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Display (RX)"},
                    "power": {"type": "enum", "required": True, "label": "Power",
                              "values": ["on", "off"]},
                },
                "help": "Send the decoder's configured power proxy command (CEC and/or RS-232, set up in NetworkHD Console; disabled by default) to the attached display.",
            },
            "cec_power": {
                "label": "Display Power (CEC)",
                "params": {
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Display (RX)"},
                    "power": {"type": "enum", "required": True, "label": "Power",
                              "values": ["onetouchplay", "standby"],
                              "labels": ["On (One Touch Play)", "Off (Standby)"]},
                },
                "help": "Send the decoder's CEC power command to the attached display — always available, unlike the sink_power proxy.",
            },
            "send_cec": {
                "label": "Send CEC Data",
                "params": {
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Display (RX)"},
                    "data": {"type": "string", "required": True, "label": "CEC Bytes (hex)",
                             "pattern": "^[0-9A-Fa-f]+$",
                             "help": "Hex bytes with no separators, e.g. FF36."},
                },
                "help": "Inject raw CEC data from a decoder to its attached display.",
            },
            "send_infrared": {
                "label": "Send Infrared Data",
                "params": {
                    "endpoint": {"type": "string", "required": True,
                                 "label": "Endpoint",
                                 "options_state": "endpoint_options",
                                 "help": _ENDPOINT_PARAM_HELP},
                    "data": {"type": "string", "required": True, "label": "IR Data",
                             "help": "Pronto CCF hex for 400/600 series (110 series firmware v6 uses Global Cache format)."},
                },
                "help": "Emit an infrared code from an endpoint's IR emitter port.",
            },
            "send_serial": {
                "label": "Send RS-232 Data",
                "params": {
                    "endpoint": {"type": "string", "required": True,
                                 "label": "Endpoint",
                                 "options_state": "endpoint_options",
                                 "help": _ENDPOINT_PARAM_HELP},
                    "data": {"type": "string", "required": True, "label": "Data",
                             "trim": False,
                             "help": "ASCII text, or space-separated hex bytes when Hex Format is on."},
                    "baud": {"type": "enum", "required": True, "label": "Baud",
                             "values": ["2400", "4800", "9600", "19200",
                                        "38400", "57600", "115200"]},
                    "data_bits": {"type": "enum", "required": False, "label": "Data Bits",
                                  "values": ["8", "7", "6"], "default": "8"},
                    "parity": {"type": "enum", "required": False, "label": "Parity",
                               "values": ["n", "o", "e"], "default": "n"},
                    "stop_bits": {"type": "enum", "required": False, "label": "Stop Bits",
                                  "values": ["1", "2"], "default": "1"},
                    "append_cr": {"type": "boolean", "required": False,
                                  "label": "Append CR", "default": False},
                    "append_lf": {"type": "boolean", "required": False,
                                  "label": "Append LF", "default": False},
                    "hex": {"type": "boolean", "required": False,
                            "label": "Hex Format", "default": False},
                },
                "help": "Transmit data out of an endpoint's RS-232 port to the connected device.",
            },
            "set_volume": {
                "label": "Analog Audio Volume",
                "params": {
                    "endpoint": {"type": "string", "required": True,
                                 "label": "Endpoint",
                                 "options_state": "endpoint_options",
                                 "help": _ENDPOINT_PARAM_HELP},
                    "action": {"type": "enum", "required": True, "label": "Action",
                               "values": ["up", "down", "mute", "unmute"],
                               "help": "400 series supports up/down only; 100/200 series support mute/unmute only."},
                },
                "help": "Step or mute the analog audio output level on an endpoint (no level read-back exists in the API).",
            },
            # ── Video wall (section 10) ──
            "scene_recall": {
                "label": "Recall Video Wall Scene",
                "params": {
                    "scene": {"type": "string", "required": True, "label": "Scene",
                              "options_state": "scene_options",
                              "help": "videowall-scene reference, e.g. OfficeVW-Splitmode."},
                },
                "help": "Apply a standard video wall scene (scene active).",
            },
            "scene_set_source": {
                "label": "Set Scene Display Source",
                "params": {
                    "scene": {"type": "string", "required": True, "label": "Scene",
                              "options_state": "scene_options"},
                    "x": {"type": "integer", "required": True, "label": "Column (X)", "min": 1},
                    "y": {"type": "integer", "required": True, "label": "Row (Y)", "min": 1},
                    "tx": {"type": "child_id", "child_type": "tx",
                           "required": True, "label": "Source (TX)"},
                },
                "help": "Change the encoder assigned to one display position in a scene. Saves to the scene; apply with Recall Video Wall Scene. Positions are 1-indexed from the top-left display.",
            },
            "vw_change": {
                "label": "Set Logical Screen Source",
                "params": {
                    "lscreen": {"type": "string", "required": True,
                                "label": "Logical Screen",
                                "options_state": "vw_options",
                                "help": "videowall-scene_LogicalScreen reference, e.g. OfficeVW-Combined_TopTwo."},
                    "tx": {"type": "child_id", "child_type": "tx",
                           "required": True, "label": "Source (TX)"},
                },
                "help": "Route an encoder to a logical screen immediately (does not save to the scene).",
            },
            "wscene_recall": {
                "label": "Recall Window Scene",
                "params": {
                    "wscene": {"type": "string", "required": True, "label": "Scene",
                               "options_state": "wscene_options"},
                },
                "help": "Apply a 'video wall within a wall' scene (wscene2 active).",
            },
            "window_open": {
                "label": "Open Window",
                "params": {
                    "wscene": {"type": "string", "required": True, "label": "Scene",
                               "options_state": "wscene_options"},
                    "window": {"type": "string", "required": True, "label": "Window Name"},
                    "x": {"type": "integer", "required": True, "label": "Column (X)", "min": 0},
                    "y": {"type": "integer", "required": True, "label": "Row (Y)", "min": 0},
                    "width": {"type": "integer", "required": True, "label": "Width (displays)", "min": 1},
                    "height": {"type": "integer", "required": True, "label": "Height (displays)", "min": 1},
                    "tx": {"type": "child_id", "child_type": "tx",
                           "required": True, "label": "Source (TX)"},
                },
                "help": "Open a window in a window scene. Positions are 0-indexed from the top-left display; effective immediately, not saved.",
            },
            "window_close": {
                "label": "Close Window",
                "params": {
                    "wscene": {"type": "string", "required": True, "label": "Scene",
                               "options_state": "wscene_options"},
                    "window": {"type": "string", "required": True, "label": "Window Name"},
                },
            },
            "window_change": {
                "label": "Set Window Source",
                "params": {
                    "wscene": {"type": "string", "required": True, "label": "Scene",
                               "options_state": "wscene_options"},
                    "window": {"type": "string", "required": True, "label": "Window Name"},
                    "tx": {"type": "child_id", "child_type": "tx",
                           "required": True, "label": "Source (TX)"},
                },
            },
            "window_adjust": {
                "label": "Move/Resize Window",
                "params": {
                    "wscene": {"type": "string", "required": True, "label": "Scene",
                               "options_state": "wscene_options"},
                    "window": {"type": "string", "required": True, "label": "Window Name"},
                    "x": {"type": "integer", "required": True, "label": "Column (X)", "min": 0},
                    "y": {"type": "integer", "required": True, "label": "Row (Y)", "min": 0},
                    "width": {"type": "integer", "required": True, "label": "Width (displays)", "min": 1},
                    "height": {"type": "integer", "required": True, "label": "Height (displays)", "min": 1},
                },
            },
            "window_layer": {
                "label": "Set Window Layer",
                "params": {
                    "wscene": {"type": "string", "required": True, "label": "Scene",
                               "options_state": "wscene_options"},
                    "window": {"type": "string", "required": True, "label": "Window Name"},
                    "layer": {"type": "enum", "required": True, "label": "Layer",
                              "values": ["up", "down", "top", "bottom"]},
                },
            },
            # ── Multiview (section 11) ──
            "mview_layout": {
                "label": "Recall Multiview Layout",
                "params": {
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Display (RX)"},
                    "layout": {"type": "string", "required": True, "label": "Layout",
                               "options_state": "mscene_options",
                               "help": "Preset layout name — see the decoder's Layouts value for what it stores."},
                },
                "help": "Apply a preset multiview layout to a decoder (mscene active).",
            },
            "mview_tile_source": {
                "label": "Set Multiview Tile Source",
                "params": {
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Display (RX)"},
                    "layout": {"type": "string", "required": True, "label": "Layout",
                               "options_state": "mscene_options"},
                    "tile": {"type": "integer", "required": True, "label": "Tile", "min": 1},
                    "tx": {"type": "child_id", "child_type": "tx",
                           "required": True, "label": "Source (TX)"},
                },
                "help": "Change the encoder shown in one tile of a preset layout (effective immediately, not saved).",
            },
            "mview_audio": {
                "label": "Set Multiview Audio (Preset Layout)",
                "params": {
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Display (RX)"},
                    "layout": {"type": "string", "required": True, "label": "Layout",
                               "options_state": "mscene_options"},
                    "mode": {"type": "enum", "required": True, "label": "Mode",
                             "values": ["window", "separate"],
                             "help": "window = audio follows a tile's video; separate = audio pinned to an encoder."},
                    "target": {"type": "string", "required": True,
                               "label": "Tile # or Encoder",
                               "help": "Tile number for window mode; encoder alias for separate mode."},
                },
                "help": "220/250 series: set a preset layout's audio mode. Saved to the decoder; apply with Recall Multiview Layout.",
            },
            "mview_single": {
                "label": "Multiview Full Screen Source",
                "params": {
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Display (RX)"},
                    "tx": {"type": "child_id", "child_type": "tx",
                           "required": True, "label": "Source (TX)"},
                },
                "help": "220/250 series: show a single encoder full screen (multiview decoders do not respond to matrix set).",
            },
            "mview_custom": {
                "label": "Apply Custom Multiview Layout",
                "params": {
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Display (RX)"},
                    "mode": {"type": "enum", "required": True, "label": "Mode",
                             "values": ["tile", "overlay"]},
                    "tiles": {"type": "string", "required": True, "label": "Tile Spec",
                              "help": "Space-separated tile specs: <tx>:<x>_<y>_<w>_<h>:<fit|stretch>, e.g. source1:0_0_960_540:fit source2:960_0_960_540:fit. Pixel positions on the canvas; first tile is topmost in overlay mode."},
                },
                "help": "220/250 series: apply a custom tile layout (effective immediately, not saved).",
            },
            "mview_custom_audio": {
                "label": "Set Multiview Audio (Custom Layout)",
                "params": {
                    "rx": {"type": "child_id", "child_type": "rx",
                           "required": True, "label": "Display (RX)"},
                    "tx": {"type": "child_id", "child_type": "tx",
                           "required": True, "label": "Audio Source (TX)"},
                },
                "help": "220/250 series: pin a custom layout's audio to an encoder (custom layouts have no audio by default).",
            },
            # ── OSD (110/140/200 TX) ──
            "osd_text": {
                "label": "Set OSD Text",
                "params": {
                    "tx": {"type": "child_id", "child_type": "tx",
                           "required": True, "label": "Source (TX)"},
                    "text": {"type": "string", "required": True, "label": "Text"},
                    "x": {"type": "integer", "required": False, "label": "X",
                          "min": 0, "max": 1920, "default": 0},
                    "y": {"type": "integer", "required": False, "label": "Y",
                          "min": 0, "max": 1080, "default": 0},
                    "color": {"type": "string", "required": False, "label": "Color (hex)",
                              "default": "FFFFFFFF",
                              "help": "8-hex-digit ARGB (e.g. FFFFFFFF white, FFFF0000 red); NHD-200-TX uses a 4-digit format (FFFF white)."},
                    "size": {"type": "integer", "required": False, "label": "Size",
                             "min": 1, "default": 1},
                },
                "help": "110/140/200 series encoders: configure the on-screen text overlay, then switch it on with osd_on.",
            },
            "osd_on": {
                "label": "OSD On",
                "params": {
                    "tx": {"type": "child_id", "child_type": "tx",
                           "required": True, "label": "Source (TX)"},
                },
            },
            "osd_off": {
                "label": "OSD Off",
                "params": {
                    "tx": {"type": "child_id", "child_type": "tx",
                           "required": True, "label": "Source (TX)"},
                },
            },
            # ── Notifications / maintenance ──
            "cec_notify": {
                "label": "CEC Notifications On/Off",
                "params": {
                    "scope": {"type": "string", "required": True, "label": "Scope",
                              "options_state": "endpoint_options",
                              "help": "An endpoint alias, or ALL_DEV / ALL_TX / ALL_RX."},
                    "enable": {"type": "boolean", "required": True, "label": "Enable"},
                },
                "help": "400/500 series: push received CEC data as notifications (updates the endpoints' CEC Data Received values).",
            },
            "reboot_device": {
                "label": "Reboot Endpoint",
                "params": {
                    "endpoint": {"type": "string", "required": True,
                                 "label": "Endpoint",
                                 "options_state": "endpoint_options",
                                 "help": _ENDPOINT_PARAM_HELP},
                },
                "help": "Reboot one endpoint (settings are retained).",
            },
            "reboot_controller": {
                "label": "Reboot Controller",
                "params": {},
                "help": "Reboot the NHD-CTL itself. The driver reconnects automatically once it returns.",
            },
            "refresh": {
                "label": "Refresh Status",
                "params": {},
                "help": "Re-read the endpoint roster, matrix assignments, endpoint status, and scene/layout lists.",
            },
            "raw_command": {
                "label": "Send Raw Command",
                "params": {
                    "command": {"type": "string", "required": True, "label": "Command",
                                "trim": False},
                },
                "help": "Escape hatch for anything in the NetworkHD API document. Returns the reply lines received within one second.",
            },
        },
    }

    # ── Lifecycle ──

    def __init__(self, device_id: str, config: dict[str, Any], state, events):
        self._request_lock = asyncio.Lock()
        self._pending: _PendingRequest | None = None
        # Parser state.
        self._json_mode: str | None = None
        self._json_buf: list[str] = []
        self._json_depth = 0
        self._json_started = False
        self._list_mode: str | None = None
        self._list_gather: list[str] = []
        self._serial_gather: dict[str, Any] | None = None
        self._json_in_string = False
        # child id <-> wire name maps.
        self._wire_by_child: dict[tuple[str, str], str] = {}
        self._child_by_name: dict[str, tuple[str, str]] = {}
        self._poll_cycle = 0
        self._roster_misses = 0
        super().__init__(device_id, config, state, events)

    def _transport_kwargs(self, transport_type: str, kwargs: dict) -> dict:
        # The Telnet API answers LF-terminated lines, not the platform's
        # bare-CR default.
        kwargs["delimiter"] = b"\n"
        return kwargs

    async def _post_connect(self) -> None:
        # Session setup runs before the device is reported connected: a
        # controller that never answers these aborts the attempt.
        host = self.config.get("host", "")
        port = int(self.config.get("port", 23))
        # Alias mode is per-session (reset on every new Telnet session),
        # so forcing it on never mutates persisted controller config. It
        # guarantees responses and notifications reference endpoints the
        # same way the roster does.
        await self._request("config set session alias on",
                            done=_mirror("config set session alias"))
        ok, _ = await self._request("config get version",
                                    done=lambda ln: _RE_SYS_VERSION.match(ln) is not None)
        if ok is None:
            raise ConnectionError(
                f"[{self.device_id}] No response from the NetworkHD API "
                f"on {host}:{port} — check this is the NHD-CTL "
                f"controller's address and its Telnet API is enabled"
            )
        ok, _ = await self._request("config get devicejsonstring",
                                    done_block="devicejson")
        if ok is None:
            raise ConnectionError(
                f"[{self.device_id}] The controller did not answer the "
                f"endpoint roster query (config get devicejsonstring)"
            )
        self._roster_misses = 0

    async def _initial_sync(self) -> None:
        # First full read; steady-state polling starts right after this.
        await self.poll()

    async def _close_session(self) -> None:
        # Runs on every teardown path: fail any in-flight request so its
        # awaiter doesn't hang on a dead link, and reset the stream parser
        # so the next session starts clean.
        pending = self._pending
        if pending is not None and not pending.future.done():
            pending.future.set_exception(ConnectionError("connection closed"))
        self._pending = None
        self._json_mode = None
        self._json_in_string = False
        self._list_mode = None
        self._serial_gather = None
        self._poll_cycle = 0

    # ── Serialized request/response ──

    async def _request(
        self,
        line: str,
        done: Callable[[str], bool] | None = None,
        done_block: str | None = None,
        timeout: float | None = None,
    ) -> tuple[bool | None, list[str]]:
        """Send one API line and await its completion.

        ``done`` is a per-line predicate (mirror echoes, ack lines, list
        headers); ``done_block`` names a JSON block tag that completes the
        request when its brackets balance. Returns (ok, lines): ok is True
        on completion, False if the controller answered 'unknown command'
        or a documented failure ack, None on timeout. Lines gathered while
        the request was pending are included (notifications excluded).
        """
        if timeout is None:
            timeout = REQUEST_TIMEOUT_S
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        async with self._request_lock:
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            pending = _PendingRequest(fut, done, done_block)
            self._pending = pending
            try:
                await self.transport.send((line + "\n").encode("utf-8"))
                try:
                    ok = await asyncio.wait_for(fut, timeout)
                except asyncio.TimeoutError:
                    return None, list(pending.lines)
                return ok, list(pending.lines)
            finally:
                if self._pending is pending:
                    self._pending = None

    def _complete_request(self, ok: bool, *, block: str | None = None,
                          line: str | None = None) -> None:
        pending = self._pending
        if pending is None or pending.future.done():
            return
        if block is not None:
            if pending.done_block == block:
                pending.future.set_result(ok)
            return
        if line is not None and pending.done is not None and pending.done(line):
            pending.future.set_result(ok)

    # ── Receiving ──

    async def on_data_received(self, data: bytes) -> None:
        line = data.decode("utf-8", errors="replace").rstrip("\r\n")
        stripped = line.strip()

        # Counted serial-data consume runs before everything else — the
        # payload may contain arbitrary text, including things that look
        # like headers.
        if self._serial_gather is not None:
            self._feed_serial_gather(line)
            return

        if not stripped:
            return

        # Notifications can arrive at any time, including between the lines
        # of a list block; they never complete a pending request.
        if stripped.startswith("notify "):
            self._handle_notify(stripped)
            return

        if self._json_mode is not None:
            self._feed_json(line)
            return

        lower = stripped.lower()
        for header, tag in _JSON_HEADERS.items():
            if lower.startswith(header):
                self._exit_list_mode()
                self._begin_json(tag, stripped[len(header):])
                return

        for header, tag in _LIST_HEADERS.items():
            if lower == header:
                self._exit_list_mode()
                self._list_mode = tag
                self._list_gather = []
                self._complete_request(True, line=stripped)
                return

        if self._list_mode is not None:
            if self._feed_list(self._list_mode, stripped):
                return
            self._exit_list_mode()
            # Fall through: this line belongs to whatever came next.

        if lower == "unknown command":
            # Completes ANY pending request as a failure, whatever its
            # completion matcher expected.
            self._pending_line(stripped)
            pending = self._pending
            if pending is not None and not pending.future.done():
                pending.future.set_result(False)
            return

        self._apply_line(stripped)
        self._pending_line(stripped)
        self._complete_request(True, line=stripped)

    def _exit_list_mode(self) -> None:
        if self._list_mode is not None:
            self._finish_list(self._list_mode)
            self._list_mode = None

    def _pending_line(self, line: str) -> None:
        pending = self._pending
        if pending is not None and not pending.future.done():
            pending.lines.append(line)

    # ── JSON block accumulation ──

    def _begin_json(self, tag: str, remainder: str) -> None:
        self._json_mode = tag
        self._json_buf = []
        self._json_depth = 0
        self._json_started = False
        self._json_in_string = False
        if remainder.strip():
            self._feed_json(remainder)

    def _feed_json(self, line: str) -> None:
        self._json_buf.append(line)
        in_string = getattr(self, "_json_in_string", False)
        escape = False
        for ch in line:
            if escape:
                escape = False
                continue
            if in_string:
                if ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch in "[{":
                self._json_depth += 1
                self._json_started = True
            elif ch in "]}":
                self._json_depth -= 1
        self._json_in_string = in_string
        if self._json_started and self._json_depth <= 0:
            tag = self._json_mode
            text = "\n".join(self._json_buf)
            self._json_mode = None
            self._json_buf = []
            self._json_in_string = False
            try:
                doc = json.loads(text)
            except (ValueError, TypeError) as exc:
                log.warning(f"[{self.device_id}] Unparseable {tag} JSON block: {exc}")
                self._complete_request(False, block=tag)
                return
            self._apply_json(tag, doc)
            self._complete_request(True, block=tag)

    def _apply_json(self, tag: str, doc: Any) -> None:
        if tag == "devicejson" and isinstance(doc, list):
            self._reconcile_roster(doc)
        elif tag == "deviceinfo" and isinstance(doc, dict):
            for entry in doc.get("devices", []) or []:
                self._apply_device_info(entry)
        elif tag == "devicestatus" and isinstance(doc, dict):
            for entry in doc.get("devices status", []) or []:
                self._apply_device_status(entry)

    # ── Roster ──

    def _reconcile_roster(self, entries: list[Any]) -> None:
        found: dict[str, dict[str, Any]] = {}  # wire name -> parsed entry
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            hostname = str(entry.get("trueName") or "").strip()
            alias = str(entry.get("aliasName") or "").strip()
            if alias.lower() == "null":
                alias = ""
            wire = alias or hostname
            if not wire:
                continue
            dtype = str(entry.get("deviceType") or "").strip().lower()
            ctype = "tx" if dtype.startswith("trans") else "rx"
            found[wire] = {
                "ctype": ctype,
                "alias": alias,
                "hostname": hostname,
                "online": bool(entry.get("online")),
                "ip": str(entry.get("ip") or ""),
                "tx_name": str(entry.get("txName") or "").strip(),
            }

        # Register new endpoints / update existing.
        seen_children: dict[str, set[str]] = {"tx": set(), "rx": set()}
        for wire, info in found.items():
            ctype = info["ctype"]
            key = self._child_by_name.get(wire.lower())
            if key is None or key[0] != ctype:
                cid = self._unique_child_id(ctype, wire)
                self.register_child(ctype, cid, initial_state={
                    "name": info["alias"] or info["hostname"],
                    "hostname": info["hostname"],
                    "model": _model_from_hostname(info["hostname"]),
                })
                self._map_child(ctype, cid, wire, info["hostname"])
                key = (ctype, cid)
            cid = key[1]
            seen_children[ctype].add(cid)
            updates: dict[str, Any] = {
                "online": info["online"],
                "name": info["alias"] or info["hostname"],
                "hostname": info["hostname"],
                "model": _model_from_hostname(info["hostname"]),
            }
            if info["ip"]:
                updates["ip"] = info["ip"]
            if ctype == "rx" and info["tx_name"]:
                updates["source"] = self._name_to_child_id(info["tx_name"])
            self.set_child_state_batch(ctype, cid, updates)

        # Deregister endpoints the controller no longer reports.
        for ctype in ("tx", "rx"):
            for cid in list(self.list_children(ctype)):
                if cid not in seen_children[ctype]:
                    self.deregister_child(ctype, cid)
                    wire = self._wire_by_child.pop((ctype, cid), None)
                    if wire is not None:
                        self._child_by_name = {
                            k: v for k, v in self._child_by_name.items()
                            if v != (ctype, cid)
                        }

        tx_infos = [i for i in found.values() if i["ctype"] == "tx"]
        rx_infos = [i for i in found.values() if i["ctype"] == "rx"]
        self.set_state("tx_count", len(tx_infos))
        self.set_state("tx_online", sum(1 for i in tx_infos if i["online"]))
        self.set_state("rx_count", len(rx_infos))
        self.set_state("rx_online", sum(1 for i in rx_infos if i["online"]))

        options = [
            {"value": wire, "label": (info["alias"] or info["hostname"])
             + (" (TX)" if info["ctype"] == "tx" else " (RX)")}
            for wire, info in sorted(found.items())
        ]
        self.set_state("endpoint_options", json.dumps(options))

    def _unique_child_id(self, ctype: str, wire: str) -> str:
        base = _sanitize_id(wire)
        cid = base
        n = 2
        existing = set(self.list_children(ctype))
        while cid in existing and self._wire_by_child.get((ctype, cid)) not in (None, wire):
            cid = f"{base}-{n}"
            n += 1
        return cid

    def _map_child(self, ctype: str, cid: str, wire: str, hostname: str) -> None:
        self._wire_by_child[(ctype, cid)] = wire
        self._child_by_name[wire.lower()] = (ctype, cid)
        if hostname:
            self._child_by_name[hostname.lower()] = (ctype, cid)

    def _name_to_child_id(self, name: str) -> str:
        """Wire name in a response -> child id (raw name if unknown)."""
        key = self._child_by_name.get(str(name).strip().lower())
        return key[1] if key else str(name).strip()

    def _wire_name(self, ctype: str, cid: str) -> str:
        wire = self._wire_by_child.get((ctype, str(cid).strip()))
        if wire is None:
            raise ValueError(
                f"Unknown {ctype.upper()} endpoint {cid!r} — pick an endpoint "
                f"from the dropdown (Refresh from Device updates the roster)"
            )
        return wire

    # ── Device info / status JSON fan-out ──

    def _child_for_entry(self, entry: dict[str, Any]) -> tuple[str, str] | None:
        for field in ("aliasname", "aliasName", "name"):
            value = str(entry.get(field) or "").strip()
            if value:
                key = self._child_by_name.get(value.lower())
                if key:
                    return key
        return None

    def _apply_device_info(self, entry: Any) -> None:
        if not isinstance(entry, dict):
            return
        key = self._child_for_entry(entry)
        if key is None:
            return
        ctype, cid = key
        updates: dict[str, Any] = {}
        if entry.get("ip4addr"):
            updates["ip"] = str(entry["ip4addr"])
        if entry.get("version"):
            updates["firmware"] = str(entry["version"])
        if ctype == "tx":
            if "video_input" in entry:
                updates["signal"] = entry.get("video_input") in (True, "true")
            if entry.get("video_source"):
                updates["video_port"] = str(entry["video_source"])
            # video_timing is NOT mapped to resolution — `config get device
            # status` is the canonical resolution source on every series.
        if updates:
            self.set_child_state_batch(ctype, cid, updates)

    def _apply_device_status(self, entry: Any) -> None:
        if not isinstance(entry, dict):
            return
        key = self._child_for_entry(entry)
        if key is None:
            return
        ctype, cid = key
        updates: dict[str, Any] = {}

        def _truthy(field: str) -> bool:
            return str(entry.get(field)).strip().lower() == "true" or entry.get(field) is True

        if ctype == "tx":
            if "hdmi in active" in entry:
                updates["signal"] = _truthy("hdmi in active")
            if entry.get("resolution"):
                updates["resolution"] = str(entry["resolution"])
            if entry.get("hdmi in frame rate"):
                updates["frame_rate"] = str(entry["hdmi in frame rate"])
            if entry.get("audio input format"):
                updates["audio_input_format"] = str(entry["audio input format"])
        else:
            if "hdmi out active" in entry:
                updates["hdmi_out_active"] = _truthy("hdmi out active")
            if entry.get("hdmi out resolution"):
                updates["hdmi_out_resolution"] = str(entry["hdmi out resolution"])
            if entry.get("hdmi out frame rate"):
                updates["hdmi_out_frame_rate"] = str(entry["hdmi out frame rate"])
            hdcp = entry.get("hdcp status") or entry.get("hdcp")
            if hdcp:
                updates["hdcp"] = str(hdcp)
        if updates:
            self.set_child_state_batch(ctype, cid, updates)

    # ── List blocks ──

    def _feed_list(self, tag: str, line: str) -> bool:
        """Try to consume one line of an open list block. Returns False if
        the line does not belong to the block (which ends it)."""
        if tag in ("av", "video", "audio", "audio2", "usb", "infrared", "serial"):
            m = _RE_PAIR.match(line)
            if not m:
                return False
            tx, rx = m.group(1), m.group(2)
            key = self._child_by_name.get(rx.lower())
            if key and key[0] == "rx":
                value = "" if tx.upper() == "NULL" else self._name_to_child_id(tx)
                prop = {"av": "source", "video": "video_source",
                        "audio": "audio_source", "usb": "usb_source"}.get(tag)
                if prop:
                    self.set_child_state_batch("rx", key[1], {prop: value})
            return True
        if tag == "audio3":
            # ARC list: "<RX> <TX>" — no child prop today; consume the lines.
            return _RE_PAIR.match(line) is not None
        if tag in ("infrared2", "serial2"):
            return _RE_MODE2.match(line) is not None
        if tag in ("scenes", "wscenes"):
            names = [n for n in line.split() if n]
            if not names:
                return False
            gather = getattr(self, "_list_gather", [])
            gather.extend(names)
            self._list_gather = gather
            return True
        if tag == "mscenes":
            parts = line.split()
            if len(parts) < 1:
                return False
            key = self._child_by_name.get(parts[0].lower())
            if key and key[0] == "rx":
                layouts = parts[1:]
                self.set_child_state_batch("rx", key[1], {"layouts": json.dumps(layouts)})
                gather = getattr(self, "_list_gather", [])
                gather.extend(layouts)
                self._list_gather = gather
                return True
            # A continuation or unknown RX — consume if it looks like names.
            return bool(parts) and not any(h in line.lower() for h in _LIST_HEADERS)
        if tag == "vw":
            if re.match(r"(?i)^row\s+\d+:", line):
                return True
            m = _RE_PAIR.match(line)
            if m and "-" in m.group(1) and "_" in m.group(1):
                gather = getattr(self, "_list_gather", [])
                gather.append(m.group(1))
                self._list_gather = gather
                return True
            return False
        if tag == "mview":
            # "<rx> tile|overlay <tx>:<x>_<y>_<w>_<h>:fit ..." — informational.
            return bool(_RE_PAIR.match(line) or ":" in line)
        return False

    def _finish_list(self, tag: str) -> None:
        gather = getattr(self, "_list_gather", [])
        if tag == "scenes":
            self.set_state("scene_options", json.dumps(
                [{"value": s, "label": s} for s in sorted(set(gather))]))
        elif tag == "wscenes":
            self.set_state("wscene_options", json.dumps(
                [{"value": s, "label": s} for s in sorted(set(gather))]))
        elif tag == "mscenes":
            self.set_state("mscene_options", json.dumps(
                [{"value": s, "label": s} for s in sorted(set(gather))]))
        elif tag == "vw":
            self.set_state("vw_options", json.dumps(
                [{"value": s, "label": s} for s in sorted(set(gather))]))
        self._list_gather = []

    # ── Single-line parsing ──

    def _apply_line(self, line: str) -> None:
        m = _RE_API_VERSION.match(line)
        if m:
            self.set_state("api_version", m.group(1))
            return
        m = _RE_SYS_VERSION.match(line)
        if m:
            self.set_state("system_version", m.group(1))
            return
        # Command mirrors that carry routing state: "matrix set <tx> <rx...>"
        # and the breakaway variants echo the command verbatim, so a
        # successful ack IS the assignment record.
        m = re.match(
            r"(?i)^matrix (?:(video|audio2|audio|usb) )?set\s+(\S+)((?:\s+\S+)+)$",
            line,
        )
        if m:
            stream = (m.group(1) or "av").lower()
            tx = m.group(2)
            prop = {"av": "source", "video": "video_source",
                    "audio": "audio_source", "usb": "usb_source"}.get(stream)
            if prop is None:
                return
            value = "" if tx.upper() == "NULL" else self._name_to_child_id(tx)
            for name in m.group(3).split():
                key = self._child_by_name.get(name.lower())
                if key and key[0] == "rx":
                    self.set_child_state_batch("rx", key[1], {prop: value})
            return

    # ── Notifications ──

    def _handle_notify(self, line: str) -> None:
        m = _RE_NOTIFY_ENDPOINT.match(line)
        if m:
            online = m.group(1) == "+"
            key = self._child_by_name.get(m.group(2).lower())
            if key:
                self.set_child_state_batch(key[0], key[1], {"online": online})
            return
        m = _RE_NOTIFY_VIDEO.match(line)
        if m:
            found = m.group(1).lower() == "found"
            key = self._child_by_name.get(m.group(2).lower())
            if key is None:
                return
            ctype, cid = key
            if ctype == "tx":
                self.set_child_state_batch("tx", cid, {"signal": found})
            else:
                self.set_child_state_batch("rx", cid, {"stream_active": found})
            return
        m = _RE_NOTIFY_CEC.match(line)
        if m:
            key = self._child_by_name.get(m.group(1).lower())
            if key:
                self.set_child_state_batch(key[0], key[1], {"cec_data": m.group(2)})
            return
        m = _RE_NOTIFY_IR.match(line)
        if m:
            key = self._child_by_name.get(m.group(1).lower())
            if key:
                self.set_child_state_batch(key[0], key[1], {"ir_data": m.group(2)})
            return
        m = _RE_NOTIFY_SERIAL.match(line)
        if m:
            self._serial_gather = {
                "name": m.group(1),
                "format": m.group(2).lower(),
                "length": int(m.group(3)),
                "parts": [],
                "count": 0,
            }
            first = m.group(4)
            if first or self._serial_gather["length"] == 0:
                self._feed_serial_gather(first)
            return
        log.debug(f"[{self.device_id}] Unhandled notification: {line}")

    def _feed_serial_gather(self, line: str) -> None:
        gather = self._serial_gather
        if gather is None:
            return
        if gather["parts"]:
            gather["count"] += 2  # the CRLF between payload lines counts
        gather["parts"].append(line)
        gather["count"] += len(line)
        if gather["count"] >= gather["length"] or gather["count"] >= SERIALINFO_MAX_CHARS:
            data = "\r\n".join(gather["parts"])
            key = self._child_by_name.get(str(gather["name"]).lower())
            if key:
                self.set_child_state_batch(key[0], key[1], {"serial_data": data})
            self._serial_gather = None

    # ── Polling ──

    async def poll(self) -> None:
        """Roster + matrix + status every cycle; breakaway views, device
        info, and scene/layout lists on the first and every 6th cycle.

        Raises ConnectionError after two consecutive unanswered roster
        queries so the platform flips the device offline — the TCP socket
        to a hung controller can stay open indefinitely (never-offline
        guard).
        """
        ok, _ = await self._request("config get devicejsonstring",
                                    done_block="devicejson")
        if ok is None:
            self._roster_misses += 1
            if self._roster_misses >= MAX_POLL_MISSES:
                raise ConnectionError(
                    f"[{self.device_id}] The controller is not responding to "
                    f"API queries ({self._roster_misses} consecutive misses)"
                )
            return
        self._roster_misses = 0

        await self._request("matrix get", done=_header("matrix information:"))
        await self._request("config get device status", done_block="devicestatus")

        if self._poll_cycle % FULL_REFRESH_EVERY == 0:
            await self._request("config get version",
                                done=lambda ln: _RE_SYS_VERSION.match(ln) is not None)
            await self._request("config get device info", done_block="deviceinfo")
            await self._request("matrix video get",
                                done=_header("matrix video information:"))
            await self._request("matrix audio get",
                                done=_header("matrix audio information:"))
            await self._request("matrix usb get",
                                done=_header("matrix usb information:"))
            await self._request("scene get", done=_header("scene list:"))
            await self._request("wscene2 get", done=_header("wscene2 list:"))
            await self._request("mscene get", done=_header("mscene list:"))
            await self._request("vw get", done=_header("video wall information:"))
            # List blocks have no terminator; give the last one a beat to
            # stream in before the cycle ends so _finish_list publishes it.
            await asyncio.sleep(0.2)
            if self._list_mode is not None:
                self._finish_list(self._list_mode)
                self._list_mode = None
        self._poll_cycle += 1

    async def refresh_children(self) -> dict[str, Any]:
        self._poll_cycle = 0  # force the full refresh path
        await self.poll()
        return {
            "encoders": len(self.list_children("tx")),
            "decoders": len(self.list_children("rx")),
        }

    # ── Commands ──

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}

        route_map = {
            "route": "matrix set",
            "route_video": "matrix video set",
            "route_audio": "matrix audio set",
            "route_audio_analog": "matrix audio2 set",
            "route_usb": "matrix usb set",
            "route_infrared": "matrix infrared set",
            "route_serial": "matrix serial set",
        }
        if command in route_map:
            tx = self._wire_name("tx", params["tx"])
            rx = self._wire_name("rx", params["rx"])
            return await self._simple(command, f"{route_map[command]} {tx} {rx}")

        route_off_map = {
            "route_off": "matrix set",
            "route_video_off": "matrix video set",
            "route_audio_off": "matrix audio set",
            "route_audio_analog_off": "matrix audio2 set",
            "route_usb_off": "matrix usb set",
            "route_infrared_off": "matrix infrared set",
            "route_serial_off": "matrix serial set",
        }
        if command in route_off_map:
            rx = self._wire_name("rx", params["rx"])
            return await self._simple(command, f"{route_off_map[command]} null {rx}")

        if command == "route_arc":
            rx = self._wire_name("rx", params["rx"])
            tx = self._wire_name("tx", params["tx"])
            return await self._simple(command, f"matrix audio3 set {rx} {tx}")

        if command in ("set_infrared_mode", "set_serial_mode"):
            verb = "infrared2" if command == "set_infrared_mode" else "serial2"
            endpoint = str(params["endpoint"]).strip()
            mode = str(params["mode"]).strip().lower()
            target = str(params.get("target") or "").strip()
            line = f"matrix {verb} set {endpoint} {mode}"
            if mode == "single":
                if not target:
                    raise ValueError(f"{command}: single mode needs a target endpoint")
                line += f" {target}"
            return await self._simple(command, line)

        if command == "set_video_port":
            tx = self._wire_name("tx", params["tx"])
            return await self._simple(
                command, f"config set device videosource {tx} {params['port']}")

        if command == "set_audio_source":
            rx = self._wire_name("rx", params["rx"])
            return await self._simple(
                command, f"config set device audiosource {rx} {params['source']}")

        if command == "set_analog_audio_source":
            rx = self._wire_name("rx", params["rx"])
            return await self._simple(
                command, f"config set device audio2source {rx} {params['source']}")

        if command == "set_audio_input_type":
            tx = self._wire_name("tx", params["tx"])
            return await self._simple(
                command, f"config set device audio input type {params['source']} {tx}")

        if command == "set_dante_audio_input":
            tx = self._wire_name("tx", params["tx"])
            return await self._simple(
                command,
                f"config set device info dante.audio_input={params['source']} {tx}")

        if command == "set_usb_mode":
            endpoint = str(params["endpoint"]).strip()
            return await self._simple(
                command,
                f"config set device info km_over_ip_enable={_on_off(params.get('kmoip'))} {endpoint}")

        if command == "sink_power":
            rx = self._wire_name("rx", params["rx"])
            return await self._simple(
                command, f"config set device sinkpower {params['power']} {rx}")

        if command == "cec_power":
            rx = self._wire_name("rx", params["rx"])
            return await self._simple(
                command, f"config set device cec {params['power']} {rx}")

        if command == "send_cec":
            rx = self._wire_name("rx", params["rx"])
            data = str(params["data"]).strip()
            return await self._simple(command, f'cec "{data}" {rx}')

        if command == "send_infrared":
            endpoint = str(params["endpoint"]).strip()
            data = str(params["data"]).strip()
            return await self._simple(command, f'infrared "{data}" {endpoint}')

        if command == "send_serial":
            endpoint = str(params["endpoint"]).strip()
            baud = str(params["baud"]).strip()
            bits = str(params.get("data_bits") or "8").strip()
            parity = str(params.get("parity") or "n").strip().lower()
            stop = str(params.get("stop_bits") or "1").strip()
            cr = _on_off(params.get("append_cr"))
            lf = _on_off(params.get("append_lf"))
            hexfmt = _on_off(params.get("hex"))
            data = str(params["data"])
            line = (f"serial -b {baud}-{bits}{parity}{stop} -r {cr} -n {lf} "
                    f'-h {hexfmt} "{data}" {endpoint}')
            return await self._simple(command, line)

        if command == "set_volume":
            endpoint = str(params["endpoint"]).strip()
            return await self._simple(
                command,
                f"config set device audio volume {params['action']} analog {endpoint}")

        if command == "scene_recall":
            scene = str(params["scene"]).strip()
            ok, lines = await self._request(
                f"scene active {scene}",
                done=lambda ln: re.match(r"(?i)^scene\s+\S+\s+active\s+(success|failure)", ln) is not None)
            return self._ack_result(command, ok, lines)

        if command == "scene_set_source":
            scene = str(params["scene"]).strip()
            tx = self._wire_name("tx", params["tx"])
            return await self._simple(
                command,
                f"scene set {scene} {int(params['x'])} {int(params['y'])} {tx}",
                done=_mirror("scene "))

        if command == "vw_change":
            lscreen = str(params["lscreen"]).strip()
            tx = self._wire_name("tx", params["tx"])
            return await self._simple(command, f"vw change {lscreen} {tx}",
                                      done=_mirror("videowall "))

        if command == "wscene_recall":
            wscene = str(params["wscene"]).strip()
            return await self._wscene(command, f"wscene2 active {wscene}")

        if command == "window_open":
            return await self._wscene(command, (
                f"wscene2 window open {str(params['wscene']).strip()} "
                f"{str(params['window']).strip()} {int(params['x'])} {int(params['y'])} "
                f"{int(params['width'])} {int(params['height'])} "
                f"{self._wire_name('tx', params['tx'])}"))

        if command == "window_close":
            return await self._wscene(command, (
                f"wscene2 window close {str(params['wscene']).strip()} "
                f"{str(params['window']).strip()}"))

        if command == "window_change":
            return await self._wscene(command, (
                f"wscene2 window change {str(params['wscene']).strip()} "
                f"{str(params['window']).strip()} {self._wire_name('tx', params['tx'])}"))

        if command == "window_adjust":
            return await self._wscene(command, (
                f"wscene2 window adjust {str(params['wscene']).strip()} "
                f"{str(params['window']).strip()} {int(params['x'])} {int(params['y'])} "
                f"{int(params['width'])} {int(params['height'])}"))

        if command == "window_layer":
            return await self._wscene(command, (
                f"wscene2 window move {str(params['wscene']).strip()} "
                f"{str(params['window']).strip()} {str(params['layer']).strip()}"))

        if command == "mview_layout":
            rx = self._wire_name("rx", params["rx"])
            layout = str(params["layout"]).strip()
            return await self._mview_ack(command, f"mscene active {rx} {layout}")

        if command == "mview_tile_source":
            rx = self._wire_name("rx", params["rx"])
            layout = str(params["layout"]).strip()
            tx = self._wire_name("tx", params["tx"])
            return await self._mview_ack(
                command, f"mscene change {rx} {layout} {int(params['tile'])} {tx}")

        if command == "mview_audio":
            rx = self._wire_name("rx", params["rx"])
            layout = str(params["layout"]).strip()
            mode = str(params["mode"]).strip().lower()
            target = str(params["target"]).strip()
            return await self._mview_ack(
                command, f"mscene set audio {rx} {layout} {mode} {target}")

        if command == "mview_single":
            rx = self._wire_name("rx", params["rx"])
            tx = self._wire_name("tx", params["tx"])
            return await self._mview_ack(command, f"mview set {rx} {tx}")

        if command == "mview_custom":
            rx = self._wire_name("rx", params["rx"])
            mode = str(params["mode"]).strip().lower()
            tiles = str(params["tiles"]).strip()
            return await self._mview_ack(command, f"mview set {rx} {mode} {tiles}")

        if command == "mview_custom_audio":
            rx = self._wire_name("rx", params["rx"])
            tx = self._wire_name("tx", params["tx"])
            return await self._mview_ack(command, f"mview set audio {rx} separate {tx}")

        if command == "osd_text":
            tx = self._wire_name("tx", params["tx"])
            text = str(params["text"])
            # The documented OSD ack is the mirror with a "receive: " prefix.
            return await self._simple(command, (
                f"config set device osd param {text} {int(params.get('x') or 0)} "
                f"{int(params.get('y') or 0)} {str(params.get('color') or 'FFFFFFFF').strip()} "
                f"{int(params.get('size') or 1)} {tx}"),
                done=_osd_ack)

        if command in ("osd_on", "osd_off"):
            tx = self._wire_name("tx", params["tx"])
            state = "on" if command == "osd_on" else "off"
            return await self._simple(command, f"config set device osd {state} {tx}",
                                      done=_osd_ack)

        if command == "cec_notify":
            scope = str(params["scope"]).strip()
            return await self._simple(
                command,
                f"config set device cec notify {_on_off(params.get('enable'))} {scope}")

        if command == "reboot_device":
            endpoint = str(params["endpoint"]).strip()
            ok, lines = await self._request(
                f"config set device reboot {endpoint}",
                done=lambda ln: "will reboot now" in ln.lower())
            return self._ack_result(command, ok, lines)

        if command == "reboot_controller":
            ok, lines = await self._request(
                "config set reboot",
                done=lambda ln: "will reboot now" in ln.lower())
            return self._ack_result(command, ok, lines)

        if command == "refresh":
            await self.refresh_children()
            return True

        if command == "raw_command":
            raw = str(params.get("command", "")).strip()
            # No completion matcher — gather whatever single-line replies
            # arrive within a second. Block replies (JSON, list dumps) are
            # folded into state as usual rather than returned.
            _ok, lines = await self._request(raw, done=lambda ln: False, timeout=1.0)
            return "\n".join(lines)

        log.warning(f"[{self.device_id}] Unknown command: {command}")
        return None

    async def _simple(self, command: str, line: str,
                      done: Callable[[str], bool] | None = None) -> bool | None:
        """Send a command whose success ack is its own mirror echo."""
        if done is None:
            token = line.split('"', 1)[0].split()[0]
            done = _mirror(token)
        ok, lines = await self._request(line, done=done)
        return self._ack_result(command, ok, lines)

    async def _wscene(self, command: str, line: str) -> bool | None:
        ok, lines = await self._request(
            line,
            done=lambda ln: ln.lower().startswith("wscene2 ")
            and (ln.lower().endswith(" success") or ln.lower().endswith(" failure")
                 or "unknown" in ln.lower()))
        if ok and lines and lines[-1].lower().endswith(" failure"):
            ok = False
        return self._ack_result(command, ok, lines)

    async def _mview_ack(self, command: str, line: str) -> bool | None:
        ok, lines = await self._request(
            line,
            done=lambda ln: (ln.lower().startswith(("mscene ", "mview set"))
                             and ln.lower().rsplit(" ", 1)[-1] in ("success", "failure")))
        if ok and lines and lines[-1].lower().endswith("failure"):
            ok = False
        return self._ack_result(command, ok, lines)

    def _ack_result(self, command: str, ok: bool | None, lines: list[str]) -> bool | None:
        if ok is False or (ok and lines and lines[-1].lower().endswith("failure")):
            log.warning(
                f"[{self.device_id}] {command}: controller answered "
                f"{lines[-1] if lines else 'unknown command'!r}")
            return False
        if ok is None:
            log.warning(f"[{self.device_id}] {command}: no reply from controller")
        return ok


class _PendingRequest:
    __slots__ = ("future", "done", "done_block", "lines")

    def __init__(self, future: asyncio.Future,
                 done: Callable[[str], bool] | None,
                 done_block: str | None) -> None:
        self.future = future
        self.done = done
        self.done_block = done_block
        self.lines: list[str] = []


def _mirror(prefix: str) -> Callable[[str], bool]:
    prefix = prefix.lower()
    return lambda line: line.lower().startswith(prefix)


def _osd_ack(line: str) -> bool:
    return re.match(r"(?i)^(?:receive:\s*)?config set device osd\b", line) is not None


def _header(header: str) -> Callable[[str], bool]:
    header = header.lower()
    return lambda line: line.lower() == header
