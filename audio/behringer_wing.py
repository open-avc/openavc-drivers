"""
OpenAVC Behringer WING driver: WING, WING-BK, WING Compact and WING Rack
digital mixing consoles over OSC.

Protocol: the WING's OSC server on UDP port 2223, as "WING Remote Protocols"
V3.1.0 describes it (firmware 3.1.0 and later), the document Behringer links
from the WING product pages. Its author, Patrick-Gilles Maillot, publishes and
maintains it with Behringer's authorization.

    https://cdn-media.empowertribe.com/2106004e217b8eb2639d93672823eef5/WING_Remote-Protocols-3.1-03.pdf

Reading and writing. Every parameter is a leaf of the console's tree
(/ch/1/fdr, /dca/3/mute). Sending the address with no argument reads it: a
float answers ``,sff`` (text, raw 0..1, value), an integer ``,sfi``, a string
or enum ``,s`` (OSC Remote Protocol, "Reading (Get)"). Sending it with an
argument writes it, and "WING does not echo data sent over UDP by the client
application" ("Writing (Set)"), so every write here is followed by a read of
the same address: the reply is what moves the state, never the value sent.
An integer 0..1 parameter sent -1 toggles ("Special case: Toggle").

Push. ``/*s`` subscribes the sender to change events in the same triplet
form a read answers ("Subscribing to OSC Data"; the document writes it
``/*s~``, where ``~`` stands for a padding NUL, not a character to send). There is one OSC
subscription at a time, held by the last client to ask, and it ends after
10 seconds unless asked again, so the driver asks every 8 seconds on its own
timer. Another OSC client that subscribes takes the feed until the next
renewal and then loses it, so the two trade it every few seconds; the full
read at Full Re-read Interval keeps the state right through that. Whether
the console sends a subscriber the changes it made itself is not stated, and
the driver does not rely on it: its own writes are read back.

Liveness. ``/?`` always answers with the console's identity
(``WING,<ip>,<name>,<model>,<serial>,<firmware>``, "WING OSC Messages"). It
is asked before the device is reported connected (the platform's generic
OSC check sends ``/info``, which the document does not list, so it is turned
off) and every 15 seconds after.

Scenes. A scene is recalled by pointing ``/$ctl/lib/$actionidx`` at it and
sending ``/$ctl/lib/$action GO`` (or ``GOTAG`` for a tag number); Next and
Previous send ``GONEXT`` / ``GOPREV`` with ``$actionidx`` at 0, as the
document's footnote 79 requires. A show must be open on the console.

Remote Lock. With OSC set read-only on the console (Setup > Remote > Remote
Lock, ``/$ctl/OSC/ronly``) every write is ignored without a word, so the
driver reads the lock and refuses a write while it is on.

Why Python, not YAML: a YAML OSC command sends one message, so it could
neither read its own write back (the console never echoes it; a panel would
show a press only at the next poll) nor recall a scene by number (two
messages). And the 10-second subscription would have had to ride a full read
of every strip on a 9-second poll; here it renews on its own and the full
read is a backstop.

Not modeled: EQ, dynamics, effects, routing, sources and headamps, meters.
Any parameter in the tree can still be written with Set Parameter.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.transport.osc_codec import osc_decode_bundle, osc_encode_message
from openavc.utils.logger import get_logger

try:  # OpenAVC 0.37.1+
    from openavc.drivers.base import CommandRefusedError
except ImportError:  # pragma: no cover - older platform
    CommandRefusedError = RuntimeError

log = get_logger(__name__)

OSC_PORT = 2223
# The subscription ends 10 s after it was last asked for.
RENEW_INTERVAL_S = 8.0
# How long the connect waits for the console to answer /?.
IDENTIFY_TIMEOUT_S = 3.0
# Spacing between the reads of a full read (about 500 of them), so the
# console is never handed them all in one burst.
READ_GAP_S = 0.002

SUBSCRIBE = "/*s"
IDENTIFY = "/?"

FADER_MIN_DB = -144.0
FADER_MAX_DB = 10.0

# child type -> (OSC root, roster size, the leaves read for it)
# A leaf maps the console's parameter name to the child state variable.
_STRIP_LEAVES = {"name": "name", "mute": "mute", "fdr": "fader_db", "pan": "pan",
                 "$solo": "solo"}
_DCA_LEAVES = {"name": "name", "mute": "mute", "fdr": "fader_db", "$solo": "solo"}
_MGRP_LEAVES = {"name": "name", "mute": "mute"}
_CHILDREN: dict[str, tuple[str, int, dict[str, str]]] = {
    "channel": ("ch", 40, _STRIP_LEAVES),
    "aux": ("aux", 8, _STRIP_LEAVES),
    "bus": ("bus", 16, _STRIP_LEAVES),
    "main": ("main", 4, _STRIP_LEAVES),
    "matrix": ("mtx", 8, _STRIP_LEAVES),
    "dca": ("dca", 16, _DCA_LEAVES),
    "mute_group": ("mgrp", 8, _MGRP_LEAVES),
}
_ROOT_TO_CHILD = {root: ctype for ctype, (root, _n, _leaves) in _CHILDREN.items()}
_LEAF_RE = re.compile(r"^/(ch|aux|bus|main|mtx|dca|mgrp)/(\d+)/(\$?[a-z]+)$")

_STATUS_WORDS = {"-": "none", "OK": "ok", "ERR": "error", "UPD": "updating"}
_PLAYER_WORDS = {"STOP": "stop", "PLAY": "play", "PAUSE": "pause", "ERROR": "error"}
_RECORDER_WORDS = {"STOP": "stop", "REC": "record", "PAUSE": "pause", "ERROR": "error"}

# Console-wide reads: address -> (state variable, how the value is read).
_DEVICE_READS: dict[str, tuple[str, Any]] = {
    "/$ctl/OSC/ronly": ("osc_read_only", bool),
    "/$ctl/lib/$actidx": ("current_scene_index", int),
    "/$ctl/lib/$active": ("current_scene", str),
    "/$ctl/lib/$actshow": ("current_show", str),
    "/$ctl/lib/$activeid": ("current_scene_tag", int),
    "/$stat/solo": ("solo_active", bool),
    "/$stat/lock": ("clock_locked", bool),
    "/$stat/A/stat": ("aes50_a_status", _STATUS_WORDS),
    "/$stat/B/stat": ("aes50_b_status", _STATUS_WORDS),
    "/$stat/C/stat": ("aes50_c_status", _STATUS_WORDS),
    "/play/$actstate": ("player_state", _PLAYER_WORDS),
    "/play/$song": ("player_song", str),
    "/play/$artist": ("player_artist", str),
    "/play/repeat": ("player_repeat", bool),
    "/rec/$actstate": ("recorder_state", _RECORDER_WORDS),
    "/rec/$actfile": ("recorder_file", str),
    "/rec/$time": ("recorder_time", float),
}
_SCENE_READS = ("/$ctl/lib/$actidx", "/$ctl/lib/$active", "/$ctl/lib/$activeid")
_PLAYER_READS = ("/play/$actstate", "/play/$song", "/play/$artist")
_RECORDER_READS = ("/rec/$actstate", "/rec/$actfile")

# The console types /? and /$syscfg/$cnsmdl report.
_CONSOLE_TYPES = {
    "ngc-full": "WING",
    "wing-bk": "WING",
    "wing-rack": "WING Rack",
    "wing-compact": "WING Compact",
}

_PLAYER_ACTIONS = {
    "player_play": "PLAY",
    "player_pause": "PAUSE",
    "player_stop": "STOP",
    "player_next": "NEXT",
    "player_previous": "PREV",
}
_RECORDER_ACTIONS = {
    "recorder_record": "REC",
    "recorder_pause": "PAUSE",
    "recorder_stop": "STOP",
    "recorder_new_file": "NEWFILE",
}

# Strip commands: command name pattern -> action. A DCA has no pan.
_STRIP_ACTIONS = {
    "set_{t}_fader": "set_fader",
    "nudge_{t}_fader": "nudge_fader",
    "mute_{t}": "mute",
    "unmute_{t}": "unmute",
    "toggle_{t}_mute": "toggle_mute",
    "set_{t}_pan": "set_pan",
    "set_{t}_name": "set_name",
    "set_{t}_solo": "set_solo",
}
_STRIP_COMMANDS: dict[str, tuple[str, str]] = {
    pattern.format(t=ctype): (ctype, action)
    for ctype in ("channel", "aux", "bus", "main", "matrix", "dca")
    for pattern, action in _STRIP_ACTIONS.items()
    if not (ctype == "dca" and action == "set_pan")
}
_STRIP_COMMANDS.update({
    "mute_group_on": ("mute_group", "mute"),
    "mute_group_off": ("mute_group", "unmute"),
    "toggle_mute_group": ("mute_group", "toggle_mute"),
    "set_mute_group_name": ("mute_group", "set_name"),
})

_ARG_TAGS = {"float": "f", "integer": "i", "string": "s"}


def _reply_value(args: list[tuple[str, Any]]) -> Any:
    """The value a read reply or change event carries.

    A float or integer comes as (text, raw 0..1, value); a string or enum as
    the string alone.
    """
    if not args:
        return None
    if len(args) >= 3 and args[0][0] == "s" and args[1][0] == "f":
        return args[2][1]
    return args[0][1]


def _as_bool(value: Any) -> bool | None:
    try:
        return bool(int(float(value)))
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class BehringerWingDriver(BaseDriver):
    """Behringer WING family over OSC: strips as children, scenes, USB player
    and recorder, push through the console's change subscription."""

    DRIVER_INFO = {
        "id": "behringer_wing",
        "name": "Behringer WING",
        "manufacturer": "Behringer",
        "category": "audio",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": (
            "Controls Behringer WING, WING Compact and WING Rack digital mixing "
            "consoles over OSC: every channel, aux, bus, main, matrix, DCA and "
            "mute group as a child entity with name, mute, fader, pan and solo, "
            "scene recall, the USB player and recorder, and the console's "
            "status. Changes made anywhere are pushed to OpenAVC as they happen."
        ),
        "source_url": (
            "https://cdn-media.empowertribe.com/2106004e217b8eb2639d93672823eef5/"
            "WING_Remote-Protocols-3.1-03.pdf"
        ),
        "tags": ["mixer", "console", "osc", "wing"],
        "verified": False,
        "simulated": True,
        "min_platform_version": "0.25.0",
        "ports": [2223],
        "transport": "osc",
        "discovery": {
            # The WING answers "WING?" on UDP 2222 with
            # "WING,<ip>,<name>,<model>,<serial>,<firmware>" ("Remote
            # communications with WING"). 2222 is its native port, not the
            # OSC port the driver holds, so a scan can ask while it is
            # connected.
            "udp_probe": {
                "port": 2222,
                "send_ascii": "WING?",
                "expect_regex": r"^WING,",
                "extract_manufacturer": "Behringer",
                "extract": {
                    "model": {"regex": r"^WING,[^,]*,[^,]*,([^,]+),"},
                    "name": {"regex": r"^WING,[^,]*,([^,]*),"},
                },
                "timeout_ms": 1500,
            },
            "oui": ["00:15:64"],   # BEHRINGER Spezielle Studiotechnik GmbH (MA-L)
            "manufacturer_alias": ["behringer", "music tribe"],
        },
        "compatible_models": [
            {
                "manufacturer": "Behringer",
                "models": ["WING", "WING-BK", "WING Compact", "WING Rack"],
                "confidence": "untested",
                "notes": (
                    "All four run the same OSC remote protocol on firmware 3.1 "
                    "and later, with the same 40 channels, 8 auxes, 16 buses, 4 "
                    "mains, 8 matrices, 16 DCAs and 8 mute groups. Not the X32 "
                    "or M32, which have their own driver."
                ),
            }
        ],
        "help": {
            "overview": (
                "Controls a Behringer WING, WING Compact or WING Rack over the "
                "network with OSC. Every channel, aux, bus, main, matrix, DCA "
                "and mute group is a child entity with its name, mute, fader "
                "and, for strips, pan and solo, so panel controls bind to "
                "device.<id>.channel.1.mute and the like. Scenes of the open "
                "show can be recalled by number or tag, or stepped with Next "
                "and Previous; the USB player and recorder have their own "
                "controls; and the console's name, model, firmware, clock lock "
                "and AES50 ports are shown on the device page.\n\n"
                "Changes made on the console, in WING Co-Pilot or anywhere else "
                "appear as they happen. The console sends its change notices to "
                "one OSC client at a time, so with another OSC app subscribed "
                "the two take turns; every value is also read again at Full "
                "Re-read Interval."
            ),
            "setup": (
                "1. On the console, open Setup > Network and note the IP "
                "address (a fixed address is best).\n"
                "2. Enter it as the IP Address. The OSC port is always 2223.\n"
                "3. Check that Setup > Remote > Remote Lock is not set to make "
                "OSC read-only: while it is, the console ignores every change "
                "and OpenAVC refuses to send one.\n"
                "Scene commands need a show open on the console."
            ),
        },
        "default_config": {
            "host": "",
            "port": 2223,
            "poll_interval": 30,
            "verify_timeout": 0,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
                "description": "The console's IP address (Setup > Network on the console).",
            },
            "port": {
                "type": "integer",
                "default": 2223,
                "min": 1,
                "max": 65535,
                "label": "OSC Port",
                "description": "The WING's OSC port is 2223.",
            },
            "poll_interval": {
                "type": "integer",
                "default": 30,
                "min": 0,
                "max": 3600,
                "label": "Full Re-read Interval (s)",
                "description": (
                    "How often every value is read again, besides the console's "
                    "change notices. 0 reads only at connect."
                ),
            },
        },
        "state_variables": {
            "console_name": {
                "type": "string", "label": "Console Name",
                "help": "The name set on the console.",
            },
            "model": {
                "type": "string", "label": "Model",
                "help": "The model the console reports, for example ngc-full for a WING.",
            },
            "console_type": {
                "type": "string", "label": "Console Type",
                "help": "WING, WING Rack or WING Compact, from the model the console reports.",
            },
            "serial_number": {
                "type": "string", "label": "Serial Number",
                "help": "The console's serial number.",
            },
            "firmware_version": {
                "type": "string", "label": "Firmware Version",
                "help": "The console's firmware version.",
            },
            "osc_read_only": {
                "type": "boolean", "label": "OSC Remote Lock", "cloud_priority": "high",
                "help": (
                    "True while the console's Remote Lock makes OSC read-only: it "
                    "then ignores every change, and OpenAVC refuses to send one."
                ),
            },
            "current_scene_index": {
                "type": "integer", "label": "Current Scene Number",
                "help": "The number of the scene loaded from the open show; 0 when none is.",
            },
            "current_scene": {
                "type": "string", "label": "Current Scene",
                "help": "The scene loaded from the open show, as the console names it.",
            },
            "current_scene_tag": {
                "type": "integer", "label": "Current Scene Tag",
                "help": "The tag number of the loaded scene, when it has one.",
            },
            "current_show": {
                "type": "string", "label": "Current Show",
                "help": "The show open on the console.",
            },
            "solo_active": {
                "type": "boolean", "label": "Solo Active",
                "help": "True while anything on the console is soloed.",
            },
            "clock_locked": {
                "type": "boolean", "label": "Clock Locked", "cloud_priority": "high",
                "help": "True while the console's audio clock is locked.",
            },
            "aes50_a_status": {
                "type": "enum", "label": "AES50 A",
                "values": ["none", "ok", "error", "updating"],
                "help": "AES50 port A: none connected, ok, error, or a connected device updating.",
            },
            "aes50_b_status": {
                "type": "enum", "label": "AES50 B",
                "values": ["none", "ok", "error", "updating"],
                "help": "AES50 port B: none connected, ok, error, or a connected device updating.",
            },
            "aes50_c_status": {
                "type": "enum", "label": "AES50 C",
                "values": ["none", "ok", "error", "updating"],
                "help": "AES50 port C: none connected, ok, error, or a connected device updating.",
            },
            "player_state": {
                "type": "enum", "label": "USB Player",
                "values": ["stop", "play", "pause", "error"],
                "help": "What the USB player is doing.",
            },
            "player_song": {
                "type": "string", "label": "Player Song", "cloud_priority": "low",
                "help": "The song the USB player has loaded.",
            },
            "player_artist": {
                "type": "string", "label": "Player Artist", "cloud_priority": "low",
                "help": "The artist of the song the USB player has loaded.",
            },
            "player_repeat": {
                "type": "boolean", "label": "Player Repeat",
                "help": "True while the USB player repeats its playlist.",
            },
            "recorder_state": {
                "type": "enum", "label": "USB Recorder",
                "values": ["stop", "record", "pause", "error"],
                "help": "What the USB recorder is doing.",
            },
            "recorder_file": {
                "type": "string", "label": "Recording File", "cloud_priority": "low",
                "help": "The file the USB recorder is writing.",
            },
            "recorder_time": {
                "type": "number", "label": "Recording Time", "cloud_priority": "low",
                "help": "How long the USB recorder has been recording, as the console counts it.",
            },
            "last_error": {
                "type": "string", "label": "Last Error",
                "help": "The last change the console refused, in its own words.",
            },
        },
        "child_entity_types": {
            "channel": {
                "label": "Channel",
                "label_plural": "Channels",
                "id_format": {"type": "integer", "min": 1, "max": 40},
                "label_field": "name",
                "summary_fields": ["name", "mute", "fader_db"],
                "instances": {"count": 40, "label": "Ch {id}"},
                "state_variables": {
                    "name": {
                        "type": "string",
                        "label": "Name",
                        "cloud_priority": "low",
                        "help": "The channel's name on the console (16 characters at most).",
                    },
                    "mute": {
                        "type": "boolean",
                        "label": "Mute",
                        "control": True,
                        "cloud_priority": "high",
                        "help": "True while the channel is muted.",
                    },
                    "fader_db": {
                        "type": "number",
                        "label": "Fader",
                        "min": -144,
                        "max": 10,
                        "step": 0.1,
                        "unit": "dB",
                        "control": True,
                        "help": "The channel's fader in dB. -144 is fully down (-inf), 0 is unity, +10 is the top of the fader.",
                    },
                    "pan": {
                        "type": "number",
                        "label": "Pan",
                        "min": -100,
                        "max": 100,
                        "step": 1,
                        "control": True,
                        "help": "Pan from -100 (left) through 0 (center) to 100 (right).",
                    },
                    "solo": {
                        "type": "boolean",
                        "label": "Solo",
                        "help": "True while the channel is soloed.",
                    },
                },
            },
            "aux": {
                "label": "Aux",
                "label_plural": "Auxes",
                "id_format": {"type": "integer", "min": 1, "max": 8},
                "label_field": "name",
                "summary_fields": ["name", "mute", "fader_db"],
                "instances": {"count": 8, "label": "Aux {id}"},
                "state_variables": {
                    "name": {
                        "type": "string",
                        "label": "Name",
                        "cloud_priority": "low",
                        "help": "The aux's name on the console (16 characters at most).",
                    },
                    "mute": {
                        "type": "boolean",
                        "label": "Mute",
                        "control": True,
                        "cloud_priority": "high",
                        "help": "True while the aux is muted.",
                    },
                    "fader_db": {
                        "type": "number",
                        "label": "Fader",
                        "min": -144,
                        "max": 10,
                        "step": 0.1,
                        "unit": "dB",
                        "control": True,
                        "help": "The aux's fader in dB. -144 is fully down (-inf), 0 is unity, +10 is the top of the fader.",
                    },
                    "pan": {
                        "type": "number",
                        "label": "Pan",
                        "min": -100,
                        "max": 100,
                        "step": 1,
                        "control": True,
                        "help": "Pan from -100 (left) through 0 (center) to 100 (right).",
                    },
                    "solo": {
                        "type": "boolean",
                        "label": "Solo",
                        "help": "True while the aux is soloed.",
                    },
                },
            },
            "bus": {
                "label": "Bus",
                "label_plural": "Buses",
                "id_format": {"type": "integer", "min": 1, "max": 16},
                "label_field": "name",
                "summary_fields": ["name", "mute", "fader_db"],
                "instances": {"count": 16, "label": "Bus {id}"},
                "state_variables": {
                    "name": {
                        "type": "string",
                        "label": "Name",
                        "cloud_priority": "low",
                        "help": "The bus's name on the console (16 characters at most).",
                    },
                    "mute": {
                        "type": "boolean",
                        "label": "Mute",
                        "control": True,
                        "cloud_priority": "high",
                        "help": "True while the bus is muted.",
                    },
                    "fader_db": {
                        "type": "number",
                        "label": "Fader",
                        "min": -144,
                        "max": 10,
                        "step": 0.1,
                        "unit": "dB",
                        "control": True,
                        "help": "The bus's fader in dB. -144 is fully down (-inf), 0 is unity, +10 is the top of the fader.",
                    },
                    "pan": {
                        "type": "number",
                        "label": "Pan",
                        "min": -100,
                        "max": 100,
                        "step": 1,
                        "control": True,
                        "help": "Pan from -100 (left) through 0 (center) to 100 (right).",
                    },
                    "solo": {
                        "type": "boolean",
                        "label": "Solo",
                        "help": "True while the bus is soloed.",
                    },
                },
            },
            "main": {
                "label": "Main",
                "label_plural": "Mains",
                "id_format": {"type": "integer", "min": 1, "max": 4},
                "label_field": "name",
                "summary_fields": ["name", "mute", "fader_db"],
                "instances": {"count": 4, "label": "Main {id}"},
                "state_variables": {
                    "name": {
                        "type": "string",
                        "label": "Name",
                        "cloud_priority": "low",
                        "help": "The main's name on the console (16 characters at most).",
                    },
                    "mute": {
                        "type": "boolean",
                        "label": "Mute",
                        "control": True,
                        "cloud_priority": "high",
                        "help": "True while the main is muted.",
                    },
                    "fader_db": {
                        "type": "number",
                        "label": "Fader",
                        "min": -144,
                        "max": 10,
                        "step": 0.1,
                        "unit": "dB",
                        "control": True,
                        "help": "The main's fader in dB. -144 is fully down (-inf), 0 is unity, +10 is the top of the fader.",
                    },
                    "pan": {
                        "type": "number",
                        "label": "Pan",
                        "min": -100,
                        "max": 100,
                        "step": 1,
                        "control": True,
                        "help": "Pan from -100 (left) through 0 (center) to 100 (right).",
                    },
                    "solo": {
                        "type": "boolean",
                        "label": "Solo",
                        "help": "True while the main is soloed.",
                    },
                },
            },
            "matrix": {
                "label": "Matrix",
                "label_plural": "Matrices",
                "id_format": {"type": "integer", "min": 1, "max": 8},
                "label_field": "name",
                "summary_fields": ["name", "mute", "fader_db"],
                "instances": {"count": 8, "label": "Mtx {id}"},
                "state_variables": {
                    "name": {
                        "type": "string",
                        "label": "Name",
                        "cloud_priority": "low",
                        "help": "The matrix's name on the console (16 characters at most).",
                    },
                    "mute": {
                        "type": "boolean",
                        "label": "Mute",
                        "control": True,
                        "cloud_priority": "high",
                        "help": "True while the matrix is muted.",
                    },
                    "fader_db": {
                        "type": "number",
                        "label": "Fader",
                        "min": -144,
                        "max": 10,
                        "step": 0.1,
                        "unit": "dB",
                        "control": True,
                        "help": "The matrix's fader in dB. -144 is fully down (-inf), 0 is unity, +10 is the top of the fader.",
                    },
                    "pan": {
                        "type": "number",
                        "label": "Pan",
                        "min": -100,
                        "max": 100,
                        "step": 1,
                        "control": True,
                        "help": "Pan from -100 (left) through 0 (center) to 100 (right).",
                    },
                    "solo": {
                        "type": "boolean",
                        "label": "Solo",
                        "help": "True while the matrix is soloed.",
                    },
                },
            },
            "dca": {
                "label": "DCA",
                "label_plural": "DCAs",
                "id_format": {"type": "integer", "min": 1, "max": 16},
                "label_field": "name",
                "summary_fields": ["name", "mute", "fader_db"],
                "instances": {"count": 16, "label": "DCA {id}"},
                "state_variables": {
                    "name": {
                        "type": "string",
                        "label": "Name",
                        "cloud_priority": "low",
                        "help": "The dca's name on the console (8 characters at most).",
                    },
                    "mute": {
                        "type": "boolean",
                        "label": "Mute",
                        "control": True,
                        "cloud_priority": "high",
                        "help": "True while the dca is muted.",
                    },
                    "fader_db": {
                        "type": "number",
                        "label": "Fader",
                        "min": -144,
                        "max": 10,
                        "step": 0.1,
                        "unit": "dB",
                        "control": True,
                        "help": "The dca's fader in dB. -144 is fully down (-inf), 0 is unity, +10 is the top of the fader.",
                    },
                    "solo": {
                        "type": "boolean",
                        "label": "Solo",
                        "help": "True while the dca is soloed.",
                    },
                },
            },
            "mute_group": {
                "label": "Mute Group",
                "label_plural": "Mute Groups",
                "id_format": {"type": "integer", "min": 1, "max": 8},
                "label_field": "name",
                "summary_fields": ["name", "mute"],
                "instances": {"count": 8, "label": "Mute Group {id}"},
                "state_variables": {
                    "name": {
                        "type": "string",
                        "label": "Name",
                        "cloud_priority": "low",
                        "help": "The mute group's name on the console (8 characters at most).",
                    },
                    "mute": {
                        "type": "boolean",
                        "label": "Active",
                        "control": True,
                        "cloud_priority": "high",
                        "help": "True while the mute group is on, muting everything assigned to it.",
                    },
                },
            },
        },
        "commands": {
        "set_channel_fader": {
            "label": "Set Channel Fader",
            "params": {
                "channel": {
                    "type": "child_id",
                    "child_type": "channel",
                    "label": "Channel",
                    "required": True,
                },
                "level": {
                    "type": "number",
                    "label": "Level",
                    "unit": "dB",
                    "min": -144,
                    "max": 10,
                    "required": True,
                    "description": "-144 is fully down (-inf), 0 is unity, +10 is the top of the fader.",
                },
            },
            "sets": {"fader_db": "{level}"},
            "help": "Moves the channel's fader to the level given.",
        },
        "nudge_channel_fader": {
            "label": "Nudge Channel Fader",
            "params": {
                "channel": {
                    "type": "child_id",
                    "child_type": "channel",
                    "label": "Channel",
                    "required": True,
                },
                "step": {
                    "type": "number",
                    "label": "Step",
                    "unit": "dB",
                    "min": -20,
                    "max": 20,
                    "default": 3,
                    "required": True,
                    "description": "dB to move by: positive raises the fader, negative lowers it.",
                },
            },
            "help": "Moves the channel's fader up or down by the step, from the level the console last reported, and stops at the top and bottom of the fader.",
        },
        "mute_channel": {
            "label": "Mute Channel",
            "params": {
                "channel": {
                    "type": "child_id",
                    "child_type": "channel",
                    "label": "Channel",
                    "required": True,
                },
            },
            "sets": {"mute": True},
            "help": "Mutes the channel.",
        },
        "unmute_channel": {
            "label": "Unmute Channel",
            "params": {
                "channel": {
                    "type": "child_id",
                    "child_type": "channel",
                    "label": "Channel",
                    "required": True,
                },
            },
            "sets": {"mute": False},
            "help": "Unmutes the channel.",
        },
        "toggle_channel_mute": {
            "label": "Toggle Channel Mute",
            "params": {
                "channel": {
                    "type": "child_id",
                    "child_type": "channel",
                    "label": "Channel",
                    "required": True,
                },
            },
            "help": "Mutes the channel if it is unmuted, and unmutes it if it is muted.",
        },
        "set_channel_pan": {
            "label": "Set Channel Pan",
            "params": {
                "channel": {
                    "type": "child_id",
                    "child_type": "channel",
                    "label": "Channel",
                    "required": True,
                },
                "pan": {
                    "type": "number",
                    "label": "Pan",
                    "min": -100,
                    "max": 100,
                    "required": True,
                    "description": "-100 is hard left, 0 is center, 100 is hard right.",
                },
            },
            "sets": {"pan": "{pan}"},
            "help": "Pans the channel.",
        },
        "set_channel_name": {
            "label": "Rename Channel",
            "params": {
                "channel": {
                    "type": "child_id",
                    "child_type": "channel",
                    "label": "Channel",
                    "required": True,
                },
                "name": {
                    "type": "string",
                    "label": "Name",
                    "required": True,
                    "pattern": "^.{0,16}$",
                    "description": "16 characters at most.",
                },
            },
            "sets": {"name": "{name}"},
            "help": "Changes the channel's name on the console.",
        },
        "set_channel_solo": {
            "label": "Set Channel Solo",
            "params": {
                "channel": {
                    "type": "child_id",
                    "child_type": "channel",
                    "label": "Channel",
                    "required": True,
                },
                "on": {"type": "boolean", "label": "Solo On", "required": True},
            },
            "sets": {"solo": "{on}"},
            "help": "Solos the channel, or releases its solo.",
        },
        "set_aux_fader": {
            "label": "Set Aux Fader",
            "params": {
                "aux": {
                    "type": "child_id",
                    "child_type": "aux",
                    "label": "Aux",
                    "required": True,
                },
                "level": {
                    "type": "number",
                    "label": "Level",
                    "unit": "dB",
                    "min": -144,
                    "max": 10,
                    "required": True,
                    "description": "-144 is fully down (-inf), 0 is unity, +10 is the top of the fader.",
                },
            },
            "sets": {"fader_db": "{level}"},
            "help": "Moves the aux's fader to the level given.",
        },
        "nudge_aux_fader": {
            "label": "Nudge Aux Fader",
            "params": {
                "aux": {
                    "type": "child_id",
                    "child_type": "aux",
                    "label": "Aux",
                    "required": True,
                },
                "step": {
                    "type": "number",
                    "label": "Step",
                    "unit": "dB",
                    "min": -20,
                    "max": 20,
                    "default": 3,
                    "required": True,
                    "description": "dB to move by: positive raises the fader, negative lowers it.",
                },
            },
            "help": "Moves the aux's fader up or down by the step, from the level the console last reported, and stops at the top and bottom of the fader.",
        },
        "mute_aux": {
            "label": "Mute Aux",
            "params": {
                "aux": {
                    "type": "child_id",
                    "child_type": "aux",
                    "label": "Aux",
                    "required": True,
                },
            },
            "sets": {"mute": True},
            "help": "Mutes the aux.",
        },
        "unmute_aux": {
            "label": "Unmute Aux",
            "params": {
                "aux": {
                    "type": "child_id",
                    "child_type": "aux",
                    "label": "Aux",
                    "required": True,
                },
            },
            "sets": {"mute": False},
            "help": "Unmutes the aux.",
        },
        "toggle_aux_mute": {
            "label": "Toggle Aux Mute",
            "params": {
                "aux": {
                    "type": "child_id",
                    "child_type": "aux",
                    "label": "Aux",
                    "required": True,
                },
            },
            "help": "Mutes the aux if it is unmuted, and unmutes it if it is muted.",
        },
        "set_aux_pan": {
            "label": "Set Aux Pan",
            "params": {
                "aux": {
                    "type": "child_id",
                    "child_type": "aux",
                    "label": "Aux",
                    "required": True,
                },
                "pan": {
                    "type": "number",
                    "label": "Pan",
                    "min": -100,
                    "max": 100,
                    "required": True,
                    "description": "-100 is hard left, 0 is center, 100 is hard right.",
                },
            },
            "sets": {"pan": "{pan}"},
            "help": "Pans the aux.",
        },
        "set_aux_name": {
            "label": "Rename Aux",
            "params": {
                "aux": {
                    "type": "child_id",
                    "child_type": "aux",
                    "label": "Aux",
                    "required": True,
                },
                "name": {
                    "type": "string",
                    "label": "Name",
                    "required": True,
                    "pattern": "^.{0,16}$",
                    "description": "16 characters at most.",
                },
            },
            "sets": {"name": "{name}"},
            "help": "Changes the aux's name on the console.",
        },
        "set_aux_solo": {
            "label": "Set Aux Solo",
            "params": {
                "aux": {
                    "type": "child_id",
                    "child_type": "aux",
                    "label": "Aux",
                    "required": True,
                },
                "on": {"type": "boolean", "label": "Solo On", "required": True},
            },
            "sets": {"solo": "{on}"},
            "help": "Solos the aux, or releases its solo.",
        },
        "set_bus_fader": {
            "label": "Set Bus Fader",
            "params": {
                "bus": {
                    "type": "child_id",
                    "child_type": "bus",
                    "label": "Bus",
                    "required": True,
                },
                "level": {
                    "type": "number",
                    "label": "Level",
                    "unit": "dB",
                    "min": -144,
                    "max": 10,
                    "required": True,
                    "description": "-144 is fully down (-inf), 0 is unity, +10 is the top of the fader.",
                },
            },
            "sets": {"fader_db": "{level}"},
            "help": "Moves the bus's fader to the level given.",
        },
        "nudge_bus_fader": {
            "label": "Nudge Bus Fader",
            "params": {
                "bus": {
                    "type": "child_id",
                    "child_type": "bus",
                    "label": "Bus",
                    "required": True,
                },
                "step": {
                    "type": "number",
                    "label": "Step",
                    "unit": "dB",
                    "min": -20,
                    "max": 20,
                    "default": 3,
                    "required": True,
                    "description": "dB to move by: positive raises the fader, negative lowers it.",
                },
            },
            "help": "Moves the bus's fader up or down by the step, from the level the console last reported, and stops at the top and bottom of the fader.",
        },
        "mute_bus": {
            "label": "Mute Bus",
            "params": {
                "bus": {
                    "type": "child_id",
                    "child_type": "bus",
                    "label": "Bus",
                    "required": True,
                },
            },
            "sets": {"mute": True},
            "help": "Mutes the bus.",
        },
        "unmute_bus": {
            "label": "Unmute Bus",
            "params": {
                "bus": {
                    "type": "child_id",
                    "child_type": "bus",
                    "label": "Bus",
                    "required": True,
                },
            },
            "sets": {"mute": False},
            "help": "Unmutes the bus.",
        },
        "toggle_bus_mute": {
            "label": "Toggle Bus Mute",
            "params": {
                "bus": {
                    "type": "child_id",
                    "child_type": "bus",
                    "label": "Bus",
                    "required": True,
                },
            },
            "help": "Mutes the bus if it is unmuted, and unmutes it if it is muted.",
        },
        "set_bus_pan": {
            "label": "Set Bus Pan",
            "params": {
                "bus": {
                    "type": "child_id",
                    "child_type": "bus",
                    "label": "Bus",
                    "required": True,
                },
                "pan": {
                    "type": "number",
                    "label": "Pan",
                    "min": -100,
                    "max": 100,
                    "required": True,
                    "description": "-100 is hard left, 0 is center, 100 is hard right.",
                },
            },
            "sets": {"pan": "{pan}"},
            "help": "Pans the bus.",
        },
        "set_bus_name": {
            "label": "Rename Bus",
            "params": {
                "bus": {
                    "type": "child_id",
                    "child_type": "bus",
                    "label": "Bus",
                    "required": True,
                },
                "name": {
                    "type": "string",
                    "label": "Name",
                    "required": True,
                    "pattern": "^.{0,16}$",
                    "description": "16 characters at most.",
                },
            },
            "sets": {"name": "{name}"},
            "help": "Changes the bus's name on the console.",
        },
        "set_bus_solo": {
            "label": "Set Bus Solo",
            "params": {
                "bus": {
                    "type": "child_id",
                    "child_type": "bus",
                    "label": "Bus",
                    "required": True,
                },
                "on": {"type": "boolean", "label": "Solo On", "required": True},
            },
            "sets": {"solo": "{on}"},
            "help": "Solos the bus, or releases its solo.",
        },
        "set_main_fader": {
            "label": "Set Main Fader",
            "params": {
                "main": {
                    "type": "child_id",
                    "child_type": "main",
                    "label": "Main",
                    "required": True,
                },
                "level": {
                    "type": "number",
                    "label": "Level",
                    "unit": "dB",
                    "min": -144,
                    "max": 10,
                    "required": True,
                    "description": "-144 is fully down (-inf), 0 is unity, +10 is the top of the fader.",
                },
            },
            "sets": {"fader_db": "{level}"},
            "help": "Moves the main's fader to the level given.",
        },
        "nudge_main_fader": {
            "label": "Nudge Main Fader",
            "params": {
                "main": {
                    "type": "child_id",
                    "child_type": "main",
                    "label": "Main",
                    "required": True,
                },
                "step": {
                    "type": "number",
                    "label": "Step",
                    "unit": "dB",
                    "min": -20,
                    "max": 20,
                    "default": 3,
                    "required": True,
                    "description": "dB to move by: positive raises the fader, negative lowers it.",
                },
            },
            "help": "Moves the main's fader up or down by the step, from the level the console last reported, and stops at the top and bottom of the fader.",
        },
        "mute_main": {
            "label": "Mute Main",
            "params": {
                "main": {
                    "type": "child_id",
                    "child_type": "main",
                    "label": "Main",
                    "required": True,
                },
            },
            "sets": {"mute": True},
            "help": "Mutes the main.",
        },
        "unmute_main": {
            "label": "Unmute Main",
            "params": {
                "main": {
                    "type": "child_id",
                    "child_type": "main",
                    "label": "Main",
                    "required": True,
                },
            },
            "sets": {"mute": False},
            "help": "Unmutes the main.",
        },
        "toggle_main_mute": {
            "label": "Toggle Main Mute",
            "params": {
                "main": {
                    "type": "child_id",
                    "child_type": "main",
                    "label": "Main",
                    "required": True,
                },
            },
            "help": "Mutes the main if it is unmuted, and unmutes it if it is muted.",
        },
        "set_main_pan": {
            "label": "Set Main Pan",
            "params": {
                "main": {
                    "type": "child_id",
                    "child_type": "main",
                    "label": "Main",
                    "required": True,
                },
                "pan": {
                    "type": "number",
                    "label": "Pan",
                    "min": -100,
                    "max": 100,
                    "required": True,
                    "description": "-100 is hard left, 0 is center, 100 is hard right.",
                },
            },
            "sets": {"pan": "{pan}"},
            "help": "Pans the main.",
        },
        "set_main_name": {
            "label": "Rename Main",
            "params": {
                "main": {
                    "type": "child_id",
                    "child_type": "main",
                    "label": "Main",
                    "required": True,
                },
                "name": {
                    "type": "string",
                    "label": "Name",
                    "required": True,
                    "pattern": "^.{0,16}$",
                    "description": "16 characters at most.",
                },
            },
            "sets": {"name": "{name}"},
            "help": "Changes the main's name on the console.",
        },
        "set_main_solo": {
            "label": "Set Main Solo",
            "params": {
                "main": {
                    "type": "child_id",
                    "child_type": "main",
                    "label": "Main",
                    "required": True,
                },
                "on": {"type": "boolean", "label": "Solo On", "required": True},
            },
            "sets": {"solo": "{on}"},
            "help": "Solos the main, or releases its solo.",
        },
        "set_matrix_fader": {
            "label": "Set Matrix Fader",
            "params": {
                "matrix": {
                    "type": "child_id",
                    "child_type": "matrix",
                    "label": "Matrix",
                    "required": True,
                },
                "level": {
                    "type": "number",
                    "label": "Level",
                    "unit": "dB",
                    "min": -144,
                    "max": 10,
                    "required": True,
                    "description": "-144 is fully down (-inf), 0 is unity, +10 is the top of the fader.",
                },
            },
            "sets": {"fader_db": "{level}"},
            "help": "Moves the matrix's fader to the level given.",
        },
        "nudge_matrix_fader": {
            "label": "Nudge Matrix Fader",
            "params": {
                "matrix": {
                    "type": "child_id",
                    "child_type": "matrix",
                    "label": "Matrix",
                    "required": True,
                },
                "step": {
                    "type": "number",
                    "label": "Step",
                    "unit": "dB",
                    "min": -20,
                    "max": 20,
                    "default": 3,
                    "required": True,
                    "description": "dB to move by: positive raises the fader, negative lowers it.",
                },
            },
            "help": "Moves the matrix's fader up or down by the step, from the level the console last reported, and stops at the top and bottom of the fader.",
        },
        "mute_matrix": {
            "label": "Mute Matrix",
            "params": {
                "matrix": {
                    "type": "child_id",
                    "child_type": "matrix",
                    "label": "Matrix",
                    "required": True,
                },
            },
            "sets": {"mute": True},
            "help": "Mutes the matrix.",
        },
        "unmute_matrix": {
            "label": "Unmute Matrix",
            "params": {
                "matrix": {
                    "type": "child_id",
                    "child_type": "matrix",
                    "label": "Matrix",
                    "required": True,
                },
            },
            "sets": {"mute": False},
            "help": "Unmutes the matrix.",
        },
        "toggle_matrix_mute": {
            "label": "Toggle Matrix Mute",
            "params": {
                "matrix": {
                    "type": "child_id",
                    "child_type": "matrix",
                    "label": "Matrix",
                    "required": True,
                },
            },
            "help": "Mutes the matrix if it is unmuted, and unmutes it if it is muted.",
        },
        "set_matrix_pan": {
            "label": "Set Matrix Pan",
            "params": {
                "matrix": {
                    "type": "child_id",
                    "child_type": "matrix",
                    "label": "Matrix",
                    "required": True,
                },
                "pan": {
                    "type": "number",
                    "label": "Pan",
                    "min": -100,
                    "max": 100,
                    "required": True,
                    "description": "-100 is hard left, 0 is center, 100 is hard right.",
                },
            },
            "sets": {"pan": "{pan}"},
            "help": "Pans the matrix.",
        },
        "set_matrix_name": {
            "label": "Rename Matrix",
            "params": {
                "matrix": {
                    "type": "child_id",
                    "child_type": "matrix",
                    "label": "Matrix",
                    "required": True,
                },
                "name": {
                    "type": "string",
                    "label": "Name",
                    "required": True,
                    "pattern": "^.{0,16}$",
                    "description": "16 characters at most.",
                },
            },
            "sets": {"name": "{name}"},
            "help": "Changes the matrix's name on the console.",
        },
        "set_matrix_solo": {
            "label": "Set Matrix Solo",
            "params": {
                "matrix": {
                    "type": "child_id",
                    "child_type": "matrix",
                    "label": "Matrix",
                    "required": True,
                },
                "on": {"type": "boolean", "label": "Solo On", "required": True},
            },
            "sets": {"solo": "{on}"},
            "help": "Solos the matrix, or releases its solo.",
        },
        "set_dca_fader": {
            "label": "Set DCA Fader",
            "params": {
                "dca": {
                    "type": "child_id",
                    "child_type": "dca",
                    "label": "DCA",
                    "required": True,
                },
                "level": {
                    "type": "number",
                    "label": "Level",
                    "unit": "dB",
                    "min": -144,
                    "max": 10,
                    "required": True,
                    "description": "-144 is fully down (-inf), 0 is unity, +10 is the top of the fader.",
                },
            },
            "sets": {"fader_db": "{level}"},
            "help": "Moves the dca's fader to the level given.",
        },
        "nudge_dca_fader": {
            "label": "Nudge DCA Fader",
            "params": {
                "dca": {
                    "type": "child_id",
                    "child_type": "dca",
                    "label": "DCA",
                    "required": True,
                },
                "step": {
                    "type": "number",
                    "label": "Step",
                    "unit": "dB",
                    "min": -20,
                    "max": 20,
                    "default": 3,
                    "required": True,
                    "description": "dB to move by: positive raises the fader, negative lowers it.",
                },
            },
            "help": "Moves the dca's fader up or down by the step, from the level the console last reported, and stops at the top and bottom of the fader.",
        },
        "mute_dca": {
            "label": "Mute DCA",
            "params": {
                "dca": {
                    "type": "child_id",
                    "child_type": "dca",
                    "label": "DCA",
                    "required": True,
                },
            },
            "sets": {"mute": True},
            "help": "Mutes the dca.",
        },
        "unmute_dca": {
            "label": "Unmute DCA",
            "params": {
                "dca": {
                    "type": "child_id",
                    "child_type": "dca",
                    "label": "DCA",
                    "required": True,
                },
            },
            "sets": {"mute": False},
            "help": "Unmutes the dca.",
        },
        "toggle_dca_mute": {
            "label": "Toggle DCA Mute",
            "params": {
                "dca": {
                    "type": "child_id",
                    "child_type": "dca",
                    "label": "DCA",
                    "required": True,
                },
            },
            "help": "Mutes the dca if it is unmuted, and unmutes it if it is muted.",
        },
        "set_dca_name": {
            "label": "Rename DCA",
            "params": {
                "dca": {
                    "type": "child_id",
                    "child_type": "dca",
                    "label": "DCA",
                    "required": True,
                },
                "name": {
                    "type": "string",
                    "label": "Name",
                    "required": True,
                    "pattern": "^.{0,8}$",
                    "description": "8 characters at most.",
                },
            },
            "sets": {"name": "{name}"},
            "help": "Changes the dca's name on the console.",
        },
        "set_dca_solo": {
            "label": "Set DCA Solo",
            "params": {
                "dca": {
                    "type": "child_id",
                    "child_type": "dca",
                    "label": "DCA",
                    "required": True,
                },
                "on": {"type": "boolean", "label": "Solo On", "required": True},
            },
            "sets": {"solo": "{on}"},
            "help": "Solos the dca, or releases its solo.",
        },
        "mute_group_on": {
            "label": "Turn On Mute Group",
            "params": {
                "mute_group": {
                    "type": "child_id",
                    "child_type": "mute_group",
                    "label": "Mute Group",
                    "required": True,
                },
            },
            "sets": {"mute": True},
            "help": "Turns the mute group on, muting everything assigned to it.",
        },
        "mute_group_off": {
            "label": "Turn Off Mute Group",
            "params": {
                "mute_group": {
                    "type": "child_id",
                    "child_type": "mute_group",
                    "label": "Mute Group",
                    "required": True,
                },
            },
            "sets": {"mute": False},
            "help": "Turns the mute group off, unmuting what it muted.",
        },
        "toggle_mute_group": {
            "label": "Toggle Mute Group",
            "params": {
                "mute_group": {
                    "type": "child_id",
                    "child_type": "mute_group",
                    "label": "Mute Group",
                    "required": True,
                },
            },
            "help": "Turns the mute group on if it is off, and off if it is on.",
        },
        "set_mute_group_name": {
            "label": "Rename Mute Group",
            "params": {
                "mute_group": {
                    "type": "child_id",
                    "child_type": "mute_group",
                    "label": "Mute Group",
                    "required": True,
                },
                "name": {
                    "type": "string",
                    "label": "Name",
                    "required": True,
                    "pattern": "^.{0,8}$",
                    "description": "8 characters at most.",
                },
            },
            "sets": {"name": "{name}"},
            "help": "Changes the mute group's name on the console.",
        },
        "set_channel_send_level": {
            "label": "Set Channel Send Level",
            "params": {
                "channel": {
                    "type": "child_id",
                    "child_type": "channel",
                    "label": "Channel",
                    "required": True,
                },
                "bus": {
                    "type": "integer",
                    "label": "Bus",
                    "min": 1,
                    "max": 16,
                    "required": True,
                },
                "level": {
                    "type": "number",
                    "label": "Level",
                    "unit": "dB",
                    "min": -144,
                    "max": 10,
                    "required": True,
                    "description": "-144 is fully down (-inf), 0 is unity, +10 is the top of the fader.",
                },
            },
            "help": "Sets how much of the channel is sent to the bus.",
        },
        "set_channel_send_on": {
            "label": "Set Channel Send On",
            "params": {
                "channel": {
                    "type": "child_id",
                    "child_type": "channel",
                    "label": "Channel",
                    "required": True,
                },
                "bus": {
                    "type": "integer",
                    "label": "Bus",
                    "min": 1,
                    "max": 16,
                    "required": True,
                },
                "on": {"type": "boolean", "label": "Send On", "required": True},
            },
            "help": "Turns the channel's send to the bus on or off.",
        },
        "set_aux_send_level": {
            "label": "Set Aux Send Level",
            "params": {
                "aux": {
                    "type": "child_id",
                    "child_type": "aux",
                    "label": "Aux",
                    "required": True,
                },
                "bus": {
                    "type": "integer",
                    "label": "Bus",
                    "min": 1,
                    "max": 16,
                    "required": True,
                },
                "level": {
                    "type": "number",
                    "label": "Level",
                    "unit": "dB",
                    "min": -144,
                    "max": 10,
                    "required": True,
                    "description": "-144 is fully down (-inf), 0 is unity, +10 is the top of the fader.",
                },
            },
            "help": "Sets how much of the aux is sent to the bus.",
        },
        "set_aux_send_on": {
            "label": "Set Aux Send On",
            "params": {
                "aux": {
                    "type": "child_id",
                    "child_type": "aux",
                    "label": "Aux",
                    "required": True,
                },
                "bus": {
                    "type": "integer",
                    "label": "Bus",
                    "min": 1,
                    "max": 16,
                    "required": True,
                },
                "on": {"type": "boolean", "label": "Send On", "required": True},
            },
            "help": "Turns the aux's send to the bus on or off.",
        },
            "recall_scene": {
                "label": "Recall Scene",
                "params": {
                    "scene": {
                        "type": "integer", "label": "Scene Number", "min": 1, "max": 16384,
                        "required": True,
                        "description": "The scene's place in the open show's list, from 1.",
                    },
                },
                "sets": {"current_scene_index": "{scene}"},
                "help": "Loads the scene at that place in the show open on the console.",
            },
            "recall_scene_tag": {
                "label": "Recall Tagged Scene",
                "params": {
                    "tag": {
                        "type": "integer", "label": "Tag", "min": 1, "max": 128,
                        "required": True,
                        "description": "The tag number given to the scene in the show.",
                    },
                },
                "sets": {"current_scene_tag": "{tag}"},
                "help": "Loads the scene of the open show that carries that tag.",
            },
            "scene_go": {
                "label": "Scene Go",
                "help": "Loads the scene selected in the open show, like the console's GO button.",
            },
            "scene_next": {
                "label": "Next Scene",
                "help": "Loads the next scene of the open show.",
            },
            "scene_previous": {
                "label": "Previous Scene",
                "help": "Loads the previous scene of the open show.",
            },
            "player_play": {
                "label": "Play (USB Player)",
                "sets": {"player_state": "play"},
                "help": "Starts the USB player. A playlist must be open on the console.",
            },
            "player_pause": {
                "label": "Pause (USB Player)",
                "sets": {"player_state": "pause"},
                "help": "Pauses the USB player.",
            },
            "player_stop": {
                "label": "Stop (USB Player)",
                "sets": {"player_state": "stop"},
                "help": "Stops the USB player.",
            },
            "player_next": {
                "label": "Next Song",
                "help": "Moves the USB player to the next song in the playlist.",
            },
            "player_previous": {
                "label": "Previous Song",
                "help": "Moves the USB player to the previous song in the playlist.",
            },
            "set_player_repeat": {
                "label": "Set Player Repeat",
                "params": {"on": {"type": "boolean", "label": "Repeat", "required": True}},
                "sets": {"player_repeat": "{on}"},
                "help": "Turns the USB player's repeat on or off.",
            },
            "recorder_record": {
                "label": "Record (USB Recorder)",
                "sets": {"recorder_state": "record"},
                "help": "Starts the USB recorder recording.",
            },
            "recorder_pause": {
                "label": "Pause (USB Recorder)",
                "sets": {"recorder_state": "pause"},
                "help": "Pauses the USB recorder.",
            },
            "recorder_stop": {
                "label": "Stop (USB Recorder)",
                "sets": {"recorder_state": "stop"},
                "help": "Stops the USB recorder and closes the file.",
            },
            "recorder_new_file": {
                "label": "New Recording File",
                "help": "Starts a new file on the USB recorder.",
            },
            "set_parameter": {
                "label": "Set Parameter",
                "params": {
                    "address": {
                        "type": "string", "label": "OSC Address", "required": True,
                        "pattern": r"^/\S+$",
                        "description": "A parameter's address, for example /ch/1/eq/on.",
                    },
                    "type": {
                        "type": "enum", "label": "Value Type",
                        "values": ["float", "integer", "string"], "default": "string",
                        "required": True,
                        "description": "string works for every parameter.",
                    },
                    "value": {"type": "string", "label": "Value", "required": True},
                },
                "help": (
                    "Sets any parameter in the console's tree by its OSC address, "
                    "for the ones this driver has no command for."
                ),
            },
            "refresh": {
                "label": "Read Everything Again",
                "help": "Reads every strip and status value from the console again.",
            },
        },
        "quick_actions": [
            "recall_scene", "scene_next", "scene_previous",
            "toggle_channel_mute", "set_channel_fader",
        ],
        "actions": [
            {"id": "recall_scene", "kind": "command", "icon": "bookmark"},
            {"id": "scene_next", "kind": "command", "icon": "skip-forward"},
            {"id": "scene_previous", "kind": "command", "icon": "skip-back"},
            {"id": "toggle_channel_mute", "kind": "command", "icon": "volume-x"},
            {"id": "set_channel_fader", "kind": "command", "icon": "sliders-vertical"},
        ],
    }

    HEALTH_INTERVAL_S = 15.0
    HEALTH_TIMEOUT_S = 3.0
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = "The console stopped answering its identity query."

    def __init__(self, device_id: str, config: dict[str, Any], state, events) -> None:
        super().__init__(device_id, config, state, events)
        self._identity_waiters: list[asyncio.Future] = []
        self._wing_tasks: set[asyncio.Task] = set()
        self._renew_task: asyncio.Task | None = None

    # ── Sending ──

    async def _send(self, address: str, args: list[tuple[str, Any]] | None = None) -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self.transport.send(osc_encode_message(address, args or []))

    async def _read(self, *addresses: str) -> None:
        for address in addresses:
            await self._send(address)

    async def _write(self, address: str, tag: str, value: Any) -> None:
        """Write one parameter, then read it back: the console never echoes a
        client's own change."""
        self._refuse_while_locked()
        await self._send(address, [(tag, value)])
        await self._send(address)

    def _refuse_while_locked(self) -> None:
        if self.get_state("osc_read_only") is True:
            raise CommandRefusedError(
                "The console's Remote Lock makes OSC read-only, so it ignores "
                "every change. Turn it off in Setup > Remote on the console."
            )

    async def _ask_identity(self) -> None:
        """Send /? and wait for the console's answer."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._identity_waiters.append(fut)
        try:
            await self._send(IDENTIFY)
            await fut
        finally:
            if fut in self._identity_waiters:
                self._identity_waiters.remove(fut)

    # ── Connection lifecycle ──

    async def _post_connect(self) -> None:
        """Ask the console who it is before the device is reported connected."""
        try:
            await asyncio.wait_for(self._ask_identity(), IDENTIFY_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise ConnectionFaultError(
                "The console did not answer on the OSC port. Check the IP "
                "address, and that the address belongs to a WING.",
                code="no_response",
            ) from exc

    async def _initial_sync(self) -> None:
        self._register_children()
        await self._send(SUBSCRIBE)
        self._renew_task = self._spawn(self._renew_loop())
        await self.poll()

    def _register_children(self) -> None:
        for ctype, (_root, count, _leaves) in _CHILDREN.items():
            for n in range(1, count + 1):
                self.register_child(ctype, n)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._wing_tasks.add(task)
        task.add_done_callback(self._wing_tasks.discard)
        return task

    async def _renew_loop(self) -> None:
        """Ask for change notices again before the 10-second subscription ends."""
        while self.transport is not None and self.transport.connected:
            await asyncio.sleep(RENEW_INTERVAL_S)
            try:
                await self._send(SUBSCRIBE)
            except (ConnectionError, OSError):
                return

    async def poll(self) -> None:
        """Read every console-wide value and every strip again. Replies arrive
        through on_data_received; silence is the liveness check's to judge."""
        for address in _DEVICE_READS:
            await self._send(address)
            await asyncio.sleep(READ_GAP_S)
        for _ctype, (root, count, leaves) in _CHILDREN.items():
            for n in range(1, count + 1):
                for leaf in leaves:
                    await self._send(f"/{root}/{n}/{leaf}")
                    await asyncio.sleep(READ_GAP_S)

    async def refresh_children(self) -> dict[str, Any]:
        """The roster is fixed; refresh reads every strip again."""
        self._register_children()
        await self.poll()
        return {"refreshed": True}

    async def _liveness_probe(self) -> None:
        await self._ask_identity()

    async def _close_session(self) -> None:
        for task in list(self._wing_tasks):
            task.cancel()
        self._wing_tasks.clear()
        self._renew_task = None
        for fut in self._identity_waiters:
            if not fut.done():
                fut.set_exception(ConnectionError("The connection to the console closed."))
        self._identity_waiters.clear()

    # ── Commands ──

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        strip = _STRIP_COMMANDS.get(command)
        if strip is not None:
            ctype, action = strip
            await self._strip_command(ctype, action, params)
            return True
        if command in ("set_channel_send_level", "set_aux_send_level",
                       "set_channel_send_on", "set_aux_send_on"):
            await self._bus_send_command(command, params)
            return True
        if command == "recall_scene":
            await self._recall(int(params["scene"]), "GO")
        elif command == "recall_scene_tag":
            await self._recall(int(params["tag"]), "GOTAG")
        elif command == "scene_go":
            self._refuse_while_locked()
            await self._send("/$ctl/lib/$action", [("s", "GO")])
            await self._read(*_SCENE_READS)
        elif command in ("scene_next", "scene_previous"):
            # Footnote 79: $actionidx must be 0 for NEXT / PREV to move from
            # the loaded scene.
            await self._recall(0, "GONEXT" if command == "scene_next" else "GOPREV")
        elif command in _PLAYER_ACTIONS:
            self._refuse_while_locked()
            await self._send("/play/$action", [("s", _PLAYER_ACTIONS[command])])
            await self._read(*_PLAYER_READS)
        elif command == "set_player_repeat":
            await self._write("/play/repeat", "i", 1 if params.get("on") else 0)
        elif command in _RECORDER_ACTIONS:
            self._refuse_while_locked()
            await self._send("/rec/$action", [("s", _RECORDER_ACTIONS[command])])
            await self._read(*_RECORDER_READS)
        elif command == "set_parameter":
            await self._set_parameter(params)
        elif command == "refresh":
            await self.poll()
        else:
            raise ValueError(f"Unknown command: {command}")
        return True

    async def _recall(self, index: int, action: str) -> None:
        self._refuse_while_locked()
        await self._send("/$ctl/lib/$actionidx", [("i", index)])
        await self._send("/$ctl/lib/$action", [("s", action)])
        await self._read(*_SCENE_READS)

    async def _strip_command(self, ctype: str, action: str, params: dict[str, Any]) -> None:
        root = _CHILDREN[ctype][0]
        n = int(params[ctype])
        base = f"/{root}/{n}"
        if action == "set_fader":
            await self._write(f"{base}/fdr", "f", float(params["level"]))
        elif action == "nudge_fader":
            current = self.get_child_state(ctype, n).get("fader_db")
            if current is None:
                raise CommandRefusedError(
                    f"The console has not reported {ctype.replace('_', ' ')} {n}'s "
                    f"fader yet, so there is nothing to nudge from."
                )
            level = min(FADER_MAX_DB, max(FADER_MIN_DB, float(current) + float(params["step"])))
            await self._write(f"{base}/fdr", "f", level)
        elif action == "mute":
            await self._write(f"{base}/mute", "i", 1)
        elif action == "unmute":
            await self._write(f"{base}/mute", "i", 0)
        elif action == "toggle_mute":
            await self._write(f"{base}/mute", "i", -1)
        elif action == "set_pan":
            await self._write(f"{base}/pan", "f", float(params["pan"]))
        elif action == "set_name":
            await self._write(f"{base}/name", "s", str(params["name"]))
        elif action == "set_solo":
            await self._write(f"{base}/$solo", "i", 1 if params.get("on") else 0)
        else:  # pragma: no cover - the table above lists every action
            raise ValueError(f"Unknown strip action: {action}")

    async def _bus_send_command(self, command: str, params: dict[str, Any]) -> None:
        src = "channel" if command.startswith("set_channel_") else "aux"
        root = _CHILDREN[src][0]
        n = int(params[src])
        bus = int(params["bus"])
        if command.endswith("_level"):
            await self._write(f"/{root}/{n}/send/{bus}/lvl", "f", float(params["level"]))
        else:
            await self._write(f"/{root}/{n}/send/{bus}/on", "i", 1 if params.get("on") else 0)

    async def _set_parameter(self, params: dict[str, Any]) -> None:
        address = str(params["address"]).strip()
        tag = _ARG_TAGS[str(params.get("type") or "string")]
        raw = str(params.get("value", ""))
        value: Any = raw
        try:
            if tag == "f":
                value = float(raw)
            elif tag == "i":
                value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{raw!r} is not a {params.get('type')} value") from exc
        await self._write(address, tag, value)

    # ── Receiving ──

    async def on_data_received(self, data: bytes) -> None:
        try:
            messages = osc_decode_bundle(bytes(data))
        except Exception as exc:  # a malformed packet from the network
            log.debug(f"[{self.device_id}] Undecodable OSC packet: {exc}")
            return
        for address, args in messages:
            self._handle(address, args)

    def _handle(self, address: str, args: list[tuple[str, Any]]) -> None:
        if address == IDENTIFY:
            self._apply_identity(_reply_value(args))
            return
        match = _LEAF_RE.match(address)
        if match:
            self._apply_leaf(match.group(1), int(match.group(2)), match.group(3), args)
            return
        read = _DEVICE_READS.get(address)
        if read is not None:
            self._apply_device(read, _reply_value(args))
            return
        if address.endswith("*") and args and args[0][0] == "s":
            # A node write's answer: OK, or what was wrong ("Node Data").
            answer = str(args[0][1])
            if answer != "OK":
                self.set_state("last_error", f"The console refused a change: {answer}")
            return
        log.debug(f"[{self.device_id}] OSC not read: {address}")

    def _apply_identity(self, value: Any) -> None:
        fields = str(value or "").split(",", 5)
        if len(fields) == 6 and fields[0] == "WING":
            _wing, _ip, name, model, serial, firmware = fields
            self.set_states({
                "console_name": name,
                "model": model,
                "console_type": _CONSOLE_TYPES.get(model.lower(), model),
                "serial_number": serial,
                "firmware_version": firmware,
            })
        else:
            log.debug(f"[{self.device_id}] Unexpected /? answer: {value!r}")
        # Any answer to /? is the console answering.
        for fut in self._identity_waiters:
            if not fut.done():
                fut.set_result(True)

    def _apply_leaf(self, root: str, n: int, leaf: str, args: list[tuple[str, Any]]) -> None:
        ctype = _ROOT_TO_CHILD[root]
        _root, count, leaves = _CHILDREN[ctype]
        prop = leaves.get(leaf)
        if prop is None or not 1 <= n <= count or not args:
            return
        value = _reply_value(args)
        if prop in ("mute", "solo"):
            value = _as_bool(value)
        elif prop in ("fader_db", "pan"):
            value = _as_float(value)
        else:
            value = "" if value is None else str(value)
        if value is None:
            return
        if not self.is_child_registered(ctype, n):
            self.register_child(ctype, n)
        self.set_child_state(ctype, n, prop, value)

    def _apply_device(self, read: tuple[str, Any], value: Any) -> None:
        prop, kind = read
        if isinstance(kind, dict):
            word = kind.get(str(value or "").strip().upper())
            if word is None:
                log.debug(f"[{self.device_id}] {prop}: unexpected value {value!r}")
                return
            self.set_state(prop, word)
            return
        if kind is bool:
            converted = _as_bool(value)
        elif kind is int:
            converted = _as_int(value)
        elif kind is float:
            converted = _as_float(value)
        else:
            converted = "" if value is None else str(value)
        if converted is not None:
            self.set_state(prop, converted)
