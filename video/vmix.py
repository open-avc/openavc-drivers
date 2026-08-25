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

Two numbers in this driver are off by one on the wire, and they are not the same
one. The Mix argument counts from zero off the main mix (see MAIN_MIX below).
The SRT output number counts from zero off Output 1 -- the vendor's own wording
is "optional output number starting from 0" -- so Output 2 is Value=1. Both
conversions happen once, on the way out; everything a user sees is vMix's own
number, the one its window shows.

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


# The Mix parameter, and the one number in this driver that is off by one.
#
# vMix numbers its mixes the way its own window does: the main mix is 1, and
# the input called "Mix2" is mix 2. The Mix argument on the wire counts from
# zero off the main mix, so mix 2 travels as Mix=1. Measured on vMix 29:
# ActiveInput Input=1&Mix=1 moved <mix number="2">, and Mix=0 moved the main
# program. Everything a user sees here -- the picker, the child ids, the state
# keys -- is vMix's number; the subtraction happens once, on the way out.
#
# Which commands accept it is NOT guesswork either. The vendor's function table
# names Mix on five of them, and the transition effects it omits entirely were
# measured accepting it (Cut, Fade, CubeZoom and Merge each moved a sub-mix and
# left the main mix alone). Two that look like they should take it do not:
# CutDirect and QuickPlay both moved the main mix whatever Mix was set to, and
# the vendor's table agrees, so neither offers the parameter.
MAIN_MIX = 1

MIX_PARAM = {
    "type": "string",
    "options_state": "mix_list",
    "help": "Which mix to act on. Leave blank for the main mix.",
}


# The numbered outputs, and the second off-by-one in this driver.
#
# vMix has up to four numbered outputs (2-4 on the 4K and Pro editions, one on
# the lower ones). Only 2, 3 and 4 can be re-pointed over the API: the function
# table has SetOutput2/3/4 and no SetOutput1, so Output 1 carries whatever it
# was set to in the vMix window and nothing here can move it. Asking for
# "SetOutput1" would be answered OK and do nothing, which is this device's
# signature failure, so the command's bound starts at 2 and the platform
# enforces it at dispatch.
#
# The SRT functions are the opposite shape: they take EVERY output, as a Value
# the vendor documents as "optional output number starting from 0. Leave blank
# to control Output 1 only." So Output 2 travels as Value=1 -- measured on the
# bench, where StopSRTOutput Value=1 killed a pull from Output 2 and
# StartSRTOutput Value=1 brought it straight back.
MAX_OUTPUTS = 4
FIRST_SETTABLE_OUTPUT = 2

# What a numbered output can be pointed at. The same six the vendor lists for
# SetOutput2/3/4, and the same six vMix's own Outputs dialog offers.
OUTPUT_SOURCES = ["Output", "Preview", "MultiView", "Replay", "Mix", "Input"]

# vMix calls its program feed "Output"; every AV integrator calls it Program.
# The device's own word is what the `source` state variable stores -- it has to
# match what they see in the vMix window -- and this is only the display name
# the stream picker reads.
_SOURCE_DISPLAY = {"Output": "Program"}

# vMix offers 10000 for an SRT output the first time you enable one, so it is
# the right guess for any single output. It cannot be right for two at once,
# which is what _output_srt_port's collision check is for.
DEFAULT_SRT_PORT = 10000


def srt_output_to_wire(output: Any) -> str | None:
    """Turn vMix's output number into the SRT functions' Value argument.

    Zero-based off Output 1, per the vendor's function table. Returns None for
    anything that is not one of the outputs vMix has, so a bad value is refused
    rather than sent as some other output's number.
    """
    try:
        number = int(str(output).strip())
    except (TypeError, ValueError):
        return None
    if not 1 <= number <= MAX_OUTPUTS:
        return None
    return str(number - 1)


def output_display_name(number: int, source: str, detail: str = "") -> str:
    """What the stream picker calls this output.

    The Video Panel plugin labels a discovered source from the child's own
    `label` (the user's, if they set one) or `name`, with no device prefix of
    its own -- so this carries enough to tell one vMix output from another in a
    list that also holds cameras and encoders.

    ``detail`` REPLACES the source word for the two sources that have a which:
    an output showing input 2 reads "Camera 2" rather than "Input Camera 2",
    and one showing a mix reads "Mix 2". A bare "Input" would be the least
    useful thing in a picker -- which input is the only question worth
    answering there.
    """
    word = detail or _SOURCE_DISPLAY.get(source, source)
    return f"vMix Output {number} - {word}" if word else f"vMix Output {number}"


def mix_to_wire(mix: Any) -> str | None:
    """Turn vMix's mix number into the Mix argument, or None for the main mix.

    Returns None for the main mix so the argument is left off entirely, which
    is what every command in this driver did before mixes existed.
    """
    try:
        number = int(str(mix).strip())
    except (TypeError, ValueError):
        return None
    if number <= MAIN_MIX:
        return None
    return str(number - 1)


def wire_to_mix(raw: Any) -> int | None:
    """The inverse, for the one place vMix REPORTS a mix number.

    An output pointed at a mix carries the mix as an attribute, and it is the
    wire's zero-based number rather than vMix's own -- measured on vMix 29 by
    setting Mix=0..3 and reading each back unchanged, so mix="0" is the main
    mix. Everything this driver publishes is vMix's number, so it converts
    back here; reading the attribute straight would label vMix's Mix 2 as
    "Mix 1" on the same page as a mix.2 child.
    """
    try:
        wire = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return wire + MAIN_MIX if wire >= 0 else None


# What every input publishes. Held out here because an input that carries a
# title gets a per-instance schema built from this plus one variable per text
# field, and the per-child schema REPLACES the type-level one rather than
# merging with it -- so the shared half has to be something both can name.
INPUT_STATE_VARIABLES: dict[str, dict[str, Any]] = {
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
}

# A title's text fields arrive as <text index name> children of the input and
# are named by whoever built the title, so one could collide with a variable
# above. The static half wins and the collision is logged: a field that loses
# is still settable, it just has to be typed rather than picked.
# The input variables flagged `control` for the UI Builder's value picker.
# Named here so a title input can stand them down — see _input_schema.
_AUDIO_CONTROL_VARS = ("muted", "volume", "balance")

TEXT_FIELD_VAR = {
    "type": "string", "label": "Text", "control": True,
    "help": (
        "Current text of this title field. Settable with Set Text. vMix has no "
        "activator for title text, so this follows the state poll rather than "
        "updating the instant somebody retypes it in the vMix window."
    ),
}


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
        "version": "2.2.0",
        # The connection lifecycle hooks this driver overrides landed in 0.24.0.
        "min_platform_version": "0.25.0",
        "author": "OpenAVC",
        "description": (
            "Controls vMix video production software via the TCP API. "
            "Supports transitions, input switching, audio, overlays, "
            "recording, streaming, titles, replay, PTZ, and the numbered "
            "outputs including their SRT streams."
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
                # The key has to be "firmware": that is one of the reserved
                # names the scan lifts off a probe onto the device record, and
                # anything else is captured into the evidence and then dropped,
                # so the card shows a vMix with no version.
                "extract": {
                    "firmware": {"regex": r"^VERSION OK (\S+)", "group": 1},
                },
                "extract_manufacturer": "StudioCoast",
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
                "for the next poll. A numbered output running SRT is offered "
                "to the Video Panel plugin as a viewable stream, so program "
                "or preview can be shown on a touch panel."
            ),
            "setup": (
                "1. Open vMix and go to Settings > Web Controller\n"
                "2. Ensure the TCP API is enabled (default port 8099)\n"
                "3. Enter the vMix PC's IP address and port below\n"
                "4. Leave both subscriptions on for live tally and live "
                "recording/overlay/audio state\n"
                "\n"
                "To show a vMix output on a panel, set up SRT on it in vMix: "
                "Settings > Outputs, click the cog beside the output, set the "
                "port, then tick Enable SRT with Type set to Listener. Set the "
                "port BEFORE you tick Enable SRT -- the field is locked while "
                "the output is running. Each output needs its own port (they "
                "all offer 10000, so change every one after the first), and "
                "the same number goes in the SRT Port field below. vMix "
                "reports that SRT is on but never which port it is on, so "
                "this is the one number OpenAVC cannot read for itself."
            ),
        },
        "default_config": {
            "host": "",
            "port": 8099,
            "poll_interval": 30,
            "subscribe_tally": True,
            "subscribe_acts": True,
            **{f"srt_port_{n}": DEFAULT_SRT_PORT for n in range(1, MAX_OUTPUTS + 1)},
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
            # One port per output, because vMix will not tell us. Its state
            # document says an output HAS SRT running and never says where --
            # checked, there is nothing else in the document either -- so the
            # number the user read off the Outputs dialog is the only source.
            # Every output defaults to 10000 because that is what vMix offers
            # each of them; two outputs cannot both keep it, and the driver
            # says so rather than publishing one right stream and one wrong one.
            **{
                f"srt_port_{n}": {
                    "type": "integer",
                    "default": DEFAULT_SRT_PORT,
                    "min": 0,
                    "max": 65535,
                    "label": f"Output {n} SRT Port",
                    "description": (
                        f"The SRT port set on Output {n} in vMix "
                        f"(Settings > Outputs > cog). 0 if this output has no "
                        f"SRT stream. Only used once vMix reports SRT running "
                        f"on it."
                    ),
                }
                for n in range(1, MAX_OUTPUTS + 1)
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
            "mix_list": {
                "type": "string",
                "label": "Mix List",
                "help": (
                    "JSON list of the mixes this production has, rebuilt on "
                    "every state poll. Feeds the Mix dropdowns. Value is "
                    "vMix's own mix number, main being 1."
                ),
            },
            "version": {"type": "string", "label": "vMix Version"},
            "edition": {"type": "string", "label": "vMix Edition"},
            "last_error": {
                "type": "string", "label": "Last Error",
                "help": (
                    "The most recent error message vMix returned for a "
                    "command, or a configuration problem this driver spotted "
                    "-- two outputs sharing one SRT port, say."
                ),
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
        # A production's inputs are discovered at runtime and change while the
        # show is live, which is what child entities are for: the roster is
        # registered from the XML state (and from the tally string, which can
        # arrive first), and an input removed in vMix is deregistered so its
        # state doesn't linger. Declared with no id padding, so every key keeps
        # the shape it has always had: device.<id>.input.<number>.<prop>.
        #
        # The type is dynamic because a title input publishes one state
        # variable per text field it contains, and only the device knows what
        # those are called. An input with no title fields gets the plain set.
        "child_entity_types": {
            "input": {
                "label": "Input",
                "label_plural": "Inputs",
                "dynamic": True,
                "id_format": {"type": "integer", "min": 1, "max": 1000},
                "state_variables": INPUT_STATE_VARIABLES,
                "summary_fields": ["title", "type", "tally"],
                "label_field": "title",
            },
            # vMix's extra mixes, each a whole second switcher with its own
            # program and preview. They appear only once the production has
            # Mix inputs, and there can be a lot of them, so they are a roster
            # rather than a fixed block of state variables.
            #
            # The id is vMix's OWN mix number, the one its window shows: mix 2
            # is the input called "Mix2". The Mix parameter on the wire counts
            # from zero off the main mix, so mix 2 is Mix=1 -- see MIX_PARAM
            # below. Publishing the wire number here would mean the number on
            # the device page and the number in vMix disagreed.
            "mix": {
                "label": "Mix",
                "label_plural": "Mixes",
                "id_format": {"type": "integer", "min": 2, "max": 64},
                "state_variables": {
                    "active": {
                        "type": "integer", "label": "Program Input",
                        "cloud_priority": "high", "control": True,
                    },
                    "preview": {
                        "type": "integer", "label": "Preview Input",
                        "cloud_priority": "high", "control": True,
                    },
                },
                "summary_fields": ["active", "preview"],
            },
            # vMix's numbered outputs. Each one is a picture -- program,
            # preview, the multiview, a mix or a single input -- and an SRT
            # output on top of it is how that picture gets onto a panel.
            #
            # The roster comes from the XML rather than being a fixed block of
            # four, because how many outputs exist is an edition question (one
            # on the lower editions, up to four on 4K and Pro) and a device
            # page listing outputs that cannot exist is a lie.
            "output": {
                "label": "Output",
                "label_plural": "Outputs",
                "id_format": {"type": "integer", "min": 1, "max": MAX_OUTPUTS},
                "state_variables": {
                    "name": {
                        "type": "string", "label": "Name",
                        "help": (
                            "What the stream picker calls this output. Follows "
                            "what the output is carrying."
                        ),
                    },
                    "source": {
                        "type": "string", "label": "Showing",
                        "cloud_priority": "high",
                        "help": (
                            "What this output is pointed at, in vMix's own "
                            "words: Output (the program feed), Preview, "
                            "MultiView, Replay, Mix or Input."
                        ),
                    },
                    # The two sources that need saying WHICH. vMix reports each
                    # as its own attribute, neither of which is in the vendor's
                    # example document -- both measured on vMix 29.
                    "source_input": {
                        "type": "integer", "label": "Showing Input",
                        "help": (
                            "Which input, when Showing is Input. 0 otherwise."
                        ),
                    },
                    "source_mix": {
                        "type": "integer", "label": "Showing Mix",
                        "help": (
                            "Which mix, when Showing is Mix, as vMix's own "
                            "number (the main mix is 1). 0 otherwise."
                        ),
                    },
                    "srt": {
                        "type": "boolean", "label": "SRT Running",
                        "cloud_priority": "high",
                        "help": (
                            "Whether this output's SRT stream is running right "
                            "now. It is runtime state, not configuration: "
                            "stopping an output clears this and keeps its "
                            "port, so false means enabled-but-stopped as well "
                            "as never-set-up."
                        ),
                    },
                    # The generic preview-source convention. The Video Panel
                    # plugin reads these two and lists the output as a stream
                    # with nothing typed; see openavc-api-reference.md.
                    "preview_url": {
                        "type": "string", "label": "Preview Source URL",
                        "cloud_priority": "low",
                        "help": (
                            "srt://<vMix host>:<port> while SRT is running and "
                            "this output's port is configured. Empty otherwise."
                        ),
                    },
                    "preview_format": {
                        "type": "string", "label": "Preview Type",
                        "cloud_priority": "low",
                    },
                },
                "summary_fields": ["name", "srt", "preview_url"],
                "label_field": "name",
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
                "params": {
                    "input": {"type": "string", "options_state": "input_list", "help": "Input number or name (optional, omit for current preview)"},
                    "mix": MIX_PARAM,
                },
                "help": "Instant cut transition to the specified input or current preview.",
            },
            "fade": {
                "label": "Fade",
                "params": {
                    "input": {"type": "string", "options_state": "input_list", "help": "Input number or name (optional)"},
                    "duration": {"type": "integer", "min": 0, "max": 60000, "help": "Fade duration in milliseconds"},
                    "mix": MIX_PARAM,
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
                    "mix": MIX_PARAM,
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
                    "mix": MIX_PARAM,
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
                "params": {
                    "input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"},
                    "mix": MIX_PARAM,
                },
                "help": "Send an input to preview.",
            },
            "active_input": {
                "label": "Active Input",
                "params": {
                    "input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"},
                    "mix": MIX_PARAM,
                },
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
                    "mix": MIX_PARAM,
                },
                "help": "Toggle an overlay channel on or off with the given input.",
            },
            "overlay_input_in": {
                "label": "Overlay In",
                "params": {
                    "channel": {"type": "integer", "required": True, "min": 1, "max": OVERLAY_CHANNELS, "help": "Overlay channel 1-8"},
                    "input": {"type": "string", "options_state": "input_list", "required": True, "help": "Input number or name"},
                    "mix": MIX_PARAM,
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
            # --- Outputs / SRT ---
            "set_output_source": {
                "label": "Set Output Source",
                "params": {
                    "output": {
                        "type": "integer", "required": True,
                        "min": FIRST_SETTABLE_OUTPUT, "max": MAX_OUTPUTS,
                        "label": "Output",
                        "help": (
                            "Which numbered output to re-point. Output 1 is "
                            "not settable over the API -- vMix has no "
                            "SetOutput1 -- so it keeps whatever the vMix "
                            "window gave it."
                        ),
                    },
                    "source": {
                        "type": "enum", "required": True, "values": OUTPUT_SOURCES,
                        "help": "What this output should carry.",
                    },
                    "input": {
                        "type": "string", "options_state": "input_list",
                        "help": (
                            "Which input, when the source is Input. Leaving "
                            "it blank puts the output on Preview, because "
                            "vMix reads a missing input as 0 and 0 is "
                            "preview -- measured, not a guess."
                        ),
                    },
                    "mix": {
                        "type": "string", "options_state": "mix_list",
                        "help": (
                            "Which mix, when the source is Mix. Blank is the "
                            "main mix."
                        ),
                    },
                },
                "help": (
                    "Point a numbered output at the program feed, preview, the "
                    "multiview, a mix or one input. One output is one picture, "
                    "so this changes it for everybody watching that output's "
                    "SRT stream, not just one panel."
                ),
            },
            "start_srt_output": {
                "label": "Start SRT Output",
                "params": {
                    "output": {
                        "type": "integer", "required": True,
                        "min": 1, "max": MAX_OUTPUTS, "label": "Output",
                    },
                },
                "help": (
                    "Start the SRT stream on an output that has SRT set up in "
                    "vMix. Does nothing on an output that has never been set "
                    "up -- there is no way to configure one from here."
                ),
            },
            "stop_srt_output": {
                "label": "Stop SRT Output",
                "params": {
                    "output": {
                        "type": "integer", "required": True,
                        "min": 1, "max": MAX_OUTPUTS, "label": "Output",
                    },
                },
                "help": (
                    "Stop an output's SRT stream. Its port survives, so "
                    "starting it again needs no trip back into vMix."
                ),
            },
            "toggle_srt_output": {
                "label": "Toggle SRT Output",
                "params": {
                    "output": {
                        "type": "integer", "required": True,
                        "min": 1, "max": MAX_OUTPUTS, "label": "Output",
                    },
                },
                "help": "Start an output's SRT stream if stopped, stop it if running.",
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
            # These two pick the input as a CHILD rather than off input_list,
            # because that is what lets the field name cascade: picking the
            # title populates the name dropdown from that title's own fields,
            # which the driver discovered from the device. The name stays a
            # string rather than an enum so a field vMix has not reported --
            # a GT title's "Headline.Text", say -- can still be typed.
            "set_text": {
                "label": "Set Text",
                "params": {
                    "input": {"type": "child_id", "child_type": "input", "required": True, "label": "Title Input"},
                    "selected_name": {
                        "type": "string",
                        "label": "Field",
                        "options_from": {"param": "input", "source": "child_schema"},
                        "help": "Text field to set. Pick one the title reports, or type a name (e.g. Headline.Text on a GT title).",
                    },
                    # Title text is the one thing on this driver that is poll-only:
                    # vMix has no activator for a text field, so a change made in
                    # the vMix window shows up on the next state read rather than
                    # immediately. Writing it from here is instant either way.
                    "value": {"type": "string", "required": True, "trim": False, "help": "Text to display"},
                },
                "help": "Set a text field in a title input.",
            },
            "set_image": {
                "label": "Set Image",
                "params": {
                    "input": {"type": "child_id", "child_type": "input", "required": True, "label": "Title Input"},
                    "selected_name": {
                        "type": "string",
                        "label": "Field",
                        "options_from": {"param": "input", "source": "child_schema"},
                        "help": "Image field to set (e.g. MyImage.Source).",
                    },
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
        "cut": {"fn": "Cut", "args": {"input": "Input", "mix": "Mix"}},
        "fade": {"fn": "Fade", "args": {"input": "Input", "duration": "Duration", "mix": "Mix"}},
        "cut_direct": {"fn": "CutDirect", "args": {"input": "Input"}},
        "fade_to_black": {"fn": "FadeToBlack", "args": {}},
        "transition": {"fn": "{effect}", "args": {"input": "Input", "duration": "Duration", "mix": "Mix"}},
        "transition_button": {"fn": "Transition{number}", "args": {}},
        "stinger": {"fn": "Stinger{number}", "args": {"input": "Input", "mix": "Mix"}},
        "set_fader": {"fn": "SetFader", "args": {"position": "Value"}},
        "quick_play": {"fn": "QuickPlay", "args": {"input": "Input"}},
        # Input switching
        "preview_input": {"fn": "PreviewInput", "args": {"input": "Input", "mix": "Mix"}},
        "active_input": {"fn": "ActiveInput", "args": {"input": "Input", "mix": "Mix"}},
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
        "overlay_input": {"fn": "OverlayInput{channel}", "args": {"input": "Input", "mix": "Mix"}},
        "overlay_input_in": {"fn": "OverlayInput{channel}In", "args": {"input": "Input", "mix": "Mix"}},
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
        # Outputs / SRT. The output number is baked into the SetOutput function
        # name (there is no generic SetOutput taking it as an argument), and it
        # travels zero-based as a Value on the three SRT functions.
        "set_output_source": {
            "fn": "SetOutput{output}",
            "args": {"source": "Value", "input": "Input", "mix": "Mix"},
        },
        "start_srt_output": {
            "fn": "StartSRTOutput", "args": {"output": "Value"},
            "zero_based": ("output",),
        },
        "stop_srt_output": {
            "fn": "StopSRTOutput", "args": {"output": "Value"},
            "zero_based": ("output",),
        },
        "toggle_srt_output": {
            "fn": "StartStopSRTOutput", "args": {"output": "Value"},
            "zero_based": ("output",),
        },
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
        # Mix numbers seen in the last XML state, same idea.
        self._known_mixes: set[int] = set()
        # Numbered outputs seen in the last XML state, same idea again: how
        # many exist is an edition question, and one removed should not linger.
        self._known_outputs: set[int] = set()
        # Input titles from the last XML state, so an output pointed at an
        # input can be named after it. _parse_inputs fills this and
        # _parse_outputs reads it, in that order within one document.
        self._input_titles: dict[int, str] = {}
        # The title-field names each input last reported, so a poll can tell a
        # title being retyped (values move) from one being swapped (names do).
        self._input_text_fields: dict[int, tuple[str, ...]] = {}

    def _ensure_input(self, num: str, text_fields: list[str] | None = None) -> bool:
        """Register input ``num`` as a child if it isn't already.

        Both the tally string and the XML state name inputs, and either can
        arrive first, so both call this before writing child state. Returns
        False for a number outside the declared id range (a production with
        more than 1000 inputs, or a malformed reply) — the caller skips it
        rather than letting a bad number abort the whole parse.

        ``text_fields`` names the title fields this input carries. They become
        state variables of their own on this child, which is what puts them in
        the Set Text field picker. A per-child schema can only be replaced by
        re-registering, so an input whose field NAMES change is deregistered
        first; changing field VALUES does not disturb anything.
        """
        try:
            local_id = int(num)
        except (TypeError, ValueError):
            return False

        wanted = tuple(text_fields or ())
        if self.is_child_registered("input", local_id):
            if text_fields is None or self._input_text_fields.get(local_id, ()) == wanted:
                return True
            # The title changed shape. Nothing else can move a child's schema.
            self.deregister_child("input", local_id)

        try:
            # Balance would otherwise start at its declared minimum, which is
            # hard left. Centre is the honest starting point, and an input
            # that carries no audio at all keeps it — vMix reports balance
            # only for inputs that have some.
            self.register_child(
                "input", local_id,
                initial_state={"balance": 0.0},
                schema=self._input_schema(wanted),
            )
        except ValueError as exc:
            log.warning(f"[{self.device_id}] Skipping input {num}: {exc}")
            return False
        self._input_text_fields[local_id] = wanted
        return True

    def _input_schema(self, text_fields: tuple[str, ...]) -> dict[str, Any]:
        """The state variables one input publishes, title fields included."""
        schema = dict(INPUT_STATE_VARIABLES)
        for name in text_fields:
            if name in schema:
                log.warning(
                    f"[{self.device_id}] Title field {name!r} shares its name "
                    f"with a built-in input property; it can still be set by "
                    f"typing the name, but it is not in the picker"
                )
                continue
            schema[name] = dict(TEXT_FIELD_VAR)

        if text_fields:
            # `control` does double duty in the platform: it orders the UI
            # Builder's value picker AND it scopes the field cascade on Set
            # Text. On a title that means the fader and mute would be offered
            # as text fields to write a headline into. An input carrying title
            # fields is a title, and nobody binds a panel fader to a title's
            # volume, so the audio half stands down here and the cascade
            # offers exactly the fields the title has.
            for name in _AUDIO_CONTROL_VARS:
                if name in schema and schema[name].get("control"):
                    var = dict(schema[name])
                    var.pop("control", None)
                    schema[name] = var
        return schema

    def _ensure_mix(self, number: int) -> bool:
        """Register one of vMix's extra mixes as a child."""
        if self.is_child_registered("mix", number):
            return True
        try:
            self.register_child("mix", number)
        except ValueError as exc:
            log.warning(f"[{self.device_id}] Skipping mix {number}: {exc}")
            return False
        return True

    def _ensure_output(self, number: int) -> bool:
        """Register one of vMix's numbered outputs as a child."""
        if self.is_child_registered("output", number):
            return True
        try:
            self.register_child("output", number)
        except ValueError as exc:
            log.warning(f"[{self.device_id}] Skipping output {number}: {exc}")
            return False
        return True

    def _output_srt_port(self, number: int) -> int:
        """The SRT port configured for one output, or 0 if there isn't one.

        vMix says an output has SRT running and never says on which port, so
        this is the number the user copied out of the Outputs dialog. Anything
        that isn't a usable port reads as "not configured", which publishes no
        stream rather than a wrong one.
        """
        try:
            port = int(self.config.get(f"srt_port_{number}", 0) or 0)
        except (TypeError, ValueError):
            return 0
        return port if 1 <= port <= 65535 else 0

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
            for name in ("channel", "number", "bus", "effect", "output"):
                token = "{" + name + "}"
                if token not in function:
                    continue
                if name not in params or params[name] in (None, ""):
                    raise ValueError(f"{command} requires the '{name}' parameter")
                function = function.replace(token, str(params[name]))
                consumed.add(name)

        zero_based = spec.get("zero_based", ())
        parts: list[str] = []
        for name, key in spec["args"].items():
            if name in consumed:
                continue
            value = params.get(name)
            if value is None or value == "":
                continue
            if name in zero_based:
                # The SRT output number, which the vendor documents as
                # "starting from 0". Refuse an out-of-range one rather than
                # send it as some other output's number.
                wire = srt_output_to_wire(value)
                if wire is None:
                    raise ValueError(
                        f"{command}: output must be 1-{MAX_OUTPUTS}, got {value!r}"
                    )
                parts.append(f"{key}={wire}")
                continue
            if name == "mix":
                # vMix's mix number in, the wire's zero-based one out. The main
                # mix sends no argument at all.
                wire = mix_to_wire(value)
                if wire is None:
                    continue
                parts.append(f"{key}={wire}")
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
        self._parse_mixes(root)
        self._parse_outputs(root)
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
        titles: dict[int, str] = {}

        for inp in inputs.findall("input"):
            input_count += 1
            num = inp.get("number", str(input_count))
            title = inp.get("title", "")

            seen.add(num)
            if num.isdigit() and title:
                titles[int(num)] = title
            input_options.append(
                {"value": num, "label": f"{num}: {title}" if title else num}
            )

            # A title input carries its text fields as children of the input:
            # <text index="0" name="Headline">Hello</text>. The name is what
            # SetText's SelectedName wants, so each becomes a state variable
            # on this child and the value is published under it.
            texts: dict[str, str] = {}
            for text_el in inp.findall("text"):
                field = (text_el.get("name") or "").strip()
                if field:
                    texts[field] = text_el.text or ""

            if not self._ensure_input(num, sorted(texts)):
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

            for field, text_value in texts.items():
                # A field whose name collided with a built-in property has no
                # variable of its own; _input_schema already said so.
                if field not in INPUT_STATE_VARIABLES:
                    updates[field] = text_value

            self.set_child_state_batch("input", int(num), updates)

        # Read by _parse_outputs later in this same document, to name an
        # output after the input it is showing.
        self._input_titles = titles

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
            else:
                self._input_text_fields.pop(int(gone), None)
        self._known_inputs = seen

    def _parse_mixes(self, root: ET.Element) -> None:
        """Register vMix's extra mixes and rebuild the Mix picker.

        A <mix> block exists only for a mix the production actually has, so
        this roster is empty on the ordinary single-mix system. The main mix
        has no block of its own — it is the document's own active/preview —
        so it is added to the picker by hand rather than found here.
        """
        seen: set[int] = set()
        options: list[dict[str, str]] = [
            {"value": str(MAIN_MIX), "label": "Main"}
        ]

        for mix in root.findall("mix"):
            raw = mix.get("number", "")
            if not raw.isdigit():
                continue
            number = int(raw)
            seen.add(number)
            options.append({"value": raw, "label": f"Mix {raw}"})
            if not self._ensure_mix(number):
                continue
            updates: dict[str, Any] = {}
            for element, prop in (("active", "active"), ("preview", "preview")):
                found = mix.find(element)
                if found is not None and found.text:
                    try:
                        updates[prop] = int(found.text.strip())
                    except ValueError:
                        pass
            if updates:
                self.set_child_state_batch("mix", number, updates)

        for gone in self._known_mixes - seen:
            try:
                self.deregister_child("mix", gone)
            except ValueError:
                pass
        self._known_mixes = seen

        self.set_state("mix_list", json.dumps(options))

    def _parse_outputs(self, root: ET.Element) -> None:
        """Register vMix's numbered outputs and derive their preview streams.

        Each output reports what it is carrying and, when SRT is live on it, an
        ``srt="True"`` attribute. That attribute is the only thing vMix says
        about SRT -- there is no port anywhere in this document -- so the URL is
        built from the port the user configured for that output.

        Two outputs cannot share a port (vMix could not bind it twice), so if
        the configuration says they do, one of the two numbers is stale. Rather
        than publish one right stream and one wrong one, both go quiet and the
        driver says which outputs and which port.
        """
        found: dict[int, ET.Element] = {}
        for out in self._output_elements(root):
            raw = out.get("number", "")
            if not raw.isdigit():
                continue
            number = int(raw)
            if not 1 <= number <= MAX_OUTPUTS:
                continue
            found[number] = out

        # Which SRT ports are claimed by more than one running output.
        ports: dict[int, list[int]] = {}
        for number, out in found.items():
            if out.get("srt", "").strip().lower() != "true":
                continue
            port = self._output_srt_port(number)
            if port:
                ports.setdefault(port, []).append(number)
        clashing = {
            number
            for port, numbers in ports.items()
            if len(numbers) > 1
            for number in numbers
        }
        for port, numbers in sorted(ports.items()):
            if len(numbers) > 1:
                message = (
                    f"Outputs {', '.join(str(n) for n in sorted(numbers))} are "
                    f"configured for the same SRT port ({port}), which vMix "
                    f"cannot do. Set each output's SRT Port to the number "
                    f"shown beside it in vMix: Settings > Outputs."
                )
                self.set_state(self.LAST_ERROR_PROPERTY, message)
                log.warning(f"[{self.device_id}] {message}")

        for number, out in sorted(found.items()):
            if not self._ensure_output(number):
                continue
            source = (out.get("source") or "").strip()
            source_input, source_mix, detail = self._output_detail(source, out)
            srt = out.get("srt", "").strip().lower() == "true"
            port = self._output_srt_port(number) if srt else 0
            host = str(self.config.get("host", "")).strip()
            url = (
                f"srt://{host}:{port}"
                if srt and port and host and number not in clashing
                else ""
            )
            self.set_child_state_batch("output", number, {
                "name": output_display_name(number, source, detail),
                "source": source,
                "source_input": source_input,
                "source_mix": source_mix,
                "srt": srt,
                # The generic preview-source convention: an empty URL means
                # "no stream right now", and the format goes with it so a
                # consumer never sees a format pointing at nothing.
                "preview_url": url,
                "preview_format": "srt" if url else "",
            })

        for gone in self._known_outputs - set(found):
            try:
                self.deregister_child("output", gone)
            except ValueError:
                pass
        self._known_outputs = set(found)

    def _output_detail(
        self, source: str, out: ET.Element
    ) -> tuple[int, int, str]:
        """Which input or mix an output is showing, and how to say it.

        Only two of the six sources have a "which", and vMix names each with
        its own attribute -- inputNumber and mix. Neither is in the vendor's
        example document; both were measured on vMix 29.

        The mix attribute is the WIRE number, zero-based, so it converts back
        to vMix's own (see wire_to_mix). The input attribute is the input's
        real number and needs no conversion; the title is used where the poll
        has seen one, because "Output 4 - Camera 2" is worth more in a picker
        than "Output 4 - Input 2".
        """
        if source == "Input":
            try:
                number = int((out.get("inputNumber") or "").strip())
            except (TypeError, ValueError):
                return 0, 0, ""
            title = self._input_titles.get(number, "")
            return number, 0, title or f"Input {number}"
        if source == "Mix":
            mix = wire_to_mix(out.get("mix"))
            if mix is None:
                return 0, 0, ""
            return 0, mix, f"Mix {mix}"
        return 0, 0, ""

    @staticmethod
    def _output_elements(root: ET.Element) -> list[ET.Element]:
        """The numbered outputs, which are not the only thing in <outputs>.

        vMix 29 lists its Fullscreen feeds in the same container and numbers
        them from 1 as well, so a document with four numbered outputs holds six
        <output> elements and two of the numbers appear twice. Reading them all
        would silently attribute a Fullscreen feed's state to Output 1.
        """
        container = root.find("outputs")
        if container is None:
            return []
        return [
            el for el in container.findall("output")
            if (el.get("type") or "output").strip().lower() == "output"
        ]

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
