"""
OpenAVC vMix Driver.

Controls vMix video production software via its TCP API (port 8099).
Covers transitions, input switching, audio, overlays, recording, streaming,
titles, replay and PTZ.

vMix TCP API reference:
https://www.vmix.com/help29/TCPAPI.html

Protocol overview:
- Commands: FUNCTION <name> [QueryString]\r\n
- Responses: FUNCTION OK <msg>\r\n  or  FUNCTION ER <msg>\r\n
- XML state: XML <length>\r\n<UTF-8 XML body>   (length INCLUDES the trailing CRLF)
- Push: SUBSCRIBE TALLY -> TALLY OK <digits>\r\n on every tally change
- Push: SUBSCRIBE ACTS  -> ACTS OK <Activator> [<input>] <value>\r\n on every change
- vMix greets a new connection with VERSION OK <version>\r\n before anything is asked.

Mixed framing: normal messages are CRLF-delimited text; an XML response is a
length-prefixed binary body after its header line, so the driver supplies its
own frame parser rather than a delimiter.

Why Python rather than YAML: the mixed length-prefixed/line framing, the ACTS
event stream that fans one line out into per-input child state, and the volume
scale conversion below all sit outside what ConfigurableDriver expresses.

Push, not polling. vMix pushes both tally and activator events, so the XML poll
is only a periodic reseed (it also carries the things ACTS never sends -- input
titles, types, the transition slots). ACTS sends no snapshot when you subscribe,
which is why the poll still runs.

A note on volume, because the vendor doc is misleading and the trap is silent.
Every write function ("SetVolume", "SetMasterVolume") takes the FADER position
0-100 -- the position of the slider in the vMix audio mixer. Every value vMix
REPORTS back, in the XML and over ACTS, is the resulting linear amplitude, and
the two are not the same number: amplitude = (fader / 100) ** 4 * 100, measured
against vMix 29 at 0/10/25/50/75/90/100 and exact at every point. Writing 50 and
reading back 6.25 makes a panel fader jump under the operator's finger, so this
driver publishes volume on the fader scale -- the scale you write, and the one
the vMix window shows -- and converts what the device reports.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, Optional

from openavc.core.connection_fault import ConnectionFaultError
from openavc.drivers.base import BaseDriver
from openavc.transport.frame_parsers import CallableFrameParser, FrameParser
from openavc.utils.logger import get_logger

log = get_logger(__name__)

# Sentinel prefix for XML body messages from the frame parser
_XML_BODY_PREFIX = b"XML_BODY "

# vMix has eight overlay channels. The XML <overlays> node lists sixteen, but
# only 1-8 are addressable: they are the only ones with OverlayInput<N>
# functions and the only ones the ACTS activator vocabulary accepts (Overlay9
# is rejected with "ACTS ER Invalid Activator"). Publishing the other eight
# would be eight state keys nothing can read or write.
OVERLAY_CHANNELS = 8

# The transition effects vMix accepts. Each one is itself a shortcut function --
# a named transition is invoked as FUNCTION <Effect>, not as a Value on some
# generic "Transition" function (there is no such function).
TRANSITION_EFFECTS = [
    "Fade", "Zoom", "Wipe", "Slide", "Fly", "CrossZoom", "FlyRotate", "Cube",
    "CubeZoom", "VerticalWipe", "VerticalSlide", "Merge", "WipeReverse",
    "SlideReverse", "VerticalWipeReverse", "VerticalSlideReverse",
]

# The audio busses vMix mixes to. M is the master bus, and an input can be
# routed to any of them. The bus masters themselves are not symmetrical: vMix
# takes a volume for A-G but only has mute/toggle functions for A and B.
AUDIO_BUSSES = ["M", "A", "B", "C", "D", "E", "F", "G"]
MIXABLE_BUSSES = ["A", "B", "C", "D", "E", "F", "G"]
MUTABLE_BUSSES = ["A", "B"]


def _parse_vmix_frame(buffer: bytes) -> tuple[bytes | None, bytes]:
    """
    Parse vMix TCP frames from a byte buffer.

    Normal messages: delimited by CRLF (\\r\\n).
    XML responses: "XML <length>\\r\\n" header followed by <length> bytes of body.

    Returns (message, remaining) or (None, buffer) if incomplete.
    """
    # Need at least a CRLF to have any complete message
    crlf_pos = buffer.find(b"\r\n")
    if crlf_pos == -1:
        return None, buffer

    line = buffer[:crlf_pos]

    # Check if this is an XML length-prefixed response
    if line.startswith(b"XML "):
        try:
            xml_len = int(line[4:])
        except ValueError:
            # Not a valid XML length — treat as normal message
            remaining = buffer[crlf_pos + 2:]
            return line, remaining

        # Need the full XML body after the header line + CRLF. vMix counts the
        # trailing CRLF in the length, so the body arrives with it attached;
        # the XML parser ignores trailing whitespace.
        body_start = crlf_pos + 2
        body_end = body_start + xml_len
        if len(buffer) < body_end:
            return None, buffer  # Wait for more data

        xml_body = buffer[body_start:body_end]
        remaining = buffer[body_end:]
        # Tag the XML body so the router can identify it
        return _XML_BODY_PREFIX + xml_body, remaining

    # Normal CRLF-delimited message
    remaining = buffer[crlf_pos + 2:]
    return line, remaining


def amplitude_to_fader(amplitude: float) -> float:
    """Convert a vMix-reported amplitude (0-100) to a fader position (0-100).

    The inverse of what vMix applies to a written volume. See the module
    docstring: the write scale and the read scale differ by a fourth power, and
    every surface in OpenAVC works in the write scale so a bound fader holds
    still where the operator left it.
    """
    if amplitude <= 0:
        return 0.0
    return round((min(amplitude, 100.0) / 100.0) ** 0.25 * 100.0, 1)


def unit_to_fader(value: float) -> float:
    """Same conversion for the 0-1 amplitude ACTS reports instead of 0-100."""
    return amplitude_to_fader(max(0.0, min(value, 1.0)) * 100.0)


class VMixDriver(BaseDriver):
    """vMix video production software driver via TCP API."""

    # vMix pushes, so the control link is receive-mostly and a dead host does
    # not necessarily produce a FIN. XMLTEXT on a value that always exists is
    # the cheapest thing that proves the far end is still answering.
    HEALTH_INTERVAL_S = 30.0
    HEALTH_TIMEOUT_S = 5.0
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = (
        "Connected, but vMix stopped answering. The PC may be asleep or vMix closed."
    )

    DRIVER_INFO = {
        "id": "vmix",
        "name": "vMix",
        "manufacturer": "StudioCoast",
        "category": "video",
        "version": "2.0.0",
        # The connection lifecycle hooks this driver overrides landed in 0.24.0.
        "min_platform_version": "0.25.0",
        "author": "OpenAVC",
        "description": (
            "Controls vMix video production software via the TCP API. "
            "Supports transitions, input switching, audio, overlays, "
            "recording, streaming, titles, replay, and PTZ."
        ),
        "source_url": "https://www.vmix.com/help29/TCPAPI.html",
        "tags": ["video-production", "streaming", "switcher", "software"],
        "verified": True,
        "simulated": True,
        "ports": [8099],
        "discovery": {
            # vMix is software on a Windows PC — no vendor OUI, no broadcast,
            # no mDNS service. What it does do is speak first: a bare TCP
            # connect to 8099 is answered with "VERSION OK <version>" before
            # anything is sent. That is a banner-read fingerprint, so the scan
            # can identify vMix outright instead of listing an open port.
            "tcp_probe": {
                "port": 8099,
                "expect_regex": r"^VERSION OK \d+\.\d+",
                "timeout_ms": 2000,
                "extract": {
                    "firmware_version": {"regex": r"^VERSION OK (\S+)", "group": 1},
                },
            },
            "port_open": [8099],
            "manufacturer_alias": ["vmix", "studiocoast"],
        },
        "compatible_models": [
            {
                "manufacturer": "StudioCoast",
                "models": ["vMix"],
                "confidence": "full",
                "notes": (
                    "Verified against vMix 29 (29.0.0.49). Any vMix edition; "
                    "the TCP API is enabled by default. Bus and Mix features "
                    "beyond the master bus need a 4K or Pro edition."
                ),
            },
        ],
        "transport": "tcp",
        "help": {
            "overview": (
                "Full control of vMix video production software. Supports "
                "input switching, transitions, audio mixing, overlays, "
                "recording/streaming, titles, replay, and PTZ cameras. "
                "Program, preview, tally, overlays, recording and audio "
                "update the moment they change in vMix rather than waiting "
                "for the next poll."
            ),
            "setup": (
                "1. Open vMix and go to Settings > Web Controller\n"
                "2. Ensure the TCP API is enabled (default port 8099)\n"
                "3. Enter the vMix PC's IP address and port below\n"
                "4. Leave both subscriptions on for live tally and live "
                "recording/overlay/audio state"
            ),
        },
        "default_config": {
            "host": "",
            "port": 8099,
            "poll_interval": 30,
            "subscribe_tally": True,
            "subscribe_acts": True,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
                "description": "IP address of the PC running vMix",
            },
            "port": {
                "type": "integer",
                "default": 8099,
                "label": "Port",
                "description": "vMix TCP API port (default 8099)",
            },
            "poll_interval": {
                "type": "integer",
                "default": 30,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "How often to request full XML state (0 to disable). Input "
                    "titles and types only arrive this way; everything else is "
                    "pushed, so this can stay slow."
                ),
            },
            "subscribe_tally": {
                "type": "boolean",
                "default": True,
                "label": "Subscribe to Tally",
                "description": "Real-time program/preview tally for every input",
            },
            "subscribe_acts": {
                "type": "boolean",
                "default": True,
                "label": "Subscribe to Activators",
                "description": (
                    "Real-time overlay, recording, streaming, fade-to-black and "
                    "audio state. Turn off only if the event volume is a problem."
                ),
            },
        },
        "state_variables": {
            "active": {
                "type": "integer", "label": "Program Input",
                "cloud_priority": "high", "control": True,
            },
            "preview": {
                "type": "integer", "label": "Preview Input",
                "cloud_priority": "high", "control": True,
            },
            "recording": {
                "type": "boolean", "label": "Recording", "cloud_priority": "high",
            },
            "streaming": {
                "type": "boolean", "label": "Streaming", "cloud_priority": "high",
            },
            "external": {"type": "boolean", "label": "External Output"},
            "multicorder": {"type": "boolean", "label": "MultiCorder"},
            "fullscreen": {"type": "boolean", "label": "Fullscreen Output"},
            "playlist": {"type": "boolean", "label": "Playlist Running"},
            "fade_to_black": {
                "type": "boolean", "label": "Fade to Black", "cloud_priority": "high",
            },
            "input_count": {"type": "integer", "label": "Input Count"},
            "input_list": {
                "type": "string",
                "label": "Input List",
                "help": (
                    "JSON list of the production's current inputs "
                    "(number + title), rebuilt on every state poll. Feeds "
                    "the input dropdowns on this driver's commands."
                ),
            },
            "version": {"type": "string", "label": "vMix Version"},
            "edition": {"type": "string", "label": "vMix Edition"},
            "last_error": {
                "type": "string", "label": "Last Error",
                "help": "The most recent error message vMix returned for a command.",
            },
            # Master audio. Volume is the fader position, matching what
            # set_master_volume writes — see the module docstring.
            "master_volume": {
                "type": "number", "label": "Master Volume",
                "min": 0, "max": 100, "step": 1, "unit": "%", "control": True,
            },
            "master_muted": {
                "type": "boolean", "label": "Master Muted", "control": True,
            },
            "master_headphones_volume": {
                "type": "number", "label": "Headphones Volume",
                "min": 0, "max": 100, "step": 1, "unit": "%", "control": True,
            },
            # Bus A/B. vMix only reports these once the busses are enabled in
            # its audio settings, so on a stock install they simply stay unset.
            "bus_a_volume": {
                "type": "number", "label": "Bus A Volume",
                "min": 0, "max": 100, "step": 1, "unit": "%", "control": True,
            },
            "bus_a_muted": {"type": "boolean", "label": "Bus A Muted", "control": True},
            "bus_b_volume": {
                "type": "number", "label": "Bus B Volume",
                "min": 0, "max": 100, "step": 1, "unit": "%", "control": True,
            },
            "bus_b_muted": {"type": "boolean", "label": "Bus B Muted", "control": True},
            # Replay, pushed by the activators of the same name.
            "replay_recording": {"type": "boolean", "label": "Replay Recording"},
            "replay_live": {"type": "boolean", "label": "Replay Live"},
            "replay_playing": {"type": "boolean", "label": "Replay Playing"},
            # Overlay channels: the input number showing on that channel, 0 when
            # the channel is off. Eight of them, which is what vMix addresses.
            **{
                f"overlay.{ch}": {
                    "type": "integer", "label": f"Overlay {ch} Input",
                    "cloud_priority": "high",
                }
                for ch in range(1, OVERLAY_CHANNELS + 1)
            },
            # The four transition buttons under the preview window.
            **{
                key: value
                for ch in range(1, 5)
                for key, value in (
                    (f"transition.{ch}.effect",
                     {"type": "string", "label": f"Transition {ch} Effect"}),
                    (f"transition.{ch}.duration",
                     {"type": "integer", "label": f"Transition {ch} Duration",
                      "unit": "ms"}),
                )
            },
        },
        # A production's inputs are discovered at runtime and change while the
        # show is live, which is what child entities are for: the roster is
        # registered from the XML state (and from the tally string, which can
        # arrive first), and an input removed in vMix is deregistered so its
        # state doesn't linger. Declared with no id padding, so every key keeps
        # the shape it has always had: device.<id>.input.<number>.<prop>.
        "child_entity_types": {
            "input": {
                "label": "Input",
                "label_plural": "Inputs",
                "id_format": {"type": "integer", "min": 1, "max": 1000},
                "state_variables": {
                    "title": {"type": "string", "label": "Title"},
                    "short_title": {"type": "string", "label": "Short Title"},
                    "key": {
                        "type": "string", "label": "Key",
                        "help": (
                            "vMix's own GUID for this input. Survives inputs "
                            "being reordered, which the number does not."
                        ),
                    },
                    "type": {"type": "string", "label": "Type"},
                    "state": {"type": "string", "label": "State"},
                    "playing": {"type": "boolean", "label": "Playing"},
                    "loop": {"type": "boolean", "label": "Loop"},
                    "position": {"type": "integer", "label": "Position", "unit": "ms"},
                    "duration": {"type": "integer", "label": "Duration", "unit": "ms"},
                    "selected_index": {"type": "integer", "label": "Selected Index"},
                    "tally": {
                        "type": "integer",
                        "label": "Tally",
                        "min": 0,
                        "max": 2,
                        "cloud_priority": "high",
                        "help": "0 = safe, 1 = program, 2 = preview.",
                    },
                    # Audio. Only inputs that carry audio report these at all,
                    # so on a Colour or Blank input they stay unset.
                    "muted": {
                        "type": "boolean", "label": "Muted",
                        "control": True, "cloud_priority": "high",
                    },
                    "volume": {
                        "type": "number", "label": "Volume",
                        "min": 0, "max": 100, "step": 1, "unit": "%",
                        "control": True, "cloud_priority": "low",
                        "help": (
                            "Fader position, the same scale set_volume takes. "
                            "vMix reports amplitude internally; this is converted."
                        ),
                    },
                    "balance": {
                        "type": "number", "label": "Balance",
                        "min": -1, "max": 1, "step": 0.1, "control": True,
                    },
                    "gain_db": {
                        "type": "number", "label": "Gain",
                        "min": 0, "max": 24, "step": 1, "unit": "dB",
                    },
                    "solo": {"type": "boolean", "label": "Solo"},
                    "solo_pfl": {"type": "boolean", "label": "Solo PFL"},
                    "audio_busses": {
                        "type": "string", "label": "Audio Busses",
                        "help": "Which busses this input feeds, e.g. \"M,A\".",
                    },
                    "headphones_volume": {
                        "type": "number", "label": "Headphones Volume",
                        "min": 0, "max": 100, "step": 1, "unit": "%",
                        "cloud_priority": "low",
                    },
                },
                "summary_fields": ["title", "type", "tally"],
                "label_field": "title",
            },
        },
        # Quick Action strip: the daily production surface. The record /
        # stream pairs swap on live state so the button always shows the
        # next action; the stops confirm because they end a live capture.
        "actions": [
            {
                "id": "start_recording", "kind": "command", "icon": "circle",
                "visible_when": {"key": "device.$id.recording", "operator": "falsy"},
            },
            {
                "id": "stop_recording", "kind": "command", "icon": "square",
                "visible_when": {"key": "device.$id.recording", "operator": "truthy"},
                "confirm": "Stop the current recording?",
            },
            {
                "id": "start_streaming", "kind": "command", "icon": "radio",
                "visible_when": {"key": "device.$id.streaming", "operator": "falsy"},
            },
            {
                "id": "stop_streaming", "kind": "command", "icon": "square",
                "visible_when": {"key": "device.$id.streaming", "operator": "truthy"},
                "confirm": "Stop the live stream?",
            },
            {"id": "fade_to_black", "kind": "command", "icon": "eye-off"},
            {"id": "overlay_all_off", "kind": "command", "icon": "layers"},
        ],
        "commands": {
            # --- Transitions ---
            "cut": {
                "label": "Cut",
                "params": {"input": {"type": "string", "options_state": "input_list", "help": "Input number or name (optional, omit for current preview)"}},
                "help": "Instant cut transition to the specified input or current preview.",
            },
            "fade": {
                "label": "Fade",
                "params": {
                    "input": {"type": "string", "options_state": "input_list", "help": "Input number or name (optional)"},
                    "duration": {"type": "integer", "min": 0, "max": 60000, "help": "Fade duration in milliseconds"},
                },
                "help": "Fade transition to the specified input.",
            },
            "cut_direct": {
                "label": "Cut Direct",
                "params": {"input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"}},
                "help": "Cut directly to an input without changing preview.",
            },
            "fade_to_black": {
                "label": "Fade to Black",
                "params": {},
                "help": "Toggle fade to black on program output.",
            },
            "transition": {
                "label": "Transition",
                "params": {
                    "effect": {
                        "type": "enum", "required": True, "values": TRANSITION_EFFECTS,
                        "help": "Transition effect to use",
                    },
                    "input": {"type": "string", "options_state": "input_list", "help": "Input number or name"},
                    "duration": {"type": "integer", "min": 0, "max": 60000, "help": "Duration in milliseconds"},
                },
                "help": "Transition to an input using a named effect.",
            },
            "transition_button": {
                "label": "Transition Button",
                "params": {
                    "number": {"type": "integer", "required": True, "min": 1, "max": 4, "help": "Transition button 1-4"},
                },
                "help": "Fire one of the four transition buttons as configured in vMix.",
            },
            "stinger": {
                "label": "Stinger",
                "params": {
                    "number": {"type": "integer", "required": True, "min": 1, "max": 8, "help": "Stinger 1-8"},
                    "input": {"type": "string", "options_state": "input_list", "help": "Input number or name"},
                },
                "help": "Play a stinger transition to an input.",
            },
            "set_fader": {
                "label": "Set T-Bar",
                "params": {"position": {"type": "integer", "required": True, "min": 0, "max": 255, "help": "T-bar position 0-255"}},
                "help": "Set the transition T-bar position (0=full A, 255=full B).",
            },
            "quick_play": {
                "label": "Quick Play",
                "params": {"input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"}},
                "help": "Send an input straight to program and start playing it.",
            },
            # --- Input Switching ---
            "preview_input": {
                "label": "Preview Input",
                "params": {"input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"}},
                "help": "Send an input to preview.",
            },
            "active_input": {
                "label": "Active Input",
                "params": {"input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"}},
                "help": "Send an input directly to program output.",
            },
            "preview_input_next": {
                "label": "Preview Next",
                "params": {},
                "help": "Move preview to the next input.",
            },
            "preview_input_previous": {
                "label": "Preview Previous",
                "params": {},
                "help": "Move preview to the previous input.",
            },
            # --- Audio ---
            "audio": {
                "label": "Audio Toggle",
                "params": {"input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"}},
                "help": "Toggle audio on/off for an input.",
            },
            "audio_on": {
                "label": "Audio On",
                "params": {"input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"}},
                "help": "Unmute an input.",
            },
            "audio_off": {
                "label": "Audio Off",
                "params": {"input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"}},
                "help": "Mute an input.",
            },
            "set_volume": {
                "label": "Set Volume",
                "params": {
                    "input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"},
                    "value": {"type": "number", "required": True, "min": 0, "max": 100, "unit": "%", "help": "Fader position 0-100"},
                },
                "help": "Set the fader position for an input (0-100).",
            },
            "set_volume_fade": {
                "label": "Set Volume (Fade)",
                "params": {
                    "input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"},
                    "value": {"type": "number", "required": True, "min": 0, "max": 100, "unit": "%", "help": "Target fader position 0-100"},
                    "duration": {"type": "integer", "required": True, "min": 0, "max": 60000, "help": "Fade duration in ms"},
                },
                "help": "Fade an input's volume to a target position over a duration.",
            },
            "set_gain": {
                "label": "Set Gain",
                "params": {
                    "input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"},
                    "value": {"type": "number", "required": True, "min": 0, "max": 24, "unit": "dB", "help": "Gain in dB (0-24)"},
                },
                "help": "Set audio gain for an input.",
            },
            "set_balance": {
                "label": "Set Balance",
                "params": {
                    "input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"},
                    "value": {"type": "number", "required": True, "min": -1, "max": 1, "help": "Balance -1 (left) to 1 (right)"},
                },
                "help": "Set audio balance/pan for an input.",
            },
            "solo": {
                "label": "Solo Toggle",
                "params": {"input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"}},
                "help": "Toggle solo monitoring for an input.",
            },
            "solo_all_off": {
                "label": "Solo All Off",
                "params": {},
                "help": "Turn solo off for every input and bus.",
            },
            "input_bus_on": {
                "label": "Add Input to Bus",
                "params": {
                    "input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"},
                    "bus": {"type": "enum", "required": True, "values": AUDIO_BUSSES, "help": "Bus to send this input to"},
                },
                "help": "Route an input's audio to a bus.",
            },
            "input_bus_off": {
                "label": "Remove Input from Bus",
                "params": {
                    "input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"},
                    "bus": {"type": "enum", "required": True, "values": AUDIO_BUSSES, "help": "Bus to stop sending this input to"},
                },
                "help": "Stop routing an input's audio to a bus.",
            },
            # vMix only has mute/toggle functions for busses A and B, even
            # though it takes a volume for all seven. Offering C-G here would
            # be five buttons that report success and do nothing.
            "bus_audio": {
                "label": "Bus Audio Toggle",
                "params": {"bus": {"type": "enum", "required": True, "values": MUTABLE_BUSSES, "help": "Bus letter"}},
                "help": "Toggle a bus master on/off.",
            },
            "bus_audio_on": {
                "label": "Bus Audio On",
                "params": {"bus": {"type": "enum", "required": True, "values": MUTABLE_BUSSES, "help": "Bus letter"}},
                "help": "Unmute a bus master.",
            },
            "bus_audio_off": {
                "label": "Bus Audio Off",
                "params": {"bus": {"type": "enum", "required": True, "values": MUTABLE_BUSSES, "help": "Bus letter"}},
                "help": "Mute a bus master.",
            },
            "set_bus_volume": {
                "label": "Set Bus Volume",
                "params": {
                    "bus": {"type": "enum", "required": True, "values": MIXABLE_BUSSES, "help": "Bus letter"},
                    "value": {"type": "number", "required": True, "min": 0, "max": 100, "unit": "%", "help": "Fader position 0-100"},
                },
                "help": "Set a bus master fader position.",
            },
            "master_audio": {
                "label": "Master Audio Toggle",
                "params": {},
                "help": "Toggle master audio on/off.",
            },
            "master_audio_on": {
                "label": "Master Audio On",
                "params": {},
                "help": "Unmute the master bus.",
            },
            "master_audio_off": {
                "label": "Master Audio Off",
                "params": {},
                "help": "Mute the master bus.",
            },
            "set_master_volume": {
                "label": "Set Master Volume",
                "params": {"value": {"type": "number", "required": True, "min": 0, "max": 100, "unit": "%", "help": "Fader position 0-100"}},
                "help": "Set the master fader position (0-100).",
            },
            "set_master_volume_fade": {
                "label": "Set Master Volume (Fade)",
                "params": {
                    "value": {"type": "number", "required": True, "min": 0, "max": 100, "unit": "%", "help": "Target fader position 0-100"},
                    "duration": {"type": "integer", "required": True, "min": 0, "max": 60000, "help": "Fade duration in ms"},
                },
                "help": "Fade the master volume to a target position over a duration.",
            },
            "set_headphones_volume": {
                "label": "Set Headphones Volume",
                "params": {"value": {"type": "number", "required": True, "min": 0, "max": 100, "unit": "%", "help": "Fader position 0-100"}},
                "help": "Set the headphones fader position (0-100).",
            },
            # --- Overlays ---
            # vMix bakes the channel into the function name (OverlayInput3In),
            # rather than taking it as a parameter.
            "overlay_input": {
                "label": "Overlay Toggle",
                "params": {
                    "channel": {"type": "integer", "required": True, "min": 1, "max": OVERLAY_CHANNELS, "help": "Overlay channel 1-8"},
                    "input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"},
                },
                "help": "Toggle an overlay channel on or off with the given input.",
            },
            "overlay_input_in": {
                "label": "Overlay In",
                "params": {
                    "channel": {"type": "integer", "required": True, "min": 1, "max": OVERLAY_CHANNELS, "help": "Overlay channel 1-8"},
                    "input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"},
                },
                "help": "Transition an overlay channel in with the given input.",
            },
            "overlay_input_out": {
                "label": "Overlay Out",
                "params": {"channel": {"type": "integer", "required": True, "min": 1, "max": OVERLAY_CHANNELS, "help": "Overlay channel 1-8"}},
                "help": "Transition an overlay channel out.",
            },
            "overlay_input_off": {
                "label": "Overlay Off",
                "params": {"channel": {"type": "integer", "required": True, "min": 1, "max": OVERLAY_CHANNELS, "help": "Overlay channel 1-8"}},
                "help": "Immediately cut an overlay channel off.",
            },
            "overlay_input_zoom": {
                "label": "Overlay Zoom",
                "params": {"channel": {"type": "integer", "required": True, "min": 1, "max": OVERLAY_CHANNELS, "help": "Overlay channel 1-8"}},
                "help": "Zoom a picture-in-picture overlay to fullscreen and back.",
            },
            "overlay_all_off": {
                "label": "All Overlays Off",
                "params": {},
                "help": "Turn off all overlay channels.",
            },
            # --- Recording / Streaming ---
            "start_recording": {
                "label": "Start Recording", "params": {},
                "help": "Start recording the program output.",
            },
            "stop_recording": {
                "label": "Stop Recording", "params": {},
                "help": "Stop the current recording.",
            },
            "start_streaming": {
                "label": "Start Streaming", "params": {},
                "help": "Start the configured live stream.",
            },
            "stop_streaming": {
                "label": "Stop Streaming", "params": {},
                "help": "Stop the live stream.",
            },
            "start_external": {
                "label": "Start External Output", "params": {},
                "help": "Start the external output.",
            },
            "stop_external": {
                "label": "Stop External Output", "params": {},
                "help": "Stop the external output.",
            },
            "start_multicorder": {
                "label": "Start MultiCorder", "params": {},
                "help": "Start MultiCorder recording.",
            },
            "stop_multicorder": {
                "label": "Stop MultiCorder", "params": {},
                "help": "Stop MultiCorder recording.",
            },
            "snapshot": {
                "label": "Snapshot",
                "params": {"value": {"type": "string", "help": "Filename (optional)"}},
                "help": "Save a still image of the program output.",
            },
            "snapshot_input": {
                "label": "Snapshot Input",
                "params": {
                    "input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"},
                    "value": {"type": "string", "help": "Filename (optional)"},
                },
                "help": "Save a still image of an input.",
            },
            # --- Titles / Text ---
            "set_text": {
                "label": "Set Text",
                "params": {
                    "input": {"type": "string", "options_state": "input_list", "required": True, "help": "Title input number or name"},
                    "selected_name": {"type": "string", "help": "Text field name (e.g. Headline, or Headline.Text for GT titles)"},
                    "value": {"type": "string", "required": True, "trim": False, "help": "Text to display"},
                },
                "help": "Set a text field in a title input.",
            },
            "set_image": {
                "label": "Set Image",
                "params": {
                    "input": {"type": "string", "options_state": "input_list", "required": True, "help": "Title input number or name"},
                    "selected_name": {"type": "string", "help": "Image field name (e.g. MyImage.Source)"},
                    "value": {"type": "string", "required": True, "help": "Image filename, or empty to clear"},
                },
                "help": "Set an image field in a title input.",
            },
            "set_countdown": {
                "label": "Set Countdown",
                "params": {
                    "input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"},
                    "value": {"type": "string", "required": True, "pattern": r"^\d{1,2}:\d{2}:\d{2}$", "help": "Duration as hh:mm:ss"},
                },
                "help": "Set a countdown duration (hh:mm:ss).",
            },
            "start_countdown": {
                "label": "Start Countdown",
                "params": {"input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"}},
                "help": "Start a countdown.",
            },
            "stop_countdown": {
                "label": "Stop Countdown",
                "params": {"input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"}},
                "help": "Stop and reset a countdown.",
            },
            # --- Playback ---
            "play": {
                "label": "Play",
                "params": {"input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"}},
                "help": "Play an input.",
            },
            "pause": {
                "label": "Pause",
                "params": {"input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"}},
                "help": "Pause an input.",
            },
            "play_pause": {
                "label": "Play/Pause",
                "params": {"input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"}},
                "help": "Toggle play/pause on an input.",
            },
            "restart": {
                "label": "Restart",
                "params": {"input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"}},
                "help": "Restart an input from the beginning.",
            },
            "loop_on": {
                "label": "Loop On",
                "params": {"input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"}},
                "help": "Enable looping on an input.",
            },
            "loop_off": {
                "label": "Loop Off",
                "params": {"input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"}},
                "help": "Disable looping on an input.",
            },
            "set_position": {
                "label": "Set Position",
                "params": {
                    "input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"},
                    "position": {"type": "integer", "required": True, "min": 0, "help": "Position in milliseconds"},
                },
                "help": "Seek an input to a position in milliseconds.",
            },
            "set_rate": {
                "label": "Set Rate",
                "params": {
                    "input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"},
                    "value": {"type": "number", "required": True, "min": 0.1, "max": 4, "help": "Playback rate (1 = normal, 0.5 = half, 2 = double)"},
                },
                "help": "Set the playback speed of an input.",
            },
            "select_index": {
                "label": "Select Index",
                "params": {
                    "input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"},
                    "index": {"type": "integer", "required": True, "min": 0, "help": "Item index (lists start at 1)"},
                },
                "help": "Select an item within a list, photos or virtual-set input.",
            },
            "next_item": {
                "label": "Next Item",
                "params": {"input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"}},
                "help": "Move to the next item in a list input.",
            },
            "previous_item": {
                "label": "Previous Item",
                "params": {"input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"}},
                "help": "Move to the previous item in a list input.",
            },
            # --- Replay ---
            "replay_play": {"label": "Replay Play", "params": {}, "help": "Start replay playback."},
            "replay_pause": {"label": "Replay Pause", "params": {}, "help": "Pause replay playback."},
            "replay_mark_in": {"label": "Replay Mark In", "params": {}, "help": "Set the replay in point."},
            "replay_mark_out": {"label": "Replay Mark Out", "params": {}, "help": "Set the replay out point."},
            "replay_mark_in_out": {
                "label": "Replay Mark In/Out",
                "params": {"value": {"type": "integer", "min": 1, "max": 3600, "help": "Seconds before now to mark in"}},
                "help": "Mark an event ending now, starting the given number of seconds back.",
            },
            "replay_live": {"label": "Replay Live", "params": {}, "help": "Switch the replay channel to live."},
            "replay_recorded": {"label": "Replay Recorded", "params": {}, "help": "Switch the replay channel to recorded."},
            "replay_set_speed": {
                "label": "Replay Set Speed",
                "params": {"value": {"type": "number", "required": True, "min": 0, "max": 1, "help": "Speed 0-1"}},
                "help": "Set the replay playback speed.",
            },
            "replay_play_last_event": {
                "label": "Replay Play Last Event", "params": {},
                "help": "Play the most recently marked replay event.",
            },
            # --- PTZ ---
            **{
                cmd: {
                    "label": label,
                    "params": {
                        "input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"},
                        "speed": {"type": "number", "min": 0, "max": 1, "help": "Speed 0-1 (default 0.5)"},
                    },
                    "help": help_text,
                }
                for cmd, label, help_text in [
                    ("ptz_move_up", "PTZ Up", "Pan/tilt camera up."),
                    ("ptz_move_down", "PTZ Down", "Pan/tilt camera down."),
                    ("ptz_move_left", "PTZ Left", "Pan/tilt camera left."),
                    ("ptz_move_right", "PTZ Right", "Pan/tilt camera right."),
                    ("ptz_zoom_in", "PTZ Zoom In", "Zoom the camera in."),
                    ("ptz_zoom_out", "PTZ Zoom Out", "Zoom the camera out."),
                ]
            },
            "ptz_move_stop": {
                "label": "PTZ Stop",
                "params": {"input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"}},
                "help": "Stop pan/tilt movement.",
            },
            "ptz_zoom_stop": {
                "label": "PTZ Zoom Stop",
                "params": {"input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"}},
                "help": "Stop zoom movement.",
            },
            "ptz_home": {
                "label": "PTZ Home",
                "params": {"input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"}},
                "help": "Move the camera to its home position.",
            },
            "ptz_focus_auto": {
                "label": "PTZ Auto Focus",
                "params": {"input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"}},
                "help": "Enable auto focus.",
            },
            # --- Input management ---
            "add_input": {
                "label": "Add Input",
                "params": {"value": {"type": "string", "required": True, "help": "Type|Filename, e.g. Colour|Red or Video|c:\\clip.mp4"}},
                "help": "Add a new input to the production.",
            },
            "remove_input": {
                "label": "Remove Input",
                "params": {"input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"}},
                "help": "Remove an input from the production.",
            },
            "set_input_name": {
                "label": "Rename Input",
                "params": {
                    "input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"},
                    "value": {"type": "string", "required": True, "help": "New name"},
                },
                "help": "Rename an input.",
            },
            "browser_navigate": {
                "label": "Browser Navigate",
                "params": {
                    "input": {"type": "string", "options_state": "input_list", "required": True, "help": "Browser input number or name"},
                    "value": {"type": "string", "required": True, "help": "URL to open"},
                },
                "help": "Point a browser input at a URL.",
            },
            "script_start": {
                "label": "Start Script",
                "params": {"value": {"type": "string", "required": True, "help": "Script name"}},
                "help": "Start a vMix script by name.",
            },
            "script_stop": {
                "label": "Stop Script",
                "params": {"value": {"type": "string", "required": True, "help": "Script name"}},
                "help": "Stop a running vMix script.",
            },
            "raw_function": {
                "label": "Raw Function",
                "params": {
                    "function": {"type": "string", "required": True, "help": "vMix shortcut function name"},
                    "query": {"type": "string", "trim": False, "help": "Query string, already URL-encoded (e.g. Input=3&Value=Hello%20world)"},
                },
                "help": (
                    "Send any vMix shortcut function this driver doesn't expose. "
                    "The query string is sent exactly as typed, so encode it yourself."
                ),
            },
        },
    }

    # How each command reaches vMix.
    #
    #   "fn"    the shortcut function name. A {placeholder} is filled from the
    #           named param and is NOT also sent as a query argument, because
    #           vMix bakes the channel, bus, stinger number or transition effect
    #           into the function name (OverlayInput3In, BusAAudio, Stinger2,
    #           CubeZoom) rather than taking it as a parameter.
    #   "args"  param name -> vMix query key. Every value is URL-encoded: a raw
    #           "&" truncates the value at the ampersand and a raw "+" arrives
    #           as a space, both silently.
    #   "join"  (query key, (param, ...)) for the handful of functions that take
    #           several values comma-joined into one argument.
    _COMMANDS: dict[str, dict[str, Any]] = {
        # Transitions
        "cut": {"fn": "Cut", "args": {"input": "Input"}},
        "fade": {"fn": "Fade", "args": {"input": "Input", "duration": "Duration"}},
        "cut_direct": {"fn": "CutDirect", "args": {"input": "Input"}},
        "fade_to_black": {"fn": "FadeToBlack", "args": {}},
        "transition": {"fn": "{effect}", "args": {"input": "Input", "duration": "Duration"}},
        "transition_button": {"fn": "Transition{number}", "args": {}},
        "stinger": {"fn": "Stinger{number}", "args": {"input": "Input"}},
        "set_fader": {"fn": "SetFader", "args": {"position": "Value"}},
        "quick_play": {"fn": "QuickPlay", "args": {"input": "Input"}},
        # Input switching
        "preview_input": {"fn": "PreviewInput", "args": {"input": "Input"}},
        "active_input": {"fn": "ActiveInput", "args": {"input": "Input"}},
        "preview_input_next": {"fn": "PreviewInputNext", "args": {}},
        "preview_input_previous": {"fn": "PreviewInputPrevious", "args": {}},
        # Audio
        "audio": {"fn": "Audio", "args": {"input": "Input"}},
        "audio_on": {"fn": "AudioOn", "args": {"input": "Input"}},
        "audio_off": {"fn": "AudioOff", "args": {"input": "Input"}},
        "set_volume": {"fn": "SetVolume", "args": {"input": "Input", "value": "Value"}},
        "set_volume_fade": {
            "fn": "SetVolumeFade", "args": {"input": "Input"},
            "join": ("Value", ("value", "duration")),
        },
        "set_gain": {"fn": "SetGain", "args": {"input": "Input", "value": "Value"}},
        "set_balance": {"fn": "SetBalance", "args": {"input": "Input", "value": "Value"}},
        "solo": {"fn": "Solo", "args": {"input": "Input"}},
        "solo_all_off": {"fn": "SoloAllOff", "args": {}},
        "input_bus_on": {"fn": "AudioBusOn", "args": {"input": "Input", "bus": "Value"}},
        "input_bus_off": {"fn": "AudioBusOff", "args": {"input": "Input", "bus": "Value"}},
        "bus_audio": {"fn": "Bus{bus}Audio", "args": {}},
        "bus_audio_on": {"fn": "Bus{bus}AudioOn", "args": {}},
        "bus_audio_off": {"fn": "Bus{bus}AudioOff", "args": {}},
        "set_bus_volume": {"fn": "SetBus{bus}Volume", "args": {"value": "Value"}},
        "master_audio": {"fn": "MasterAudio", "args": {}},
        "master_audio_on": {"fn": "MasterAudioOn", "args": {}},
        "master_audio_off": {"fn": "MasterAudioOff", "args": {}},
        "set_master_volume": {"fn": "SetMasterVolume", "args": {"value": "Value"}},
        "set_master_volume_fade": {
            "fn": "SetMasterVolumeFade", "args": {},
            "join": ("Value", ("value", "duration")),
        },
        "set_headphones_volume": {"fn": "SetHeadphonesVolume", "args": {"value": "Value"}},
        # Overlays
        "overlay_input": {"fn": "OverlayInput{channel}", "args": {"input": "Input"}},
        "overlay_input_in": {"fn": "OverlayInput{channel}In", "args": {"input": "Input"}},
        "overlay_input_out": {"fn": "OverlayInput{channel}Out", "args": {}},
        "overlay_input_off": {"fn": "OverlayInput{channel}Off", "args": {}},
        "overlay_input_zoom": {"fn": "OverlayInput{channel}Zoom", "args": {}},
        "overlay_all_off": {"fn": "OverlayInputAllOff", "args": {}},
        # Recording / streaming
        "start_recording": {"fn": "StartRecording", "args": {}},
        "stop_recording": {"fn": "StopRecording", "args": {}},
        "start_streaming": {"fn": "StartStreaming", "args": {}},
        "stop_streaming": {"fn": "StopStreaming", "args": {}},
        "start_external": {"fn": "StartExternal", "args": {}},
        "stop_external": {"fn": "StopExternal", "args": {}},
        "start_multicorder": {"fn": "StartMultiCorder", "args": {}},
        "stop_multicorder": {"fn": "StopMultiCorder", "args": {}},
        "snapshot": {"fn": "Snapshot", "args": {"value": "Value"}},
        "snapshot_input": {"fn": "SnapshotInput", "args": {"input": "Input", "value": "Value"}},
        # Titles
        "set_text": {
            "fn": "SetText",
            "args": {"input": "Input", "selected_name": "SelectedName", "value": "Value"},
        },
        "set_image": {
            "fn": "SetImage",
            "args": {"input": "Input", "selected_name": "SelectedName", "value": "Value"},
        },
        "set_countdown": {"fn": "SetCountdown", "args": {"input": "Input", "value": "Value"}},
        "start_countdown": {"fn": "StartCountdown", "args": {"input": "Input"}},
        "stop_countdown": {"fn": "StopCountdown", "args": {"input": "Input"}},
        # Playback
        "play": {"fn": "Play", "args": {"input": "Input"}},
        "pause": {"fn": "Pause", "args": {"input": "Input"}},
        "play_pause": {"fn": "PlayPause", "args": {"input": "Input"}},
        "restart": {"fn": "Restart", "args": {"input": "Input"}},
        "loop_on": {"fn": "LoopOn", "args": {"input": "Input"}},
        "loop_off": {"fn": "LoopOff", "args": {"input": "Input"}},
        "set_position": {"fn": "SetPosition", "args": {"input": "Input", "position": "Value"}},
        "set_rate": {"fn": "SetRate", "args": {"input": "Input", "value": "Value"}},
        "select_index": {"fn": "SelectIndex", "args": {"input": "Input", "index": "Value"}},
        "next_item": {"fn": "NextItem", "args": {"input": "Input"}},
        "previous_item": {"fn": "PreviousItem", "args": {"input": "Input"}},
        # Replay
        "replay_play": {"fn": "ReplayPlay", "args": {}},
        "replay_pause": {"fn": "ReplayPause", "args": {}},
        "replay_mark_in": {"fn": "ReplayMarkIn", "args": {}},
        "replay_mark_out": {"fn": "ReplayMarkOut", "args": {}},
        "replay_mark_in_out": {"fn": "ReplayMarkInOut", "args": {"value": "Value"}},
        "replay_live": {"fn": "ReplayLive", "args": {}},
        "replay_recorded": {"fn": "ReplayRecorded", "args": {}},
        "replay_set_speed": {"fn": "ReplaySetSpeed", "args": {"value": "Value"}},
        "replay_play_last_event": {"fn": "ReplayPlayLastEvent", "args": {}},
        # PTZ
        "ptz_move_up": {"fn": "PTZMoveUp", "args": {"input": "Input", "speed": "Value"}},
        "ptz_move_down": {"fn": "PTZMoveDown", "args": {"input": "Input", "speed": "Value"}},
        "ptz_move_left": {"fn": "PTZMoveLeft", "args": {"input": "Input", "speed": "Value"}},
        "ptz_move_right": {"fn": "PTZMoveRight", "args": {"input": "Input", "speed": "Value"}},
        "ptz_move_stop": {"fn": "PTZMoveStop", "args": {"input": "Input"}},
        "ptz_zoom_in": {"fn": "PTZZoomIn", "args": {"input": "Input", "speed": "Value"}},
        "ptz_zoom_out": {"fn": "PTZZoomOut", "args": {"input": "Input", "speed": "Value"}},
        "ptz_zoom_stop": {"fn": "PTZZoomStop", "args": {"input": "Input"}},
        "ptz_home": {"fn": "PTZHome", "args": {"input": "Input"}},
        "ptz_focus_auto": {"fn": "PTZFocusAuto", "args": {"input": "Input"}},
        # Input management
        "add_input": {"fn": "AddInput", "args": {"value": "Value"}},
        "remove_input": {"fn": "RemoveInput", "args": {"input": "Input"}},
        "set_input_name": {"fn": "SetInputName", "args": {"input": "Input", "value": "Value"}},
        "browser_navigate": {"fn": "BrowserNavigate", "args": {"input": "Input", "value": "Value"}},
        "script_start": {"fn": "ScriptStart", "args": {"value": "Value"}},
        "script_stop": {"fn": "ScriptStop", "args": {"value": "Value"}},
    }

    # Global activators: the name vMix pushes -> (state key, how to read the value).
    # These arrive as "ACTS OK <Name> <value>" with no input number.
    _GLOBAL_ACTS: dict[str, tuple[str, str]] = {
        "FadeToBlack": ("fade_to_black", "bool"),
        "Recording": ("recording", "bool"),
        "Streaming": ("streaming", "bool"),
        "External": ("external", "bool"),
        "MultiCorder": ("multicorder", "bool"),
        "Fullscreen": ("fullscreen", "bool"),
        "MasterVolume": ("master_volume", "fader"),
        # The vMix audio button reads "on" when the bus is AUDIBLE, so the
        # activator is the inverse of muted.
        "MasterAudio": ("master_muted", "inverted_bool"),
        "MasterHeadphones": ("master_headphones_volume", "fader"),
        "BusAVolume": ("bus_a_volume", "fader"),
        "BusBVolume": ("bus_b_volume", "fader"),
        "BusAAudio": ("bus_a_muted", "inverted_bool"),
        "BusBAudio": ("bus_b_muted", "inverted_bool"),
        "ReplayRecording": ("replay_recording", "bool"),
        "ReplayLive": ("replay_live", "bool"),
        "ReplayPlaying": ("replay_playing", "bool"),
    }

    # Input-scoped activators: "ACTS OK <Name> <input> <value>".
    _INPUT_ACTS: dict[str, tuple[str, str]] = {
        "InputPlaying": ("playing", "bool"),
        "InputVolume": ("volume", "fader"),
        "InputHeadphones": ("headphones_volume", "fader"),
        "InputAudio": ("muted", "inverted_bool"),
        "InputSolo": ("solo", "bool"),
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._cmd_lock = asyncio.Lock()
        self._cmd_response: asyncio.Queue[str] = asyncio.Queue()
        self._probe_response: asyncio.Queue[str] = asyncio.Queue()
        self._tally_subscribed = False
        self._acts_subscribed = False
        # Input numbers seen in the last XML state — lets the next parse
        # deregister inputs removed from the production.
        self._known_inputs: set[str] = set()

    def _ensure_input(self, num: str) -> bool:
        """Register input ``num`` as a child if it isn't already.

        Both the tally string and the XML state name inputs, and either can
        arrive first, so both call this before writing child state. Returns
        False for a number outside the declared id range (a production with
        more than 1000 inputs, or a malformed reply) — the caller skips it
        rather than letting a bad number abort the whole parse.
        """
        try:
            local_id = int(num)
        except (TypeError, ValueError):
            return False
        if self.is_child_registered("input", local_id):
            return True
        try:
            # Balance would otherwise start at its declared minimum, which is
            # hard left. Centre is the honest starting point, and an input
            # that carries no audio at all keeps it — vMix reports balance
            # only for inputs that have some.
            self.register_child("input", local_id, initial_state={"balance": 0.0})
        except ValueError as exc:
            log.warning(f"[{self.device_id}] Skipping input {num}: {exc}")
            return False
        return True

    def _create_frame_parser(self) -> Optional[FrameParser]:
        """Use callable parser for vMix mixed-mode framing."""
        return CallableFrameParser(_parse_vmix_frame)

    def _resolve_delimiter(self) -> Optional[bytes]:
        """vMix uses custom framing, not delimiter-based."""
        return None

    async def _initial_sync(self) -> None:
        """Arm the push subscriptions, then seed from a full state read.

        The seed is not optional and does not belong to the poll loop.
        Subscribing to the activators sends nothing back until something
        changes, so a system with polling turned off would sit showing program
        input 0 until an operator happened to touch the switcher. One XML read
        on connect settles everything, including the input titles and the
        transition slots that no event ever carries.
        """
        if self.config.get("subscribe_tally", True):
            await self._subscribe("TALLY")
            self._tally_subscribed = True
        if self.config.get("subscribe_acts", True):
            await self._subscribe("ACTS")
            self._acts_subscribed = True
        await self.poll()

    async def _close_session(self) -> None:
        # Runs on every teardown path: forget this session's subscriptions
        # and drop any queued command responses, so a stale reply can never
        # answer the next session's first command.
        self._tally_subscribed = False
        self._acts_subscribed = False
        self._drain(self._cmd_response)
        self._drain(self._probe_response)

    @staticmethod
    def _drain(queue: asyncio.Queue) -> None:
        while not queue.empty():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def disconnect(self) -> None:
        """Disconnect from vMix."""
        # Politely drop the subscriptions while the link is still open.
        # Best-effort: the software may already be gone.
        if self.transport:
            try:
                if self._tally_subscribed:
                    await self.transport.send(b"UNSUBSCRIBE TALLY\r\n")
                if self._acts_subscribed:
                    await self.transport.send(b"UNSUBSCRIBE ACTS\r\n")
            except Exception:
                pass
        await super().disconnect()

    async def _liveness_probe(self) -> None:
        """Ask for one value vMix always has and wait for the answer.

        The control link is receive-mostly once the subscriptions are armed, so
        a vMix that closed or a PC that slept can leave a socket that never
        reports a fault. XMLTEXT on the version is the cheapest question with a
        guaranteed answer.
        """
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        self._drain(self._probe_response)
        await self.transport.send(b"XMLTEXT vmix/version\r\n")
        await self._probe_response.get()

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Send a named command to vMix."""
        params = params or {}

        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        # raw_function hands the query through untouched: the point of the
        # escape hatch is to send exactly what the user typed.
        if command == "raw_function":
            func_name = str(params.get("function", "")).strip()
            if not func_name:
                raise ValueError("raw_function needs a function name")
            return await self._send_function(func_name, str(params.get("query", "")))

        spec = self._COMMANDS.get(command)
        if spec is None:
            log.warning(f"[{self.device_id}] Unknown command: {command}")
            return None

        function, query = self._build_request(command, spec, params)
        return await self._send_function(function, query)

    def _build_request(
        self, command: str, spec: dict[str, Any], params: dict[str, Any]
    ) -> tuple[str, str]:
        """Render one command into a vMix function name and query string."""
        function = spec["fn"]
        consumed: set[str] = set()
        if "{" in function:
            for name in ("channel", "number", "bus", "effect"):
                token = "{" + name + "}"
                if token not in function:
                    continue
                if name not in params or params[name] in (None, ""):
                    raise ValueError(f"{command} requires the '{name}' parameter")
                function = function.replace(token, str(params[name]))
                consumed.add(name)

        parts: list[str] = []
        for name, key in spec["args"].items():
            if name in consumed:
                continue
            value = params.get(name)
            if value is None or value == "":
                continue
            parts.append(f"{key}={self._encode(value)}")

        join = spec.get("join")
        if join:
            key, names = join
            values = [params.get(n) for n in names]
            if any(v is None or v == "" for v in values):
                raise ValueError(f"{command} requires {' and '.join(names)}")
            # vMix takes these as one comma-joined argument, not as separate
            # query keys — sending them separately is rejected outright.
            parts.append(f"{key}={self._encode(','.join(str(v) for v in values))}")

        return function, "&".join(parts)

    @staticmethod
    def _encode(value: Any) -> str:
        """Percent-encode one query value.

        vMix parses the query string after the function name, so an unencoded
        "&" ends the value there and an unencoded "+" arrives as a space. Both
        corrupt silently — the command still answers OK.
        """
        return urllib.parse.quote(str(value), safe="")

    async def _send_function(self, function: str, query: str = "") -> str:
        """Send a FUNCTION command and wait for the OK/ER response."""
        cmd = f"FUNCTION {function}"
        if query:
            cmd += f" {query}"
        cmd += "\r\n"

        async with self._cmd_lock:
            # A previous command that timed out may have had its reply arrive
            # late. Left queued, it would answer this command instead, and
            # every answer after it would belong to the question before.
            self._drain(self._cmd_response)
            await self.transport.send(cmd.encode("utf-8"))
            log.debug(f"[{self.device_id}] Sent: {cmd.strip()}")

            try:
                response = await asyncio.wait_for(
                    self._cmd_response.get(), timeout=5.0
                )
            except asyncio.TimeoutError:
                log.warning(f"[{self.device_id}] Command timeout: {function}")
                return "TIMEOUT"

        # vMix answers an unknown function with OK, so an ER is always a real
        # complaint about a command it does understand — worth surfacing.
        if " ER " in f" {response} " or response.startswith("FUNCTION ER"):
            message = response.partition(" ER ")[2].strip() or "vMix rejected the command"
            self.set_state(self.LAST_ERROR_PROPERTY, f"{function}: {message}")
            log.warning(f"[{self.device_id}] {function} rejected: {message}")
        return response

    async def _subscribe(self, channel: str) -> None:
        """Subscribe to a push channel (TALLY or ACTS)."""
        if not self.transport or not self.transport.connected:
            return
        await self.transport.send(f"SUBSCRIBE {channel}\r\n".encode())
        log.debug(f"[{self.device_id}] Subscribed to {channel}")

    async def on_data_received(self, data: bytes) -> None:
        """Route incoming messages by prefix."""
        # XML body (tagged by frame parser)
        if data.startswith(_XML_BODY_PREFIX):
            xml_body = data[len(_XML_BODY_PREFIX):]
            await self._handle_xml(xml_body)
            return

        text = data.decode("utf-8", errors="replace").strip()
        if not text:
            return

        # FUNCTION OK <msg> / FUNCTION ER <msg>
        if text.startswith("FUNCTION"):
            await self._cmd_response.put(text)
            return

        if text.startswith("TALLY OK"):
            self._handle_tally(text)
            return

        if text.startswith("ACTS OK"):
            self._handle_acts(text)
            return

        # XMLTEXT is only used by the liveness probe today.
        if text.startswith("XMLTEXT"):
            await self._probe_response.put(text)
            return

        if text.startswith("SUBSCRIBE OK") or text.startswith("UNSUBSCRIBE OK"):
            log.debug(f"[{self.device_id}] {text}")
            return

        # vMix greets a new connection with its version before anything is
        # asked, which is where this normally arrives.
        if text.startswith("VERSION OK"):
            self.set_state("version", text[len("VERSION OK"):].strip())
            return

        log.debug(f"[{self.device_id}] Unhandled message: {text[:80]}")

    def _handle_tally(self, text: str) -> None:
        """
        Parse a TALLY response and update per-input tally.

        Format: TALLY OK <tally_string>, one character per input:
            0 = safe (not in program or preview)
            1 = program (live)
            2 = preview
        """
        tally_data = text[len("TALLY OK"):].strip()

        active = None
        preview = None

        for i, ch in enumerate(tally_data):
            input_num = i + 1  # 1-based
            try:
                tally_val = int(ch)
            except ValueError:
                continue

            # The tally subscription fires before the first XML poll, so this
            # is often where an input is first seen. register_child is a no-op
            # for one already registered, and the XML parse fills in the rest.
            self._ensure_input(str(input_num))
            self.set_child_state("input", input_num, "tally", tally_val)

            if tally_val == 1 and active is None:
                active = input_num
            elif tally_val == 2 and preview is None:
                preview = input_num

        # An overlay puts its own input into program too, so the first "1" in
        # the string is not reliably the program input. When the activators are
        # running they say which input is live outright, so let them own it and
        # let the XML poll correct anything either way.
        if self._acts_subscribed:
            return
        if active is not None:
            self.set_state("active", active)
        if preview is not None:
            self.set_state("preview", preview)

    def _handle_acts(self, text: str) -> None:
        """Parse one ACTS activator event.

        Two shapes, and which one you get depends on the activator:
            ACTS OK FadeToBlack 1        global
            ACTS OK Overlay1 3 1         input-scoped: <name> <input> <value>
        Values are 0/1 for a button and a 0-1 float for a fader.
        """
        body = text[len("ACTS OK"):].strip()
        parts = body.split()
        if len(parts) < 2:
            log.debug(f"[{self.device_id}] Short ACTS event: {text[:80]}")
            return

        name = parts[0]

        if name in self._GLOBAL_ACTS:
            key, kind = self._GLOBAL_ACTS[name]
            value = self._coerce_act(parts[-1], kind)
            if value is not None:
                self.set_state(key, value)
            return

        # Everything below is input-scoped: <name> <input> <value>.
        if len(parts) < 3:
            log.debug(f"[{self.device_id}] ACTS event without an input: {text[:80]}")
            return
        number, raw = parts[1], parts[2]

        if name.startswith("Overlay"):
            channel = name[len("Overlay"):]
            if not channel.isdigit() or not 1 <= int(channel) <= OVERLAY_CHANNELS:
                return
            # The middle token names the input assigned to the channel; the
            # last says whether it is showing. Off publishes 0, matching the
            # XML, where an idle channel carries no input number at all.
            showing = self._coerce_act(raw, "bool")
            try:
                self.set_state(f"overlay.{int(channel)}", int(number) if showing else 0)
            except ValueError:
                pass
            return

        if name in ("Input", "InputPreview"):
            try:
                input_num = int(number)
            except ValueError:
                return
            if not self._coerce_act(raw, "bool"):
                return
            self.set_state("active" if name == "Input" else "preview", input_num)
            if not self._tally_subscribed and self._ensure_input(number):
                self.set_child_state(
                    "input", input_num, "tally", 1 if name == "Input" else 2
                )
            return

        mapping = self._INPUT_ACTS.get(name)
        if mapping is None:
            log.debug(f"[{self.device_id}] Unmapped activator: {text[:80]}")
            return
        prop, kind = mapping
        value = self._coerce_act(raw, kind)
        if value is None:
            return
        try:
            input_num = int(number)
        except ValueError:
            return
        if self._ensure_input(number):
            self.set_child_state("input", input_num, prop, value)

    @staticmethod
    def _coerce_act(raw: str, kind: str) -> Any:
        """Turn one activator value into the type its state variable declares."""
        try:
            number = float(raw)
        except ValueError:
            return None
        if kind == "bool":
            return number != 0
        if kind == "inverted_bool":
            return number == 0
        if kind == "fader":
            return unit_to_fader(number)
        return number

    async def _handle_xml(self, xml_data: bytes) -> None:
        """Parse vMix XML state and flatten into state keys."""
        try:
            root = ET.fromstring(xml_data)
        except ET.ParseError as e:
            log.warning(f"[{self.device_id}] XML parse error: {e}")
            return

        # Everything at the top level of the vMix document is a child ELEMENT.
        # The <vmix> root carries no attributes at all.
        for element, key in (
            ("version", "version"),
            ("edition", "edition"),
        ):
            found = root.find(element)
            if found is not None and found.text:
                self.set_state(key, found.text.strip())

        for element, key in (
            ("recording", "recording"),
            ("streaming", "streaming"),
            ("external", "external"),
            ("fadeToBlack", "fade_to_black"),
            ("playList", "playlist"),
            ("multiCorder", "multicorder"),
            ("fullscreen", "fullscreen"),
        ):
            found = root.find(element)
            if found is not None:
                self.set_state(key, (found.text or "").strip() == "True")

        for element, key in (("active", "active"), ("preview", "preview")):
            found = root.find(element)
            if found is not None and found.text:
                try:
                    self.set_state(key, int(found.text.strip()))
                except ValueError:
                    pass

        self._parse_inputs(root)
        self._parse_overlays(root)
        self._parse_transitions(root)
        self._parse_audio(root)

    def _parse_inputs(self, root: ET.Element) -> None:
        inputs = root.find("inputs")
        if inputs is None:
            return

        input_count = 0
        input_options: list[dict[str, str]] = []
        seen: set[str] = set()

        for inp in inputs.findall("input"):
            input_count += 1
            num = inp.get("number", str(input_count))
            title = inp.get("title", "")

            seen.add(num)
            input_options.append(
                {"value": num, "label": f"{num}: {title}" if title else num}
            )

            if not self._ensure_input(num):
                continue

            updates: dict[str, Any] = {
                "title": title,
                "short_title": inp.get("shortTitle", ""),
                "key": inp.get("key", ""),
                "type": inp.get("type", ""),
                "state": inp.get("state", ""),
                "playing": inp.get("state", "") == "Running",
                "loop": inp.get("loop", "False") == "True",
            }
            for attr, prop in (
                ("position", "position"),
                ("duration", "duration"),
                ("selectedIndex", "selected_index"),
            ):
                raw = inp.get(attr)
                if raw is not None:
                    try:
                        updates[prop] = int(raw)
                    except ValueError:
                        pass

            # Audio attributes are only present on inputs that carry audio, so
            # a Colour or Blank input reports none of them and its audio state
            # stays unset rather than claiming a default.
            if inp.get("muted") is not None:
                updates["muted"] = inp.get("muted") == "True"
            raw_volume = inp.get("volume")
            if raw_volume is not None:
                try:
                    updates["volume"] = amplitude_to_fader(float(raw_volume))
                except ValueError:
                    pass
            for attr, prop in (("balance", "balance"), ("gainDb", "gain_db")):
                raw = inp.get(attr)
                if raw is not None:
                    try:
                        updates[prop] = float(raw)
                    except ValueError:
                        pass
            for attr, prop in (("solo", "solo"), ("soloPFL", "solo_pfl")):
                raw = inp.get(attr)
                if raw is not None:
                    updates[prop] = raw == "True"
            if inp.get("audiobusses") is not None:
                updates["audio_busses"] = inp.get("audiobusses", "")

            self.set_child_state_batch("input", int(num), updates)

        self.set_state("input_count", input_count)
        # The command dropdowns read this JSON list (options_state).
        # Rebuilt from scratch each poll so inputs removed in vMix
        # drop out of the picker immediately.
        self.set_state("input_list", json.dumps(input_options))

        # Inputs are a live editing surface — drop the ones that left the
        # production so their state doesn't linger. deregister_child
        # deletes every key under the child, including tally.
        for gone in self._known_inputs - seen:
            try:
                self.deregister_child("input", int(gone))
            except ValueError:
                pass
        self._known_inputs = seen

    def _parse_overlays(self, root: ET.Element) -> None:
        overlays = root.find("overlays")
        if overlays is None:
            return
        for overlay in overlays.findall("overlay"):
            raw_num = overlay.get("number", "")
            if not raw_num.isdigit():
                continue
            channel = int(raw_num)
            # vMix lists sixteen but only addresses eight; the rest would be
            # state keys nothing can read or write.
            if not 1 <= channel <= OVERLAY_CHANNELS:
                continue
            text = (overlay.text or "").strip()
            try:
                self.set_state(f"overlay.{channel}", int(text) if text else 0)
            except ValueError:
                self.set_state(f"overlay.{channel}", 0)

    def _parse_transitions(self, root: ET.Element) -> None:
        transitions = root.find("transitions")
        if transitions is None:
            return
        for trans in transitions.findall("transition"):
            raw_num = trans.get("number", "")
            if not raw_num.isdigit() or not 1 <= int(raw_num) <= 4:
                continue
            number = int(raw_num)
            self.set_state(f"transition.{number}.effect", trans.get("effect", ""))
            try:
                self.set_state(
                    f"transition.{number}.duration", int(trans.get("duration", "0"))
                )
            except ValueError:
                pass

    def _parse_audio(self, root: ET.Element) -> None:
        audio = root.find("audio")
        if audio is None:
            return

        master = audio.find("master")
        if master is not None:
            self._set_bus_state(master, "master_volume", "master_muted")
            raw = master.get("headphonesVolume")
            if raw is not None:
                try:
                    self.set_state(
                        "master_headphones_volume", amplitude_to_fader(float(raw))
                    )
                except ValueError:
                    pass

        # Bus A/B only appear once they are enabled in vMix's audio settings.
        for element, volume_key, muted_key in (
            ("busA", "bus_a_volume", "bus_a_muted"),
            ("busB", "bus_b_volume", "bus_b_muted"),
        ):
            bus = audio.find(element)
            if bus is not None:
                self._set_bus_state(bus, volume_key, muted_key)

    def _set_bus_state(self, node: ET.Element, volume_key: str, muted_key: str) -> None:
        raw = node.get("volume")
        if raw is not None:
            try:
                self.set_state(volume_key, amplitude_to_fader(float(raw)))
            except ValueError:
                pass
        raw = node.get("muted")
        if raw is not None:
            self.set_state(muted_key, raw == "True")

    async def poll(self) -> None:
        """Request full XML state from vMix.

        The subscriptions carry everything that changes during a show; this
        reseeds the things they never send — input titles, types, the
        transition slots — and corrects anything a missed event left stale.
        """
        if not self.transport or not self.transport.connected:
            return

        try:
            await self.transport.send(b"XML\r\n")
        except (ConnectionError, ConnectionFaultError, OSError) as exc:
            log.warning(f"[{self.device_id}] Poll failed: {exc}")
