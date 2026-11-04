"""
OpenAVC Blackmagic HyperDeck driver.

Controls Blackmagic Design HyperDeck disk recorders (HyperDeck Studio,
Shuttle and Extreme) over the HyperDeck Ethernet Protocol, a text protocol on
TCP 9993. Source: "HyperDeck Ethernet Protocol", Blackmagic Design developer
information, December 2024.

Why Python: the deck answers in blocks under a numbered header that a blank
line ends (``208 transport info:`` ... blank), and the keys repeat from block
to block (``status:`` and ``slot id:`` are in both the transport and the slot
blocks), so a YAML line rule cannot tell which block a line belongs to.
Requests go one at a time, a request ends at a one-line reply or at the blank
line that closes its block, and asynchronous 5xx blocks arrive between them on
the same connection.

Push and poll: after connecting the driver sends ``notify: transport: true
slot: true remote: true configuration: true``, and the deck then reports
transport, slot, remote and configuration changes as 508 / 502 / 510 / 511
blocks. The format of the display-timecode notice is not documented, so the
timecode moves with the poll: ``transport info`` every poll, the active slot
while recording (its recording time remaining), and device info, remote,
configuration, every slot and the timeline's clip list every twelfth poll.
``ping`` is the liveness check.

Wire rules from the document, as built:
- The server ends lines with CR LF; the client may send LF.
- A successful reply is ``200 ok`` or a ``2xx text:`` block; a failure is one
  ``1xx text`` line; asynchronous messages are 5xx blocks and never answer a
  request. ``213 deck rebooting`` is a success after which the deck closes the
  connection.
- On connecting the deck sends a ``500 connection info:`` block, or
  ``120 connection failed`` and closes the connection when it already serves
  as many clients as it accepts.
- ``111 remote control disabled`` refuses any change while the deck's Remote
  setting is off. ``remote: override: true`` lifts that for one connection
  while it stays open, so the Remote Override option sends it on every
  connection.
- With the protocol's access set to "Enabled with security" (the HyperDeck
  Setup manual), a command is answered ``122 authentication required``; the
  driver then sends the multiline ``authenticate:`` and repeats the command
  once. Not tried on a deck. Nor is TLS: no document gives its details.
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl as ssl_module
from dataclasses import dataclass, field
from typing import Any

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.utils.logger import get_logger

try:
    from openavc.drivers.base import CommandRefusedError
except ImportError:  # OpenAVC before 0.37.1
    CommandRefusedError = RuntimeError

log = get_logger(__name__)

DEFAULT_PORT = 9993
# The deck greets the moment the connection opens.
BANNER_TIMEOUT_S = 5.0
REQUEST_TIMEOUT_S = 5.0
# A timeline of many clips is a long reply.
CLIPS_TIMEOUT_S = 10.0
# A reply that arrives after its request timed out is waited for this long
# before the next request goes out; past it the connection is reopened, since
# every later reply would answer the wrong question.
LATE_REPLY_S = 3.0
# Poll cycles between full reads, with the deck's notices on and without them.
FULL_READ_EVERY = 12
FULL_READ_EVERY_UNNOTIFIED = 3
# The clip picker lists this many clips; a later clip is typed by number.
CLIP_OPTIONS_MAX = 200
SLOT_MAX = 16
# A line longer than this is not the protocol; the buffer is dropped.
RX_LIMIT = 1 << 20

REMOTE_DISABLED = 111
CONNECTION_FAILED = 120
AUTH_REFUSED = 121
AUTH_REQUIRED = 122
DECK_REBOOTING = 213
# Refusals a read meets on a deck with nothing to read (no disk, an empty
# timeline, no such clip): answers, not faults.
EMPTY_READ_CODES = frozenset({105, 107, 112})

_CODE_LINE = re.compile(r"^(\d{3})(?:\s(.*))?$")
# clips get: version: 2: "{id}: {start} {duration} {in} {out} {name}".
_CLIP_V2 = re.compile(r"^(\d+):\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.+)$")
# version 1: "{id}: {name} {start} {duration}".
_CLIP_V1 = re.compile(r"^(\d+):\s+(.+?)\s+(\d\d:\d\d:\d\d[:;]\d\d)\s+(\d\d:\d\d:\d\d[:;]\d\d)$")
_TIMECODE = r"^[+-]?\d{2}:\d{2}:\d{2}:\d{2}$"

REFUSED_CONNECTION = (
    "The HyperDeck refused the connection because every control connection "
    "it accepts is in use (120 connection failed). Disconnect another "
    "controller, such as an ATEM switcher or a second control system, and "
    "this one connects on the next try."
)

# Device setting -> (configuration parameter, what the refusal names).
CONFIG_SETTINGS = {
    "video_input": ("video input", "the video input"),
    "audio_input": ("audio input", "the audio input"),
    "file_format": ("file format", "the file format"),
    "audio_codec": ("audio codec", "the audio codec"),
    "record_prefix": ("record prefix", "the record prefix"),
    "append_timestamp": ("append timestamp", "Append Timestamp"),
    "record_trigger": ("record trigger", "the record trigger"),
    "timecode_input": ("timecode input", "the timecode input"),
}
# configuration parameter -> state variable.
CONFIG_STATE = {wire: key for key, (wire, _label) in CONFIG_SETTINGS.items()}


class _HyperDeckReply:
    """One answer from the deck: a code, its text, and a block's lines."""

    __slots__ = ("code", "text", "params", "lines")

    def __init__(self, code: int, text: str, params: dict[str, str], lines: list[str] | None):
        self.code = code
        self.text = text
        self.params = params
        self.lines = lines

    @property
    def ok(self) -> bool:
        return 200 <= self.code < 300

    def words(self) -> str:
        return f"{self.code} {self.text}".strip()


@dataclass
class _Pending:
    command: str
    future: asyncio.Future = field(repr=False)


class BlackmagicHyperDeckDriver(BaseDriver):
    """Blackmagic HyperDeck Ethernet Protocol driver."""

    DRIVER_INFO = {
        "id": "blackmagic_hyperdeck",
        "name": "Blackmagic HyperDeck",
        "manufacturer": "Blackmagic Design",
        "category": "video",
        "version": "1.0.1",
        "author": "OpenAVC",
        # Computed: Restart's confirm needs 0.36.0. Everything newer the code
        # calls is looked up first (CommandRefusedError, local_address).
        "min_platform_version": "0.36.0",
        "description": (
            "Controls Blackmagic Design HyperDeck disk recorders (HyperDeck "
            "Studio, Shuttle and Extreme) over the HyperDeck Ethernet Protocol "
            "(TCP 9993): record, play and stop, clip and timecode navigation, "
            "slot selection, recording settings, and transport, slot and "
            "remote status the deck reports as it changes."
        ),
        "source_url": "https://documents.blackmagicdesign.com/DeveloperManuals/HyperDeckEthernetProtocol.pdf",
        "tags": ["recorder", "playback", "hyperdeck", "broadcast", "recording", "blackmagic"],
        "verified": False,
        "simulated": True,
        "protocols": ["hyperdeck"],
        "ports": [9993],
        "transport": "tcp",
        "discovery": {
            # The deck sends "500 connection info:" with its model the moment
            # a client connects, so a connect-only probe reads the banner. No
            # document names an mDNS service. Blackmagic's IEEE blocks cover
            # every Blackmagic product (ATEM, Videohub, cameras), so no oui.
            "port_open": [9993],
            "manufacturer_alias": ["blackmagic", "blackmagic design", "bmd"],
            "tcp_probe": {
                "port": 9993,
                "expect_regex": r"^500 connection info:",
                "extract_manufacturer": "Blackmagic Design",
                "extract": {
                    "model": {"regex": r"model:\s*([^\r\n]+)"},
                },
                "timeout_ms": 3000,
            },
        },
        "compatible_models": [
            {
                "manufacturer": "Blackmagic Design",
                "models": [
                    "HyperDeck Studio HD Mini",
                    "HyperDeck Studio HD Plus",
                    "HyperDeck Studio HD Pro",
                    "HyperDeck Studio 4K Pro",
                ],
                "confidence": "untested",
                "notes": (
                    "Built from the HyperDeck Ethernet Protocol (December "
                    "2024). Video inputs, file formats and the number of "
                    "slots differ by model; the deck refuses a choice it "
                    "does not have."
                ),
            },
            {
                "manufacturer": "Blackmagic Design",
                "models": ["HyperDeck Shuttle HD", "HyperDeck Shuttle 4K Pro"],
                "confidence": "untested",
                "notes": "Same protocol. The Shuttle HD records from HDMI.",
            },
            {
                "manufacturer": "Blackmagic Design",
                "models": ["HyperDeck Extreme 8K HDR", "HyperDeck Extreme 4K HDR"],
                "confidence": "untested",
                "notes": "Same protocol, with the HDR dynamic range readings.",
            },
        ],
        "help": {
            "overview": (
                "Blackmagic HyperDeck recorders over the HyperDeck Ethernet "
                "Protocol (TCP 9993). Record, play, stop, jump between clips "
                "or to a timecode, shuttle, choose the recording slot, and set "
                "the input, codec and file naming. Each storage slot is shown "
                "with its disk, its status and the recording time left. The "
                "deck reports transport and slot changes as they happen; the "
                "timecode is read every poll."
            ),
            "setup": (
                "1. Give the HyperDeck a fixed IP address (on the deck, or in "
                "Blackmagic HyperDeck Setup) and enter it here. Control uses "
                "TCP 9993.\n"
                "2. HyperDeck Setup > Network Access > HyperDeck Ethernet "
                "Protocol: Enabled needs no login. With Enabled with security, "
                "enter the username and password set under Secure Login "
                "Settings. For the security only option, also turn on TLS.\n"
                "3. Remote: the deck refuses changes from the network while "
                "its Remote setting is off. Turn it on at the deck, or turn "
                "on Remote Override, which lets this connection control the "
                "deck whatever the setting.\n"
                "4. The deck accepts a limited number of control connections. "
                "If it reports that every connection is in use, disconnect "
                "another controller."
            ),
        },
        "default_config": {
            "host": "",
            "port": 9993,
            "username": "",
            "password": "",
            "ssl": False,
            "verify_ssl": False,
            "remote_override": False,
            "poll_interval": 5,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {
                "type": "integer", "default": 9993, "label": "TCP Port",
                "description": "HyperDeck Ethernet Protocol port, 9993.",
            },
            "username": {
                "type": "string", "default": "", "label": "Username",
                "description": (
                    "Only when the deck's Ethernet protocol access is Enabled "
                    "with security."
                ),
            },
            "password": {
                "type": "string", "default": "", "secret": True, "label": "Password",
                "description": (
                    "Only when the deck's Ethernet protocol access is Enabled "
                    "with security."
                ),
            },
            "ssl": {
                "type": "boolean", "default": False, "label": "TLS",
                "description": (
                    "Encrypts the connection, for the deck's security only "
                    "access option."
                ),
            },
            "verify_ssl": {
                "type": "boolean", "default": False, "label": "Verify Certificate",
                "description": (
                    "Leave off for the deck's self-signed certificate; turn on "
                    "when it has one a certificate authority signed."
                ),
            },
            "remote_override": {
                "type": "boolean", "default": False, "label": "Remote Override",
                "description": (
                    "Controls the deck even while its Remote setting is off. "
                    "Applies to this connection only."
                ),
            },
            "poll_interval": {
                "type": "integer", "default": 5, "min": 1, "label": "Poll Interval (sec)",
                "description": "How often the timecode and transport are read.",
            },
        },
        "state_variables": {
            "transport_status": {
                "type": "enum", "label": "Transport",
                "values": ["preview", "stopped", "play", "forward", "rewind", "jog", "shuttle", "record"],
                "control": True, "cloud_priority": "high",
                "help": "Preview shows the input; the others are the playback and record states.",
            },
            "recording": {
                "type": "boolean", "label": "Recording", "control": True, "cloud_priority": "high",
            },
            "speed": {
                "type": "integer", "label": "Speed", "min": -5000, "max": 5000, "unit": "%",
                "help": "Playback speed in percent of normal; negative is backwards.",
            },
            "clip_id": {"type": "integer", "label": "Clip", "help": "The current clip's number on the timeline."},
            "active_slot": {
                "type": "integer", "label": "Active Slot", "control": True,
                "help": "The slot the deck records to and plays from.",
            },
            "timecode": {"type": "string", "label": "Timecode", "cloud_priority": "low"},
            "display_timecode": {
                "type": "string", "label": "Display Timecode", "cloud_priority": "low",
                "help": "The timecode on the deck's front panel.",
            },
            "loop": {"type": "boolean", "label": "Loop"},
            "single_clip": {"type": "boolean", "label": "Single Clip"},
            "video_format": {"type": "string", "label": "Video Format"},
            "input_video_format": {"type": "string", "label": "Input Video Format"},
            "dynamic_range": {"type": "string", "label": "Dynamic Range"},
            "reference_locked": {"type": "boolean", "label": "Reference Locked"},
            "remote_enabled": {
                "type": "boolean", "label": "Remote Control", "cloud_priority": "high",
                "help": "Off: the deck refuses changes from the network unless Remote Override is on.",
            },
            "remote_override": {
                "type": "boolean", "label": "Remote Override",
                "help": "This connection controls the deck whatever its Remote setting.",
            },
            "clip_count": {"type": "integer", "label": "Clips on Timeline"},
            "clip_options": {
                "type": "string", "label": "Clip List", "cloud_priority": "low",
                "help": "The timeline's clips as a picker list (the first 200).",
            },
            "slot_count": {"type": "integer", "label": "Slots"},
            "video_input": {
                "type": "enum", "label": "Video Input",
                "values": ["SDI", "4xSDI", "HDMI", "component", "composite"],
            },
            "audio_input": {"type": "enum", "label": "Audio Input", "values": ["embedded", "XLR", "RCA"]},
            "file_format": {
                "type": "enum", "label": "File Format",
                "values": [
                    "QuickTimeProResHQ", "QuickTimeProRes", "QuickTimeProResLT", "QuickTimeProResProxy",
                    "H.264High", "H.264Medium", "H.264Low", "H.264High_SDI", "H.264High10_422",
                    "H.265High", "H.265Medium", "H.265Low", "H.265High_SDI", "H.265High_422",
                    "DNxHD220x", "QuickTimeDNxHD220x", "DNxHD145", "QuickTimeDNxHD145",
                    "DNxHD45", "QuickTimeDNxHD45", "DNxHR_HQX", "QuickTimeDNxHR_HQX",
                    "DNxHR_SQ", "QuickTimeDNxHR_SQ", "DNxHR_LB", "QuickTimeDNxHR_LB",
                ],
            },
            "audio_codec": {"type": "enum", "label": "Audio Codec", "values": ["PCM", "AAC"]},
            "record_prefix": {"type": "string", "label": "Record Prefix"},
            "append_timestamp": {"type": "boolean", "label": "Append Timestamp"},
            "record_trigger": {
                "type": "enum", "label": "Record Trigger", "values": ["none", "recordbit", "timecoderun"],
            },
            "timecode_input": {
                "type": "enum", "label": "Timecode Input",
                "values": ["external", "embedded", "internal", "preset", "clip"],
            },
            "model": {"type": "string", "label": "Model", "cloud_priority": "low"},
            "device_name": {"type": "string", "label": "Device Name", "cloud_priority": "low"},
            "unique_id": {"type": "string", "label": "Unique ID", "cloud_priority": "low"},
            "software_version": {"type": "string", "label": "Software Version", "cloud_priority": "low"},
            "protocol_version": {"type": "string", "label": "Protocol Version", "cloud_priority": "low"},
            "last_error": {"type": "string", "label": "Last Error"},
        },
        "child_entity_types": {
            "slot": {
                "label": "Slot",
                "label_plural": "Slots",
                "id_format": {"type": "integer", "min": 1, "max": 16},
                "state_variables": {
                    "status": {
                        "type": "enum", "label": "Status",
                        "values": ["empty", "mounting", "error", "mounted"],
                        "cloud_priority": "high",
                    },
                    "slot_name": {"type": "string", "label": "Slot Name"},
                    "device_name": {"type": "string", "label": "Disk"},
                    "volume_name": {"type": "string", "label": "Volume Name"},
                    "recording_time": {
                        "type": "integer", "label": "Recording Time Left", "unit": "s",
                        "cloud_priority": "low",
                        "help": "Recording time the disk has left at the current settings.",
                    },
                    "remaining_size": {"type": "integer", "label": "Free Space", "unit": "bytes", "cloud_priority": "low"},
                    "total_size": {"type": "integer", "label": "Size", "unit": "bytes", "cloud_priority": "low"},
                    "video_format": {"type": "string", "label": "Video Format"},
                    "blocked": {"type": "boolean", "label": "Blocked"},
                },
                "summary_fields": ["status", "volume_name", "recording_time"],
                "label_field": "slot_name",
            },
        },
        "device_settings": {
            "remote_enabled": {
                "type": "boolean", "label": "Remote Control", "state_key": "remote_enabled",
                "default": True, "setup": False,
                "help": "Lets controllers on the network change the deck. Off, they are refused unless Remote Override is on.",
            },
            "video_input": {
                "type": "enum", "label": "Video Input", "state_key": "video_input",
                "values": ["SDI", "4xSDI", "HDMI", "component", "composite"],
                "default": "SDI", "setup": False,
                "help": "The input the deck records. The choices vary by model.",
            },
            "audio_input": {
                "type": "enum", "label": "Audio Input", "state_key": "audio_input",
                "values": ["embedded", "XLR", "RCA"], "default": "embedded", "setup": False,
                "help": "Embedded audio, or the XLR or RCA inputs on models that have them.",
            },
            "file_format": {
                "type": "enum", "label": "File Format", "state_key": "file_format",
                "values": [
                    "QuickTimeProResHQ", "QuickTimeProRes", "QuickTimeProResLT", "QuickTimeProResProxy",
                    "H.264High", "H.264Medium", "H.264Low", "H.264High_SDI", "H.264High10_422",
                    "H.265High", "H.265Medium", "H.265Low", "H.265High_SDI", "H.265High_422",
                    "DNxHD220x", "QuickTimeDNxHD220x", "DNxHD145", "QuickTimeDNxHD145",
                    "DNxHD45", "QuickTimeDNxHD45", "DNxHR_HQX", "QuickTimeDNxHR_HQX",
                    "DNxHR_SQ", "QuickTimeDNxHR_SQ", "DNxHR_LB", "QuickTimeDNxHR_LB",
                ],
                "default": "QuickTimeProResHQ", "setup": False,
                "help": (
                    "The recording codec. The formats vary by model. Changing it "
                    "can restart the deck, which then reconnects."
                ),
            },
            "audio_codec": {
                "type": "enum", "label": "Audio Codec", "state_key": "audio_codec",
                "values": ["PCM", "AAC"], "default": "PCM", "setup": False,
                "help": "The audio codec recorded with the video.",
            },
            "record_prefix": {
                "type": "string", "label": "Record Prefix", "state_key": "record_prefix",
                "default": "", "setup": False, "regex": r"^[^\r\n]*$",
                "help": "The start of each new clip's file name.",
            },
            "append_timestamp": {
                "type": "boolean", "label": "Append Timestamp", "state_key": "append_timestamp",
                "default": False, "setup": False,
                "help": "Adds the date and time to each new clip's file name.",
            },
            "record_trigger": {
                "type": "enum", "label": "Record Trigger", "state_key": "record_trigger",
                "values": ["none", "recordbit", "timecoderun"], "default": "none", "setup": False,
                "help": (
                    "none: records when told. recordbit: records while the SDI "
                    "input's record flag is set. timecoderun: records while the "
                    "input's timecode runs."
                ),
            },
            "timecode_input": {
                "type": "enum", "label": "Timecode Input", "state_key": "timecode_input",
                "values": ["external", "embedded", "internal", "preset", "clip"],
                "default": "internal", "setup": False,
                "help": "Where the recorded timecode comes from.",
            },
        },
        "quick_actions": ["play", "stop", "record"],
        "actions": [
            {
                "id": "test_connection",
                "kind": "setup",
                "label": "Test Connection",
                "icon": "search",
                "availability": "always",
            },
        ],
        "commands": {
            "play": {
                "label": "Play",
                "params": {
                    "speed": {
                        "type": "integer", "label": "Speed", "min": -5000, "max": 5000, "unit": "%",
                        "help": "Percent of normal speed; negative plays backwards. Blank plays at normal speed.",
                    },
                    "loop": {"type": "boolean", "label": "Loop", "help": "Starts again at the end instead of stopping."},
                    "single_clip": {
                        "type": "boolean", "label": "Single Clip",
                        "help": "Plays only the current clip instead of the rest of the timeline.",
                    },
                },
                "sets": {"transport_status": "play"},
                "help": "Plays from the current position.",
            },
            "play_clip": {
                "label": "Play Clip",
                "params": {
                    "clip_id": {
                        "type": "integer", "label": "Clip", "required": True, "min": 1,
                        "options_state": "clip_options",
                        "help": "The clip's number on the timeline.",
                    },
                    "single_clip": {
                        "type": "boolean", "label": "Single Clip",
                        "help": "Stops at the end of this clip instead of playing on.",
                    },
                    "loop": {"type": "boolean", "label": "Loop", "help": "Starts again at the end instead of stopping."},
                },
                "sets": {"transport_status": "play", "clip_id": "{clip_id}"},
                "help": "Plays the chosen clip from its start.",
            },
            "stop": {
                "label": "Stop",
                "params": {},
                "sets": {"recording": False},
                "help": "Stops playback or recording.",
            },
            "record": {
                "label": "Record",
                "params": {},
                "sets": {"transport_status": "record", "recording": True},
                "help": "Starts recording the input to the active slot.",
            },
            "record_named": {
                "label": "Record Named Clip",
                "params": {
                    "name": {
                        "type": "string", "label": "Clip Name", "required": True,
                        "pattern": r"^[^\r\n]+$",
                        "help": "The new clip's file name.",
                    },
                },
                "sets": {"transport_status": "record", "recording": True},
                "help": "Starts recording the input to a clip with the given name.",
            },
            "record_spill": {
                "label": "Spill Recording",
                "params": {
                    "slot": {
                        "type": "child_id", "child_type": "slot", "label": "Slot",
                        "help": "The slot to carry on in. Blank: the next slot.",
                    },
                },
                "help": "Carries the current recording on in another slot without stopping it.",
            },
            "preview_on": {
                "label": "Show Input (Preview)",
                "params": {},
                "sets": {"transport_status": "preview"},
                "help": "Shows the live input on the deck's outputs. Stops playback.",
            },
            "preview_off": {
                "label": "Show Playback",
                "params": {},
                "help": "Shows playback on the deck's outputs instead of the input.",
            },
            "next_clip": {
                "label": "Next Clip",
                "params": {},
                "help": "Moves to the start of the next clip.",
            },
            "previous_clip": {
                "label": "Previous Clip",
                "params": {},
                "help": "Moves to the start of the previous clip.",
            },
            "goto_clip": {
                "label": "Go To Clip",
                "params": {
                    "clip_id": {
                        "type": "integer", "label": "Clip", "required": True, "min": 1,
                        "options_state": "clip_options",
                        "help": "The clip's number on the timeline.",
                    },
                },
                "sets": {"clip_id": "{clip_id}"},
                "help": "Moves to the start of the chosen clip without playing it.",
            },
            "goto_clip_start": {
                "label": "Go To Clip Start",
                "params": {},
                "help": "Moves to the start of the current clip.",
            },
            "goto_clip_end": {
                "label": "Go To Clip End",
                "params": {},
                "help": "Moves to the end of the current clip.",
            },
            "goto_timeline_start": {
                "label": "Go To Timeline Start",
                "params": {},
                "help": "Moves to the start of the first clip.",
            },
            "goto_timeline_end": {
                "label": "Go To Timeline End",
                "params": {},
                "help": "Moves to the end of the last clip.",
            },
            "goto_timecode": {
                "label": "Go To Timecode",
                "params": {
                    "timecode": {
                        "type": "string", "label": "Timecode", "required": True,
                        "pattern": r"^[+-]?\d{2}:\d{2}:\d{2}:\d{2}$",
                        "help": "HH:MM:SS:FF on the timeline. A leading + or - moves by that much.",
                    },
                },
                "help": "Moves to a timecode on the timeline, or forward or back by a duration.",
            },
            "shuttle": {
                "label": "Shuttle",
                "params": {
                    "speed": {
                        "type": "integer", "label": "Speed", "required": True,
                        "min": -5000, "max": 5000, "unit": "%",
                        "help": "Percent of normal speed; negative is backwards.",
                    },
                },
                "sets": {"transport_status": "shuttle"},
                "help": "Shuttles through the timeline at the given speed.",
            },
            "select_slot": {
                "label": "Select Slot",
                "params": {
                    "slot": {"type": "child_id", "child_type": "slot", "label": "Slot", "required": True},
                },
                "sets": {"active_slot": "{slot}"},
                "help": "Makes the chosen slot the one the deck records to and plays from.",
            },
            "identify_on": {
                "label": "Identify",
                "params": {},
                "help": "Turns on the deck's identify indicator so it can be found in the rack.",
            },
            "identify_off": {
                "label": "Stop Identify",
                "params": {},
                "help": "Turns the identify indicator off.",
            },
            "reboot": {
                "label": "Restart HyperDeck",
                "params": {},
                "confirm": "Restarts the deck. Recording or playback stops, and the deck is offline until it has started.",
                "help": "Restarts the deck. It is offline until it has started.",
            },
        },
    }

    # Ping on the BaseDriver cadence (every 30 s). The deadline covers waiting
    # behind the request in progress (a clip list 10 s, a late reply 3 s) and
    # the ping itself (5 s).
    HEALTH_TIMEOUT_S = 20.0
    HEALTH_FAULT_MESSAGE = "The HyperDeck stopped answering ping."

    # ── Lifecycle ──

    def __init__(self, device_id: str, config: dict[str, Any], state, events):
        self._rx = b""
        self._block: tuple[int, str, list[str]] | None = None
        self._banner: asyncio.Future | None = None
        self._pending: _Pending | None = None
        self._lock = asyncio.Lock()
        self._session_up = False
        self._notified = False
        self._cycle = 0
        self._recording = False
        self._clips_stale = True
        self._read_problems: dict[str, str] = {}
        self._standing: dict[str, str] = {}
        super().__init__(device_id, config, state, events)

    def _transport_kwargs(self, transport_type: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        # Raw bytes: the driver splits lines itself and keeps the blank line
        # that ends a block, which a delimiter parser would drop.
        kwargs["delimiter"] = None
        kwargs["frame_parser"] = None
        return kwargs

    async def _pre_connect(self) -> None:
        # Before the socket opens: the deck greets at once, and the greeting
        # must find a waiter.
        self._rx = b""
        self._block = None
        self._pending = None
        self._session_up = False
        self._notified = False
        self._banner = asyncio.get_running_loop().create_future()

    async def _post_connect(self) -> None:
        banner = self._banner
        assert banner is not None
        try:
            await asyncio.wait_for(asyncio.shield(banner), BANNER_TIMEOUT_S)
        except asyncio.TimeoutError:
            port = self.config.get("port", DEFAULT_PORT)
            raise ConnectionError(
                f"The device on port {port} did not send a HyperDeck greeting "
                f"within {BANNER_TIMEOUT_S:.0f} s. Check this is a HyperDeck's "
                "address and its Ethernet protocol access is enabled."
            ) from None

        reply = await self._request("device info")
        if not reply.ok:
            raise ConnectionError(f"The HyperDeck refused device info ({reply.words()}).")

        self._standing.pop("override", None)
        if self.config.get("remote_override"):
            reply = await self._request("remote: override: true")
            if not reply.ok:
                self._standing["override"] = f"The HyperDeck refused Remote Override ({reply.words()})."
                log.warning(f"[{self.device_id}] {self._standing['override']}")

        reply = await self._request("notify: transport: true slot: true remote: true configuration: true")
        self._notified = reply.ok
        if not reply.ok:
            log.info(f"[{self.device_id}] Notifications refused ({reply.words()}); status is polled only")
        self._session_up = True

    async def _initial_sync(self) -> None:
        self._cycle = 0
        self._clips_stale = True
        await self.poll()

    async def _close_session(self) -> None:
        self._session_up = False
        pending, self._pending = self._pending, None
        if pending is not None and not pending.future.done():
            pending.future.set_exception(ConnectionError("connection closed"))
            pending.future.exception()
        banner = self._banner
        if banner is not None:
            if not banner.done():
                banner.cancel()
            elif not banner.cancelled():
                banner.exception()  # a refused connection's, seen
        self._rx = b""
        self._block = None

    # ── Requests (one at a time) ──

    async def _request(
        self, command: str, timeout: float = REQUEST_TIMEOUT_S, lines: list[str] | None = None,
    ) -> _HyperDeckReply:
        """Send one command and wait for its answer.

        A 1xx answer is returned, not raised: the caller decides whether it is
        a refusal. A ``122 authentication required`` signs in and sends the
        command once more.
        """
        async with self._lock:
            await self._settle_late_reply()
            reply = await self._exchange(command, lines, timeout)
            if reply.code == AUTH_REQUIRED:
                await self._authenticate(timeout)
                reply = await self._exchange(command, lines, timeout)
                if reply.code == AUTH_REQUIRED:
                    raise self._auth_fault("The HyperDeck asked for a login again straight after accepting one.")
            return reply

    async def _exchange(self, command: str, lines: list[str] | None, timeout: float) -> _HyperDeckReply:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        pending = _Pending(command, future)
        self._pending = pending
        if lines is None:
            text = command + "\n"
        else:
            # Multiline form: the command, a line per parameter, a blank line.
            text = command + "\n" + "".join(line + "\n" for line in lines) + "\n"
        await self.transport.send(text.encode("utf-8"))
        try:
            reply = await asyncio.wait_for(asyncio.shield(future), timeout)
        except asyncio.TimeoutError:
            # Left pending, so a late answer lands on it and not on the next
            # request.
            raise ConnectionError(
                f"[{self.device_id}] The HyperDeck did not answer '{command}' within {timeout:.0f} s"
            ) from None
        if self._pending is pending:
            self._pending = None
        return reply

    async def _settle_late_reply(self) -> None:
        stale = self._pending
        if stale is None:
            return
        if not stale.future.done():
            try:
                await asyncio.wait_for(asyncio.shield(stale.future), LATE_REPLY_S)
            except asyncio.TimeoutError:
                self._force_disconnect(
                    "no_response", f"The HyperDeck stopped answering ('{stale.command}' got no reply).",
                )
                raise ConnectionError(f"[{self.device_id}] The HyperDeck stopped answering") from None
            except Exception:
                pass
        if self._pending is stale:
            self._pending = None

    async def _authenticate(self, timeout: float) -> None:
        username = str(self.config.get("username") or "")
        password = str(self.config.get("password") or "")
        if not username:
            raise self._auth_fault(
                "The HyperDeck asks for a username and password (its Ethernet "
                "protocol access is Enabled with security), and none is entered."
            )
        reply = await self._exchange(
            "authenticate:", [f"username: {username}", f"password: {password}"], timeout,
        )
        if not reply.ok:
            raise self._auth_fault(f"The HyperDeck refused the username and password ({reply.words()}).")

    def _auth_fault(self, message: str) -> ConnectionFaultError:
        # Mid-session the connection goes down typed at once; at connect the
        # raise is the connect's own failure.
        if self._session_up:
            self._force_disconnect("auth_failed", message)
        return ConnectionFaultError(message, code="auth_failed")

    # ── Receiving ──

    def on_data_received(self, data: bytes) -> None:
        # Synchronous: the transport runs a plain handler inline, in the order
        # the bytes arrive, so a reply is never applied after a later line, and
        # a "120 connection failed" is read before the deck closes the socket.
        self._rx += data
        while True:
            end = self._rx.find(b"\n")
            if end < 0:
                break
            raw, self._rx = self._rx[:end], self._rx[end + 1:]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            self._line(raw.decode("utf-8", errors="replace"))
        if len(self._rx) > RX_LIMIT:
            log.warning(f"[{self.device_id}] Dropped {len(self._rx)} bytes with no line ending")
            self._rx = b""

    def _line(self, line: str) -> None:
        block = self._block
        if block is not None:
            if line.strip():
                block[2].append(line)
                return
            self._block = None
            self._message(block[0], block[1], block[2])
            return
        if not line.strip():
            return
        match = _CODE_LINE.match(line)
        if not match:
            log.debug(f"[{self.device_id}] Unexpected line: {line!r}")
            return
        code = int(match.group(1))
        text = (match.group(2) or "").strip()
        if text.endswith(":"):
            # The block's lines follow, up to a blank line. Inside a block
            # nothing is read as a code (a clip line starts with a number).
            self._block = (code, text[:-1].strip(), [])
            return
        self._message(code, text, None)

    def _message(self, code: int, text: str, lines: list[str] | None) -> None:
        params = _params(lines)
        if code == CONNECTION_FAILED and not self._banner_received():
            self._refused_connection()
            return
        if 500 <= code < 600:
            self._apply_async(code, params)
            return
        reply = _HyperDeckReply(code, text, params, lines)
        if reply.ok and lines is not None:
            self._apply_reply(code, params, lines)
        pending = self._pending
        if pending is not None and not pending.future.done():
            pending.future.set_result(reply)
        else:
            log.debug(f"[{self.device_id}] Answer with no request: {reply.words()}")

    def _banner_received(self) -> bool:
        banner = self._banner
        return banner is not None and banner.done() and not banner.cancelled() and banner.exception() is None

    def _refused_connection(self) -> None:
        fault = ConnectionFaultError(REFUSED_CONNECTION, code="connection_refused")
        banner = self._banner
        if banner is not None and not banner.done():
            banner.set_exception(fault)
        # The deck closes the connection next; that drop ends the connect
        # with this reason.
        self._stash_fault("connection_refused", REFUSED_CONNECTION)

    def _apply_async(self, code: int, params: dict[str, str]) -> None:
        if code == 500:
            if "protocol version" in params:
                self.set_state("protocol_version", params["protocol version"])
            if "model" in params:
                self.set_state("model", params["model"])
            banner = self._banner
            if banner is not None and not banner.done():
                banner.set_result(True)
        elif code == 502:
            self._apply_slot(params)
        elif code == 508:
            self._apply_transport(params)
        elif code == 510:
            self._apply_remote(params)
        elif code == 511:
            self._apply_configuration(params)

    def _apply_reply(self, code: int, params: dict[str, str], lines: list[str]) -> None:
        if code == 204:
            self._apply_device_info(params)
        elif code == 202:
            self._apply_slot(params)
        elif code == 208:
            self._apply_transport(params)
        elif code == 210:
            self._apply_remote(params)
        elif code == 211:
            self._apply_configuration(params)
        elif code == 214:
            count = _int(params.get("clip count"))
            if count is not None:
                self.set_state("clip_count", count)
        elif code == 205:
            self._apply_clips(lines)

    # ── What each block reports ──

    def _apply_device_info(self, p: dict[str, str]) -> None:
        for wire, key in (
            ("model", "model"), ("protocol version", "protocol_version"),
            ("unique id", "unique_id"), ("software version", "software_version"),
            ("name", "device_name"),
        ):
            if wire in p:
                self.set_state(key, p[wire])
        count = _int(p.get("slot count"))
        if count is not None:
            count = max(0, min(count, SLOT_MAX))
            self.set_state("slot_count", count)
            want = set(range(1, count + 1))
            have = set(self.list_children("slot"))
            for sid in sorted(want - have):
                self.register_child("slot", sid)
            for sid in have - want:
                self.deregister_child("slot", sid)

    def _apply_slot(self, p: dict[str, str]) -> None:
        sid = _int(p.get("slot id"))
        if sid is None or not 1 <= sid <= SLOT_MAX:
            return
        if not self.is_child_registered("slot", sid):
            self.register_child("slot", sid)
        values: dict[str, Any] = {}
        if "status" in p:
            values["status"] = p["status"].strip().lower()
        for wire, key in (
            ("slot name", "slot_name"), ("device name", "device_name"),
            ("volume name", "volume_name"), ("video format", "video_format"),
        ):
            if wire in p:
                values[key] = p[wire]
        for wire, key in (
            ("recording time", "recording_time"), ("remaining size", "remaining_size"),
            ("total size", "total_size"),
        ):
            if wire in p:
                values[key] = _int(p[wire])
        if "blocked" in p:
            values["blocked"] = _bool(p["blocked"])
        if values:
            self.set_children_state_batch([("slot", sid, values)])

    def _apply_transport(self, p: dict[str, str]) -> None:
        if "status" in p:
            status = p["status"].strip().lower()
            recording = status == "record"
            if self._recording and not recording:
                # The clip just recorded joins the timeline.
                self._clips_stale = True
            self._recording = recording
            self.set_state("transport_status", status)
            self.set_state("recording", recording)
        if "slot id" in p:
            slot = _int(p["slot id"])
            if slot != self.get_state("active_slot"):
                self._clips_stale = True
            self.set_state("active_slot", slot)
        if "clip id" in p:
            self.set_state("clip_id", _int(p["clip id"]))
        if "speed" in p:
            self.set_state("speed", _int(p["speed"]))
        for wire, key in (("loop", "loop"), ("single clip", "single_clip"), ("reference locked", "reference_locked")):
            if wire in p:
                self.set_state(key, _bool(p[wire]))
        for wire, key in (
            ("timecode", "timecode"), ("display timecode", "display_timecode"),
            ("video format", "video_format"), ("input video format", "input_video_format"),
            ("dynamic range", "dynamic_range"),
        ):
            if wire in p:
                self.set_state(key, p[wire])

    def _apply_remote(self, p: dict[str, str]) -> None:
        if "enabled" in p:
            self.set_state("remote_enabled", _bool(p["enabled"]))
        if "override" in p:
            self.set_state("remote_override", _bool(p["override"]))

    def _apply_configuration(self, p: dict[str, str]) -> None:
        variables = self.DRIVER_INFO["state_variables"]
        for wire, key in CONFIG_STATE.items():
            if wire not in p:
                continue
            raw = p[wire]
            spec = variables[key]
            if spec["type"] == "boolean":
                self.set_state(key, _bool(raw))
            elif spec["type"] == "enum":
                # The Ethernet document writes component / composite, the
                # REST document Component / Composite: match either.
                self.set_state(key, _canonical(raw, spec["values"]))
            else:
                self.set_state(key, raw)

    def _apply_clips(self, lines: list[str]) -> None:
        options: list[dict[str, Any]] = []
        count: int | None = None
        for line in lines:
            if line.lower().startswith("clip count:"):
                count = _int(line.split(":", 1)[1])
                continue
            match = _CLIP_V2.match(line)
            if match:
                clip, name = int(match.group(1)), match.group(6).strip()
            else:
                match = _CLIP_V1.match(line)
                if not match:
                    continue
                clip, name = int(match.group(1)), match.group(2).strip()
            if len(options) < CLIP_OPTIONS_MAX:
                options.append({"value": clip, "label": f"{clip}: {name}"})
            elif count is not None:
                break
        if count is None:
            count = len(options)
        self.set_state("clip_count", count)
        self.set_state("clip_options", json.dumps(options))
        self._clips_stale = False

    # ── Polling ──

    async def poll(self) -> None:
        self._cycle += 1
        every = FULL_READ_EVERY if self._notified else FULL_READ_EVERY_UNNOTIFIED
        full = self._cycle == 1 or self._cycle % every == 0
        await self._read("transport info")
        if full:
            await self._read("device info")
            await self._read("remote")
            await self._read("configuration")
            for sid in sorted(self.list_children("slot")):
                await self._read(f"slot info: slot id: {sid}")
        elif self._recording:
            active = self.get_state("active_slot")
            if isinstance(active, int) and self.is_child_registered("slot", active):
                await self._read(f"slot info: slot id: {active}")
        if full or self._clips_stale:
            await self._read_clips()
        self._write_last_error()

    async def _read(self, command: str, timeout: float = REQUEST_TIMEOUT_S) -> _HyperDeckReply:
        """A read whose refusal is an answer: noted, never raised. Silence raises."""
        reply = await self._request(command, timeout)
        if reply.ok or reply.code in EMPTY_READ_CODES:
            self._read_problems.pop(command, None)
        else:
            self._read_problems[command] = f"'{command}' refused ({reply.words()})"
        return reply

    async def _read_clips(self) -> None:
        reply = await self._read("clips count")
        count = _int(reply.params.get("clip count")) if reply.ok else None
        if reply.code in EMPTY_READ_CODES or count == 0:
            self.set_state("clip_count", 0)
            self.set_state("clip_options", "[]")
            self._clips_stale = False
            return
        if not reply.ok:
            return
        # Version 2 puts the name last, so a name with spaces reads whole.
        reply = await self._request("clips get: version: 2", CLIPS_TIMEOUT_S)
        if not reply.ok and reply.code not in EMPTY_READ_CODES:
            # A deck without the version parameter: the version 1 form.
            reply = await self._read("clips get", timeout=CLIPS_TIMEOUT_S)
        if reply.code in EMPTY_READ_CODES:
            self.set_state("clip_count", 0)
            self.set_state("clip_options", "[]")
        # Read again at the next full read or timeline change, not every poll.
        self._clips_stale = False

    def _write_last_error(self) -> None:
        # Standing conditions are written again on every poll while they hold.
        problems = list(self._standing.values()) + list(self._read_problems.values())
        if self.get_state("remote_enabled") is False and not self.get_state("remote_override"):
            problems.insert(0, "Remote is off on the HyperDeck, so it refuses changes from the network.")
        if problems:
            self.set_state("last_error", " ".join(problems))

    async def refresh_children(self) -> dict[str, Any]:
        await self._read("device info")
        for sid in sorted(self.list_children("slot")):
            await self._read(f"slot info: slot id: {sid}")
        return {"slots": len(self.list_children("slot"))}

    async def _liveness_probe(self) -> None:
        # Any answer counts, a refusal included: ping only asks whether the
        # deck is answering. The platform's deadline governs the wait (on a
        # reconnect held offline it is HEALTH_INTERVAL_S, so a deck still
        # starting comes online at its late answer); the request's own timeout
        # is longer than either deadline.
        await self._request("ping", timeout=float(self.HEALTH_INTERVAL_S) + float(self.HEALTH_TIMEOUT_S))

    # ── Commands ──

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if command == "play":
            parts = []
            speed = _optional_int(params.get("speed"))
            if speed is not None:
                parts.append(f"speed: {speed}")
            for name, wire in (("loop", "loop"), ("single_clip", "single clip")):
                flag = _optional_bool(params.get(name))
                if flag is not None:
                    parts.append(f"{wire}: {_word(flag)}")
            return await self._transport_command("play" + (": " + " ".join(parts) if parts else ""), "Play")
        if command == "play_clip":
            clip = _required_int(params.get("clip_id"), "Clip")
            parts = [f"clip id: {clip}"]
            for name, wire in (("single_clip", "single clip"), ("loop", "loop")):
                flag = _optional_bool(params.get(name))
                if flag is not None:
                    parts.append(f"{wire}: {_word(flag)}")
            return await self._transport_command("play: " + " ".join(parts), f"Play Clip {clip}")
        if command == "stop":
            return await self._transport_command("stop", "Stop")
        if command == "record":
            return await self._transport_command("record", "Record")
        if command == "record_named":
            name = str(params.get("name") or "").strip()
            if not name or "\r" in name or "\n" in name:
                raise ValueError("The clip name is one line of text")
            # Multiline form, so a name holding a colon is not read as a parameter.
            return await self._transport_command("record:", "Record Named Clip", lines=[f"name: {name}"])
        if command == "record_spill":
            slot = _optional_int(params.get("slot"))
            line = "record spill" if slot is None else f"record: spill: slot id: {slot}"
            return await self._transport_command(line, "Spill Recording")
        if command == "preview_on":
            return await self._transport_command("preview: enable: true", "Show Input")
        if command == "preview_off":
            return await self._transport_command("preview: enable: false", "Show Playback")
        if command == "next_clip":
            return await self._transport_command("goto: clip id: +1", "Next Clip")
        if command == "previous_clip":
            return await self._transport_command("goto: clip id: -1", "Previous Clip")
        if command == "goto_clip":
            clip = _required_int(params.get("clip_id"), "Clip")
            return await self._transport_command(f"goto: clip id: {clip}", f"Go To Clip {clip}")
        if command in ("goto_clip_start", "goto_clip_end"):
            end = command.rsplit("_", 1)[1]
            return await self._transport_command(f"goto: clip: {end}", f"Go To Clip {end.title()}")
        if command in ("goto_timeline_start", "goto_timeline_end"):
            end = command.rsplit("_", 1)[1]
            return await self._transport_command(f"goto: timeline: {end}", f"Go To Timeline {end.title()}")
        if command == "goto_timecode":
            timecode = str(params.get("timecode") or "").strip()
            if not re.fullmatch(_TIMECODE, timecode):
                raise ValueError("The timecode is HH:MM:SS:FF, with an optional leading + or -")
            return await self._transport_command(f"goto: timecode: {timecode}", "Go To Timecode")
        if command == "shuttle":
            speed = _required_int(params.get("speed"), "Speed")
            return await self._transport_command(f"shuttle: speed: {speed}", "Shuttle")
        if command == "select_slot":
            slot = _required_int(params.get("slot"), "Slot")
            await self._command(f"slot select: slot id: {slot}", f"Select Slot {slot}")
            self._clips_stale = True
            if not self._notified:
                await self._read("transport info")
            return True
        if command == "identify_on":
            return await self._command("identify: enable: true", "Identify")
        if command == "identify_off":
            return await self._command("identify: enable: false", "Stop Identify")
        if command == "reboot":
            return await self._command("reboot", "Restart HyperDeck")
        raise ValueError(f"Unknown command '{command}'")

    async def _command(self, line: str, label: str, lines: list[str] | None = None) -> bool:
        reply = await self._request(line, lines=lines)
        if reply.ok:
            # 213 deck rebooting included: a success, then the deck closes
            # the connection and the device reconnects.
            return True
        raise CommandRefusedError(_refusal(label, reply))

    async def _transport_command(self, line: str, label: str, lines: list[str] | None = None) -> bool:
        await self._command(line, label, lines)
        if not self._notified:
            # Without the deck's notices the new state is read at once.
            await self._read("transport info")
        return True

    # ── Device settings ──

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if key == "remote_enabled":
            await self._command(f"remote: enable: {_word(_as_bool(value))}", "Remote Control")
            await self._read("remote")
            return True
        if key not in CONFIG_SETTINGS:
            raise ValueError(f"Unknown device setting '{key}'")
        wire, label = CONFIG_SETTINGS[key]
        spec = self.DRIVER_INFO["state_variables"][key]
        if spec["type"] == "boolean":
            text = _word(_as_bool(value))
        else:
            text = str(value if value is not None else "").strip()
            if spec["type"] == "enum":
                text = _canonical(text, spec["values"])
            if "\r" in text or "\n" in text:
                raise ValueError(f"{label} is one line of text")
        if key == "record_prefix":
            # Multiline form, so a prefix holding a colon or a space is kept whole.
            reply = await self._request("configuration:", lines=[f"{wire}: {text}"])
        else:
            reply = await self._request(f"configuration: {wire}: {text}")
        if reply.code == DECK_REBOOTING:
            # A file format change can restart the deck; it reconnects.
            return True
        if not reply.ok:
            raise CommandRefusedError(_refusal(f"setting {label}", reply))
        await self._read("configuration")
        return True

    # ── Setup action ──

    async def run_setup_action(self, action_id: str, params: dict[str, Any], progress: Any) -> dict[str, Any]:
        if action_id != "test_connection":
            raise ValueError(f"Unknown setup action: {action_id}")
        host = str(self.config.get("host") or "").strip()
        port = int(self.config.get("port") or DEFAULT_PORT)
        if not host:
            raise ValueError("No IP address is entered")
        await progress(f"Connecting to {host}:{port}", 20)
        context = None
        if self.config.get("ssl"):
            context = ssl_module.create_default_context()
            if not self.config.get("verify_ssl"):
                context.check_hostname = False
                context.verify_mode = ssl_module.CERT_NONE
        local = getattr(self, "local_address", None)  # OpenAVC 0.37.0 and later
        local_ip = local(host) if callable(local) else None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host, port, ssl=context, local_addr=(local_ip, 0) if local_ip else None,
                ),
                timeout=5.0,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise ConnectionError(
                f"Could not reach {host}:{port} ({exc or type(exc).__name__}). Check the IP "
                "address and that the deck is on the network."
            ) from exc
        await progress("Reading the greeting", 60)
        text = ""
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 4.0
            while loop.time() < deadline:
                try:
                    chunk = await asyncio.wait_for(reader.read(4096), timeout=deadline - loop.time())
                except asyncio.TimeoutError:
                    break
                if not chunk:
                    break
                text += chunk.decode("utf-8", errors="replace")
                if re.search(r"^500 connection info:.*?\r?\n\r?\n", text, re.S | re.M) or "120 connection failed" in text:
                    break
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.TimeoutError, ssl_module.SSLError):
                pass
        if "120 connection failed" in text:
            raise ConnectionError(REFUSED_CONNECTION)
        if "500 connection info:" not in text:
            raise ConnectionError(
                f"{host}:{port} answered but sent no HyperDeck greeting. Check this "
                "is a HyperDeck and its Ethernet protocol access is enabled."
            )
        model = _grep(text, r"model:\s*([^\r\n]+)") or "HyperDeck"
        version = _grep(text, r"protocol version:\s*([^\r\n]+)")
        await progress("Connected", 100)
        suffix = f", protocol {version}" if version else ""
        return {"success": True, "message": f"Reached {model}{suffix} on {host}:{port}."}


# ── Helpers ──


def _params(lines: list[str] | None) -> dict[str, str]:
    """A block's "name: value" lines; names lowercased, values trimmed.

    A name never holds a colon, so the first colon divides; a timecode value
    keeps its own.
    """
    out: dict[str, str] = {}
    for line in lines or ():
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        out[name.strip().lower()] = value.strip()
    return out


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool | None:
    text = str(value).strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _optional_bool(value: Any) -> bool | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _as_bool(value)


def _optional_int(value: Any) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    number = _int(value)
    if number is None:
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            raise ValueError(f"{value!r} is not a whole number") from None
    return number


def _required_int(value: Any, label: str) -> int:
    number = _optional_int(value)
    if number is None:
        raise ValueError(f"{label} is required")
    return number


def _word(flag: bool) -> str:
    return "true" if flag else "false"


def _canonical(value: str, choices: list[str]) -> str:
    for choice in choices:
        if choice.lower() == value.strip().lower():
            return choice
    return value.strip()


def _refusal(label: str, reply: _HyperDeckReply) -> str:
    if reply.code == REMOTE_DISABLED:
        return (
            f"The HyperDeck refused {label}: its Remote setting is off "
            f"({reply.words()}). Turn Remote on at the deck, or turn on Remote "
            "Override for this device."
        )
    return f"The HyperDeck refused {label} ({reply.words()})."


def _grep(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1).strip() if match else None
