"""
ETC Paradigm Driver.

Controls ETC Paradigm Architectural Control Processors Mk2 (the P-ACP-E Mk2
enclosure processor and the P-ACP-D Mk2 DIN rail processor) through the
Unison Paradigm Serial Access Protocol (PSAP 6.0.0): one ASCII line per
command, ended by a carriage return by default, over the processor's RS-232
port or over UDP. PSAP is set up per processor in LightDesigner: the RS-232
port answers PSAP out of the box (9600 8N1), while UDP is off until its Input
UDP Port is set (ETC recommends 4703-4727) and its Input Handler is PSAP
6.0.0. The processor answers a UDP message to the port it came from. PSAP
over ACN ("ACN Serial Config") is not reached: the platform has no ACN
transport.

What it reaches
---------------
Everything in PSAP is addressed by its LightDesigner name (case-sensitive,
spaces allowed) and, for most objects, the name of the space that holds it.
PSAP has no request that lists a site's objects, so the integrator lists the
ones to control and watch on the device page: a type, the name, and the
space. Each row becomes a child entity of its type (channel, group, preset,
sequence, space, wall, macro, override) with what PSAP reports about it:
a channel's or group's level, a preset's state (active, inactive, altered,
and its HTP state when asked for), a sequence's, a macro's, a wall's and an
override's. Spaces have no status request; they take commands only.

Every PSAP command in the guide is a command here, addressed to a listed
object. A row's space is sent with every command and status request for it:
a fade time needs one (the guide says so for groups and presets, and the
syntax is the same everywhere), and a status request without one is answered
"error invalid" for an Echo object (P-ACP Mk2 6.1.0 release notes,
PDGM-22773, ETC's workaround). A row with no space learns the one the
processor reports and uses it from then on. Send PSAP Command sends any line,
so a UString set up in LightDesigner can be fired too.

Push vs poll
------------
Hybrid. PSAP answers each "get" with a status line, and a processor whose
LightDesigner configuration has PSAP Triggers sends the same line by itself
whenever that object changes. Over RS-232 (or a serial line through a
bridge) those lines arrive on the connection and are applied like any
answer. Over UDP the guide does not say where a trigger is sent. A trigger
aimed at a fixed address and port set in LightDesigner reaches the Trigger
Port: the driver declares it as ``push: {type: udp_listener}``, so on
OpenAVC 0.40.0 and later the platform listens there and hands over each
datagram the processor's address sends. Such a line is applied like any
status line, but it is never taken as the answer to the request in flight
and does not count as the processor answering on the control link, so a
trigger cannot stand in for the answer the connect check or the liveness
probe asks for. Trigger Port 0 (the default) opens nothing. On a release
without the listener a Trigger Port set is ignored with one warning each
connect. Any line that does reach the driver's own socket is applied as
well. Every listed object is read at connect and then by the poll (at
most MAX_READS_PER_POLL a poll, in turn, so a long list is read across
several polls), and each command reads its object back at once, again once
its fade has run, and the other listed objects in its space after a preset,
space or wall command.

Any line the processor sends that is not a status line (a Send Serial
action's string, for instance) is published as Last Message Received, with a
count that changes on every line so a trigger fires on a repeat.

Requests and replies
--------------------
One status request is in flight at a time. A reply names the object it is
about and is matched to the request by type and name; any other status line
is applied and answers nobody. "error invalid", which carries no name, is
the answer to the request in flight. The guide does not say whether
commands are answered at all; nothing waits for them, and the read-back that
follows is what says whether the name was right.

Liveness
--------
The probe sends "?" (the guide: PSAP returns its list of commands), which
every processor answers however it is set up, and only when nothing has been
heard from the processor for a whole probe interval. Any line counts, an
error included. The connect check asks the same. A status request that draws
nothing is never taken as a dead link by itself: it is addressed to a name
the installer typed, and the guide does not say what a processor answers
for a name it does not have. The poll asks "?" before it gives up on the
link, and otherwise reports the name as not answering.

Why Python (not a YAML .avcdriver)
----------------------------------
1) Objects are addressed by LightDesigner names with spaces and mixed case,
   and a child id is [A-Za-z0-9_-]: a reply names "Lobby On, Main Floor",
   which has to be looked up in the declared rows to reach the child it
   belongs to. A YAML child_set can only use the captured text as the id.
2) One table of rows becomes eight child types, each with its own roster.
3) Optional segments with their own separators (", space", ",fade",
   ":priority") that appear only when the row or the caller supplies them.
4) "error invalid" carries no name, so status requests go one at a time and
   an error is the answer to the one in flight.

Source: ETC, "Unison Paradigm Serial Access Protocol (PSAP) 6.0.0
Configuration Guide", 7180M1250-6.0.0 Rev A (2025-07)
https://www.etcconnect.com/WorkArea/DownloadAsset.aspx?id=10737521406
and the P-ACP Mk2 v6.1.0 Release Notes, 7180M1101-6.1.0 Rev A (2026-06),
https://www.etcconnect.com/WorkArea/DownloadAsset.aspx?id=10737521377
(both read 2026-11-05).
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from openavc.drivers.base import BaseDriver, CommandParamError, ConnectionFaultError
from openavc.transport.frame_parsers import CallableFrameParser
from openavc.utils.logger import get_logger

try:  # OpenAVC 0.37.1+; older releases refuse the same way with RuntimeError.
    from openavc.drivers.base import CommandRefusedError
except ImportError:  # pragma: no cover - depends on the platform release
    CommandRefusedError = RuntimeError  # type: ignore[misc,assignment]

log = get_logger(__name__)


DEFAULT_UDP_PORT = 4703
REQUEST_TIMEOUT_S = 2.0
CONNECT_TIMEOUT_S = 4.0
# A poll reads at most this many status requests, in turn across the list,
# so a long list never fills the link with reads (each one is a round trip).
MAX_READS_PER_POLL = 40
# The longest wait before reading an object again after a fade.
MAX_FADE_READ_BACK_S = 60.0
# The help list ("?") is not published as messages from the processor: its
# lines are held back while it is asked for, and until this long after the
# last of them.
HELP_GAP_S = 0.5

END_OF_MESSAGE = {"cr": b"\r", "crlf": b"\r\n", "lf": b"\n"}

# Row type -> the PSAP object word.
WIRE = {
    "channel": "chan",
    "group": "grp",
    "preset": "pst",
    "sequence": "seq",
    "space": "spc",
    "wall": "wall",
    "macro": "macro",
    "override": "ovr",
}
KIND_OF_WIRE = {v: k for k, v in WIRE.items()}
# Types whose rows carry a space (a space row is the space itself).
HAS_SPACE = frozenset({"channel", "group", "preset", "sequence", "wall"})
# Types PSAP has a status request for.
READABLE = frozenset({"channel", "group", "preset", "sequence", "wall", "macro", "override"})
TYPE_LABEL = {
    "channel": "Channel", "group": "Group", "preset": "Preset",
    "sequence": "Sequence", "space": "Space", "wall": "Wall",
    "macro": "Macro", "override": "Override",
}

PRESET_LTP = {"act": "active", "dact": "inactive", "alt": "altered"}
PRESET_HTP = {"acth": "active", "dacth": "inactive", "alth": "altered"}
SEQUENCE_STATE = {"start": "running", "stop": "stopped", "pause": "paused"}
MACRO_STATE = {"on": "on", "off": "off", "running": "running"}
# The guide's answers are "Wall open" and "Wall close"; "closed" is read too.
WALL_OPEN = {"open": True, "close": False, "closed": False}
OVERRIDE_ENABLED = {"enab": True, "disab": False}

# "chan int:128 Zone 1, Primary Space 1". The guide prints the group answer
# with a comma after the level ("grp int:level, grpname, spacename"), unlike
# the channel one, so the comma is optional. A level followed by % is a
# command echoed back, not a report.
_LEVEL_RE = re.compile(r"^(?i:(chan|grp))\s+(?i:int):(\d+)(?![\d%])\s*,?\s*(.+)$")
# "pst act Lobby On, Main Floor", "Macro on Macro 1", "Wall open Wall 1, Ballroom"
_STATE_RE = re.compile(r"^(?i:(pst|seq|macro|wall|ovr))\s+([A-Za-z]+)\s+(.+)$")
_ERROR_RE = re.compile(r"^(?i:error)\b\s*(.*)$")

STATE_VERBS = {
    "pst": set(PRESET_LTP) | set(PRESET_HTP),
    "seq": set(SEQUENCE_STATE),
    "macro": set(MACRO_STATE),
    "wall": set(WALL_OPEN),
    "ovr": set(OVERRIDE_ENABLED),
}


@dataclass
class Status:
    """One line the processor sent, read."""
    kind: str            # a row type, "error", or "" for anything else
    verb: str = ""       # the state word (act, start, open...), lowercase
    level: int | None = None
    name: str = ""
    space: str = ""
    text: str = ""


def parse_line(text: str) -> Status:
    """Read one PSAP line. Names keep their case; keywords are read in any."""
    text = text.strip()
    m = _LEVEL_RE.match(text)
    if m:
        name, space = _split_target(m.group(3))
        return Status(KIND_OF_WIRE[m.group(1).lower()], level=int(m.group(2)),
                      name=name, space=space, text=text)
    m = _STATE_RE.match(text)
    if m:
        word = m.group(1).lower()
        verb = m.group(2).lower()
        if verb in STATE_VERBS[word]:
            if word in ("macro", "ovr"):
                name, space = m.group(3).strip(), ""
            else:
                name, space = _split_target(m.group(3))
            return Status(KIND_OF_WIRE[word], verb=verb, name=name, space=space, text=text)
    m = _ERROR_RE.match(text)
    if m:
        return Status("error", text=text)
    return Status("", text=text)


def _split_target(rest: str) -> tuple[str, str]:
    """"Zone 1, Primary Space 1" -> ("Zone 1", "Primary Space 1"). The guide
    asks for no commas in object names, so the first comma ends the name."""
    name, _, space = rest.partition(",")
    return name.strip(), space.strip()


def split_psap_lines(buf: bytes) -> tuple[bytes | None, bytes]:
    """Frame a byte stream on CR, LF or CR LF, whichever the processor's End
    of Message Char is set to. Empty lines are dropped."""
    buf = buf.lstrip(b"\r\n")
    for i, byte in enumerate(buf):
        if byte in (0x0D, 0x0A):
            return buf[:i], buf[i + 1:]
    return None, buf


def percent_from_raw(raw: int) -> int:
    """A reported level (always 0-255) as a percentage: 128 is 50%."""
    return round(max(0, min(255, int(raw))) * 100 / 255)


def format_fade(fade: float) -> str:
    """Seconds in tenths, as the guide writes them ("5", "2.5")."""
    fade = round(float(fade), 1)
    return f"{int(fade)}" if fade.is_integer() else f"{fade:.1f}"


def format_address(name: str, space: str = "", fade: float | None = None) -> str:
    """"Preset 1, Primary Space 1,5": the name, ", " and the space, then ","
    and the fade time, the form of the guide's examples. A fade time is read
    by its place after the space, so callers give one only with a space."""
    text = f"{name}, {space}" if space else name
    if fade is not None:
        text += "," + format_fade(fade)
    return text


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text.strip()).strip("_").lower()


@dataclass
class ParadigmObject:
    kind: str
    name: str
    space: str          # from the row; "" when the row has none
    cid: str
    learned_space: str = ""
    found: bool | None = None   # None until the processor has answered for it

    @property
    def effective_space(self) -> str:
        return self.space or self.learned_space


def parse_object_rows(rows: Any) -> tuple[list[ParadigmObject], list[str]]:
    """The Objects table: rows of {type, name, space}. A bad row is reported,
    never fatal."""
    objects: list[ParadigmObject] = []
    problems: list[str] = []
    used: dict[str, set[str]] = {k: set() for k in WIRE}
    seen: set[tuple[str, str, str]] = set()
    if not isinstance(rows, list):
        return objects, problems
    for n, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        raw_type = str(row.get("type") or "").strip().lower()
        kind = raw_type if raw_type in WIRE else KIND_OF_WIRE.get(raw_type, "")
        name = str(row.get("name") or "").strip()
        space = str(row.get("space") or "").strip() if kind in HAS_SPACE else ""
        if not name:
            if raw_type:
                problems.append(f"Row {n} has no name.")
            continue
        if not kind:
            problems.append(f"Row {n} ({name}) has no type.")
            continue
        if "," in name or ":" in name or "," in space or ":" in space:
            problems.append(
                f"{TYPE_LABEL[kind]} \"{name}\": PSAP cannot read a name with a "
                f"comma or a colon in it."
            )
        key = (kind, name, space)
        if key in seen:
            continue
        seen.add(key)
        base = _slug(name) + (f"-{_slug(space)}" if space else "")
        base = base[:120] or kind
        cid = base
        i = 2
        while cid in used[kind]:
            cid = f"{base}_{i}"
            i += 1
        used[kind].add(cid)
        objects.append(ParadigmObject(kind, name, space, cid))
    return objects, problems


class PsapError(Exception):
    """The processor answered a request with an error line."""


@dataclass
class _Request:
    matcher: Callable[[Status], bool] | None   # None: any line answers
    future: asyncio.Future


class EtcParadigmDriver(BaseDriver):
    """ETC Paradigm processor over the Paradigm Serial Access Protocol."""

    DRIVER_INFO = {
        "id": "etc_paradigm",
        "name": "ETC Paradigm",
        "manufacturer": "ETC",
        "category": "lighting",
        "version": "1.1.0",
        # Computed: commands.preset_record.confirm (0.36.0). Checked on the
        # v0.36.0 tag with its simulator: every hook the code calls is there.
        # The push block raises no floor for a Python driver: a release
        # without the UDP listener warns once a connect and polls.
        "min_platform_version": "0.36.0",
        "author": "OpenAVC",
        "description": (
            "Controls ETC Paradigm architectural lighting processors (P-ACP-E Mk2, "
            "P-ACP-D Mk2) through the Paradigm Serial Access Protocol over UDP or "
            "RS-232. List the channels, groups, presets, sequences, spaces, walls, "
            "macros and overrides to control by their LightDesigner names; each "
            "one shows its level or state."
        ),
        "source_url": "https://www.etcconnect.com/WorkArea/DownloadAsset.aspx?id=10737521406",
        "tags": ["lighting", "architectural", "paradigm", "etc", "psap", "presets", "rs-232"],
        "verified": False,
        "simulated": True,
        "protocols": ["etc_psap"],
        "ports": [4703],
        "transport": "udp",
        "transports": ["udp", "serial"],
        # PSAP Triggers sent over UDP to this computer (OpenAVC 0.40.0+), on
        # the Trigger Port; 0 opens nothing (_start_push).
        "push": {"type": "udp_listener", "port": "{trigger_port}"},
        "discovery": {
            # No probe: the processor's UDP input is off until LightDesigner
            # sets a port, and the port is the site's choice (4703-4727
            # recommended). ETC's IEEE MA-L block, the one etc_eos declares
            # too, so an ETC MAC offers both as possible.
            "oui": ["00:c0:16"],
            "manufacturer_alias": ["etc", "electronic theatre controls", "paradigm"],
        },
        "compatible_models": [
            {
                "manufacturer": "ETC",
                "models": ["P-ACP-E Mk2", "P-ACP-D Mk2"],
                "confidence": "untested",
                "notes": (
                    "Paradigm Processor Mk2 (Enclosure and DIN), on Paradigm 6.0.0 or "
                    "later with the PSAP 6.0.0 Input Handler. Status updates arrive on "
                    "their own over RS-232 when the LightDesigner configuration has "
                    "PSAP Triggers; over UDP they are read by polling."
                ),
            },
        ],
        "help": {
            "overview": (
                "Controls an ETC Paradigm processor through PSAP, the Paradigm "
                "Serial Access Protocol. Each channel, group, preset, sequence, "
                "space, wall, macro and override you list under Objects appears "
                "under the device: a channel's or group's level, a preset's state "
                "(active, inactive or altered), a sequence's, a macro's, a wall's "
                "(open or closed) and an override's. Commands set levels, activate "
                "presets, start sequences, turn spaces off, open walls, run "
                "macros and enable overrides. Send PSAP Command sends any line, "
                "including a UString set up in LightDesigner."
            ),
            "setup": (
                "1. In LightDesigner, select the processor and expand UDP Serial "
                "Config. Set Input UDP Port to a port from 4703 to 4727 (4703 here "
                "by default) and Input Handler to PSAP 6.0.0, then upload the "
                "configuration. For RS-232 instead, RS232 Serial Config already "
                "uses PSAP 6.0.0 at 9600 baud, 8 data bits, no parity, 1 stop bit.\n"
                "2. Leave End of Message Char at CR unless the site uses another, "
                "and set the same here.\n"
                "3. In OpenAVC, enter the processor's IP address and the UDP port, "
                "or pick Direct serial and the serial port.\n"
                "4. Under Objects, add a row for each thing to control: its type, "
                "its name exactly as LightDesigner shows it (case included), and "
                "the space that holds it. Fill in the space: a fade time needs "
                "it, and the processor reads a name in that space only. PSAP "
                "cannot read a name with a comma or a colon in it.\n"
                "5. Optional: add PSAP Triggers in LightDesigner (user access "
                "Advanced) for the objects listed, so their changes show at once "
                "instead of at the next poll. Over RS-232 they arrive on the serial "
                "line. Over UDP, ETC's PSAP guide does not say where a trigger is "
                "sent: if LightDesigner lets you send it to an IP address and UDP "
                "port, use this computer's address and a free port, and enter that "
                "port as Trigger Port here (OpenAVC 0.40.0 or later). The poll "
                "still reads every listed object either way."
            ),
        },
        "quick_actions": ["preset_activate", "preset_deactivate", "channel_set_level"],
        "child_entity_types": {
            "channel": {
                "label": "Channel",
                "label_plural": "Channels",
                "id_format": {"type": "string", "max_length": 128},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "space": {"type": "string", "label": "Space"},
                    "level": {
                        "type": "integer", "label": "Level", "min": 0, "max": 100,
                        "step": 1, "unit": "%", "control": True,
                        "cloud_priority": "low",
                        "help": "The channel's intensity, from the processor's 0-255.",
                    },
                    "level_raw": {
                        "type": "integer", "label": "Level (0-255)", "min": 0,
                        "max": 255, "cloud_priority": "low",
                        "help": "The intensity as the processor reports it.",
                    },
                },
                "summary_fields": ["space", "level"],
                "label_field": "name",
            },
            "group": {
                "label": "Group",
                "label_plural": "Groups",
                "id_format": {"type": "string", "max_length": 128},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "space": {"type": "string", "label": "Space"},
                    "level": {
                        "type": "integer", "label": "Level", "min": 0, "max": 100,
                        "step": 1, "unit": "%", "control": True,
                        "cloud_priority": "low",
                        "help": "The group's intensity, from the processor's 0-255.",
                    },
                    "level_raw": {
                        "type": "integer", "label": "Level (0-255)", "min": 0,
                        "max": 255, "cloud_priority": "low",
                        "help": "The intensity as the processor reports it.",
                    },
                },
                "summary_fields": ["space", "level"],
                "label_field": "name",
            },
            "preset": {
                "label": "Preset",
                "label_plural": "Presets",
                "id_format": {"type": "string", "max_length": 128},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "space": {"type": "string", "label": "Space"},
                    "state": {
                        "type": "enum", "label": "State",
                        "values": ["active", "inactive", "altered"],
                        "help": (
                            "Its LTP playback state. Altered: active, and a level "
                            "it set has been changed since."
                        ),
                    },
                    "active": {
                        "type": "boolean", "label": "Active", "control": True,
                        "help": "True while the preset is active (LTP).",
                    },
                    "htp_state": {
                        "type": "enum", "label": "HTP State",
                        "values": ["active", "inactive", "altered"],
                        "help": (
                            "Its HTP playback state, read when Read HTP Preset "
                            "State is on and after an HTP command."
                        ),
                    },
                    "htp_active": {
                        "type": "boolean", "label": "HTP Active",
                        "help": "True while the preset is active (HTP).",
                    },
                },
                "summary_fields": ["space", "state"],
                "label_field": "name",
            },
            "sequence": {
                "label": "Sequence",
                "label_plural": "Sequences",
                "id_format": {"type": "string", "max_length": 128},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "space": {"type": "string", "label": "Space"},
                    "state": {
                        "type": "enum", "label": "State", "control": True,
                        "values": ["running", "stopped", "paused"],
                    },
                },
                "summary_fields": ["space", "state"],
                "label_field": "name",
            },
            "space": {
                "label": "Space",
                "label_plural": "Spaces",
                "id_format": {"type": "string", "max_length": 128},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                },
                "summary_fields": ["name"],
                "label_field": "name",
            },
            "wall": {
                "label": "Wall",
                "label_plural": "Walls",
                "id_format": {"type": "string", "max_length": 128},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "space": {"type": "string", "label": "Space"},
                    "open": {
                        "type": "boolean", "label": "Open", "control": True,
                        "help": "True while the wall is open (its spaces combined).",
                    },
                },
                "summary_fields": ["space", "open"],
                "label_field": "name",
            },
            "macro": {
                "label": "Macro",
                "label_plural": "Macros",
                "id_format": {"type": "string", "max_length": 128},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "state": {
                        "type": "enum", "label": "State",
                        "values": ["on", "off", "running"],
                        "help": "On or off (the steps it ran last), or running.",
                    },
                },
                "summary_fields": ["state"],
                "label_field": "name",
            },
            "override": {
                "label": "Override",
                "label_plural": "Overrides",
                "id_format": {"type": "string", "max_length": 128},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "enabled": {"type": "boolean", "label": "Enabled", "control": True},
                },
                "summary_fields": ["enabled"],
                "label_field": "name",
            },
        },
        "default_config": {
            "host": "",
            "port": 4703,
            "baudrate": 9600,
            "bytesize": 8,
            "parity": "N",
            "stopbits": 1,
            "objects": [],
            "end_of_message": "cr",
            "read_htp": False,
            "trigger_port": 0,
            "poll_interval": 10,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {
                "type": "integer", "default": 4703, "min": 1, "max": 65535,
                "label": "UDP Port",
                "description": (
                    "The processor's Input UDP Port in LightDesigner (UDP Serial "
                    "Config). ETC recommends one from 4703 to 4727."
                ),
            },
            "baudrate": {"type": "integer", "default": 9600, "label": "Baud Rate (serial)"},
            "objects": {
                "type": "table", "label": "Objects", "row_label": "object",
                "columns": {
                    "type": {
                        "type": "enum", "label": "Type", "required": True,
                        "values": [
                            {"value": "channel", "label": "Channel"},
                            {"value": "group", "label": "Group"},
                            {"value": "preset", "label": "Preset"},
                            {"value": "sequence", "label": "Sequence"},
                            {"value": "space", "label": "Space"},
                            {"value": "wall", "label": "Wall"},
                            {"value": "macro", "label": "Macro"},
                            {"value": "override", "label": "Override"},
                        ],
                    },
                    "name": {
                        "type": "string", "label": "Name", "required": True,
                        "help": "Exactly as in LightDesigner, case included.",
                    },
                    "space": {
                        "type": "string", "label": "Space",
                        "help": (
                            "The space that holds it (for a wall, the parent space). "
                            "Not used for a space, a macro or an override."
                        ),
                    },
                },
                "help": (
                    "Each object to control or watch, by its LightDesigner name. "
                    "PSAP cannot list them, so only these are read."
                ),
            },
            "end_of_message": {
                "type": "enum", "default": "cr", "label": "End of Message",
                "values": [
                    {"value": "cr", "label": "CR"},
                    {"value": "crlf", "label": "CR+LF"},
                    {"value": "lf", "label": "LF"},
                ],
                "description": (
                    "The PSAP Input Handler's End of Message Char in LightDesigner. "
                    "CR unless the site changed it."
                ),
                "advanced": True,
            },
            "read_htp": {
                "type": "boolean", "default": False, "label": "Read HTP Preset State",
                "description": (
                    "Also read each preset's HTP playback state. Leave off unless "
                    "the site plays presets HTP."
                ),
            },
            "trigger_port": {
                "type": "integer", "default": 0, "min": 0, "max": 65535,
                "label": "Trigger Port",
                "description": (
                    "The UDP port on this computer that the processor's PSAP "
                    "Triggers are sent to, when LightDesigner sends them over "
                    "UDP. 0: none."
                ),
            },
            "poll_interval": {
                "type": "integer", "default": 10, "min": 0,
                "label": "Status Read Interval (sec)",
                "description": (
                    "How often to read the listed objects. PSAP Triggers report "
                    "changes at once as well, over RS-232 or to the Trigger Port."
                ),
            },
        },
        "state_variables": {
            "objects_declared": {"type": "integer", "label": "Objects Listed"},
            "objects_found": {
                "type": "integer", "label": "Objects Found",
                "help": (
                    "How many listed objects the processor has answered for. "
                    "Spaces are not counted: PSAP has no status request for one."
                ),
            },
            "objects_missing": {
                "type": "integer", "label": "Objects Not Found",
                "help": (
                    "Listed objects the processor answered with an error or did "
                    "not answer for: a name or space that does not match "
                    "LightDesigner."
                ),
            },
            "config_problems": {
                "type": "string", "label": "Object List Problems",
                "help": "Rows of the Objects table that cannot be used, and why.",
            },
            "last_error": {
                "type": "string", "label": "Last Error",
                "help": "The processor's most recent error answer, in words.",
            },
            "last_received": {
                "type": "string", "label": "Last Message Received",
                "help": (
                    "The last line the processor sent that is not a status report, "
                    "such as the string a Send Serial action sends."
                ),
            },
            "received_count": {
                "type": "integer", "label": "Messages Received",
                "help": (
                    "Counts those lines, so a trigger on it fires on every one, the "
                    "same string twice included."
                ),
            },
        },
        "commands": {
            "channel_set_level": {
                "label": "Set Channel Level",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                                "label": "Channel", "help": "One of the channels listed under Objects."},
                    "level": {"type": "integer", "required": True, "label": "Level (%)",
                              "unit": "%", "min": 0, "max": 100},
                    "fade": {"type": "number", "label": "Fade Time (s)", "unit": "s", "min": 0,
                             "decimals": 1,
                             "help": "Seconds, in tenths. Blank: no fade time sent. Needs the channel's space."},
                },
                "sets": {"level": "{level}"},
                "help": "Sets the channel's intensity to the level given.",
            },
            "channel_raise": {
                "label": "Raise Channel",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                                "label": "Channel", "help": "One of the channels listed under Objects."},
                    "amount": {"type": "integer", "required": True, "label": "Amount (%)",
                               "unit": "%", "min": 0, "max": 100, "default": 10},
                    "fade": {"type": "number", "label": "Fade Time (s)", "unit": "s", "min": 0,
                             "decimals": 1,
                             "help": "Seconds, in tenths. Blank: no fade time sent. Needs the channel's space."},
                },
                "help": "Raises the channel's intensity by the amount given.",
            },
            "channel_lower": {
                "label": "Lower Channel",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                                "label": "Channel", "help": "One of the channels listed under Objects."},
                    "amount": {"type": "integer", "required": True, "label": "Amount (%)",
                               "unit": "%", "min": 0, "max": 100, "default": 10},
                    "fade": {"type": "number", "label": "Fade Time (s)", "unit": "s", "min": 0,
                             "decimals": 1,
                             "help": "Seconds, in tenths. Blank: no fade time sent. Needs the channel's space."},
                },
                "help": "Lowers the channel's intensity by the amount given.",
            },
            "channel_toggle": {
                "label": "Toggle Channel",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                                "label": "Channel", "help": "One of the channels listed under Objects."},
                    "fade": {"type": "number", "label": "Fade Time (s)", "unit": "s", "min": 0,
                             "decimals": 1,
                             "help": "Seconds, in tenths. Blank: no fade time sent. Needs the channel's space."},
                },
                "help": "Turns the channel off if it is on, or on if it is off.",
            },
            "channel_set_min": {
                "label": "Set Channel Minimum",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                                "label": "Channel", "help": "One of the channels listed under Objects."},
                    "level": {"type": "integer", "required": True, "label": "Minimum (%)",
                              "unit": "%", "min": 0, "max": 100},
                },
                "help": "Sets the lowest level the channel goes to.",
            },
            "channel_set_max": {
                "label": "Set Channel Maximum",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                                "label": "Channel", "help": "One of the channels listed under Objects."},
                    "level": {"type": "integer", "required": True, "label": "Maximum (%)",
                              "unit": "%", "min": 0, "max": 100},
                },
                "help": "Sets the highest level the channel goes to.",
            },
            "group_set_level": {
                "label": "Set Group Level",
                "params": {
                    "group": {"type": "child_id", "child_type": "group", "required": True,
                              "label": "Group", "help": "One of the groups listed under Objects."},
                    "level": {"type": "integer", "required": True, "label": "Level (%)",
                              "unit": "%", "min": 0, "max": 100},
                    "fade": {"type": "number", "label": "Fade Time (s)", "unit": "s", "min": 0,
                             "decimals": 1,
                             "help": "Seconds, in tenths. Blank: no fade time sent. Needs the group's space."},
                },
                "sets": {"level": "{level}"},
                "help": "Sets the group's intensity to the level given.",
            },
            "group_raise": {
                "label": "Raise Group",
                "params": {
                    "group": {"type": "child_id", "child_type": "group", "required": True,
                              "label": "Group", "help": "One of the groups listed under Objects."},
                    "amount": {"type": "integer", "required": True, "label": "Amount (%)",
                               "unit": "%", "min": 0, "max": 100, "default": 10},
                    "fade": {"type": "number", "label": "Fade Time (s)", "unit": "s", "min": 0,
                             "decimals": 1,
                             "help": "Seconds, in tenths. Blank: no fade time sent. Needs the group's space."},
                },
                "help": "Raises the group's intensity by the amount given.",
            },
            "group_lower": {
                "label": "Lower Group",
                "params": {
                    "group": {"type": "child_id", "child_type": "group", "required": True,
                              "label": "Group", "help": "One of the groups listed under Objects."},
                    "amount": {"type": "integer", "required": True, "label": "Amount (%)",
                               "unit": "%", "min": 0, "max": 100, "default": 10},
                    "fade": {"type": "number", "label": "Fade Time (s)", "unit": "s", "min": 0,
                             "decimals": 1,
                             "help": "Seconds, in tenths. Blank: no fade time sent. Needs the group's space."},
                },
                "help": "Lowers the group's intensity by the amount given.",
            },
            "group_toggle": {
                "label": "Toggle Group",
                "params": {
                    "group": {"type": "child_id", "child_type": "group", "required": True,
                              "label": "Group", "help": "One of the groups listed under Objects."},
                    "fade": {"type": "number", "label": "Fade Time (s)", "unit": "s", "min": 0,
                             "decimals": 1,
                             "help": "Seconds, in tenths. Blank: no fade time sent. Needs the group's space."},
                },
                "help": "Turns the group off if it is on, or on if it is off.",
            },
            "preset_activate": {
                "label": "Activate Preset",
                "params": {
                    "preset": {"type": "child_id", "child_type": "preset", "required": True,
                               "label": "Preset", "help": "One of the presets listed under Objects."},
                    "priority": {"type": "integer", "label": "Priority", "min": 1, "max": 200,
                                 "help": ("1-200, as the installing technician set them up. With a "
                                          "priority the preset stays active beneath higher-priority "
                                          "actions. Blank: none.")},
                    "fade": {"type": "number", "label": "Fade Time (s)", "unit": "s", "min": 0,
                             "decimals": 1,
                             "help": "Seconds, in tenths. Blank: the preset's own fade. Needs the preset's space."},
                },
                "sets": {"active": True},
                "help": "Activates the preset (LTP playback): its lights go to the preset's levels.",
            },
            "preset_deactivate": {
                "label": "Deactivate Preset",
                "params": {
                    "preset": {"type": "child_id", "child_type": "preset", "required": True,
                               "label": "Preset", "help": "One of the presets listed under Objects."},
                    "fade": {"type": "number", "label": "Fade Time (s)", "unit": "s", "min": 0,
                             "decimals": 1,
                             "help": "Seconds, in tenths. Blank: the preset's own fade. Needs the preset's space."},
                },
                "sets": {"active": False},
                "help": "Deactivates the preset (LTP playback): its lights are released.",
            },
            "preset_toggle": {
                "label": "Toggle Preset",
                "params": {
                    "preset": {"type": "child_id", "child_type": "preset", "required": True,
                               "label": "Preset", "help": "One of the presets listed under Objects."},
                    "priority": {"type": "integer", "label": "Priority", "min": 1, "max": 200,
                                 "help": "1-200, as the installing technician set them up. Blank: none."},
                    "fade": {"type": "number", "label": "Fade Time (s)", "unit": "s", "min": 0,
                             "decimals": 1,
                             "help": "Seconds, in tenths. Blank: the preset's own fade. Needs the preset's space."},
                },
                "help": "Deactivates the preset if it is active, or activates it if not (LTP).",
            },
            "preset_activate_htp": {
                "label": "Activate Preset (HTP)",
                "params": {
                    "preset": {"type": "child_id", "child_type": "preset", "required": True,
                               "label": "Preset", "help": "One of the presets listed under Objects."},
                    "priority": {"type": "integer", "label": "Priority", "min": 1, "max": 200,
                                 "help": "1-200, as the installing technician set them up. Blank: none."},
                    "fade": {"type": "number", "label": "Fade Time (s)", "unit": "s", "min": 0,
                             "decimals": 1,
                             "help": "Seconds, in tenths. Blank: the preset's own fade. Needs the preset's space."},
                },
                "sets": {"htp_active": True},
                "help": (
                    "Activates the preset with HTP playback: each light takes the "
                    "higher of its current level and the preset's."
                ),
            },
            "preset_deactivate_htp": {
                "label": "Deactivate Preset (HTP)",
                "params": {
                    "preset": {"type": "child_id", "child_type": "preset", "required": True,
                               "label": "Preset", "help": "One of the presets listed under Objects."},
                    "fade": {"type": "number", "label": "Fade Time (s)", "unit": "s", "min": 0,
                             "decimals": 1,
                             "help": "Seconds, in tenths. Blank: the preset's own fade. Needs the preset's space."},
                },
                "sets": {"htp_active": False},
                "help": "Deactivates the preset's HTP playback.",
            },
            "preset_toggle_htp": {
                "label": "Toggle Preset (HTP)",
                "params": {
                    "preset": {"type": "child_id", "child_type": "preset", "required": True,
                               "label": "Preset", "help": "One of the presets listed under Objects."},
                    "priority": {"type": "integer", "label": "Priority", "min": 1, "max": 200,
                                 "help": "1-200, as the installing technician set them up. Blank: none."},
                    "fade": {"type": "number", "label": "Fade Time (s)", "unit": "s", "min": 0,
                             "decimals": 1,
                             "help": "Seconds, in tenths. Blank: the preset's own fade. Needs the preset's space."},
                },
                "help": "Deactivates the preset's HTP playback if active, or activates it if not.",
            },
            "preset_record": {
                "label": "Record Preset",
                "params": {
                    "preset": {"type": "child_id", "child_type": "preset", "required": True,
                               "label": "Preset", "help": "One of the presets listed under Objects."},
                },
                "help": "Stores the levels its lights are at now in the preset.",
                "confirm": (
                    "Stores the levels the preset's lights are at now in this preset. "
                    "The levels it held before are lost."
                ),
            },
            "sequence_start": {
                "label": "Start Sequence",
                "params": {
                    "sequence": {"type": "child_id", "child_type": "sequence", "required": True,
                                 "label": "Sequence", "help": "One of the sequences listed under Objects."},
                    "priority": {"type": "integer", "label": "Priority", "min": 1, "max": 200,
                                 "help": ("1-200, as the installing technician set them up. With a "
                                          "priority the sequence keeps running beneath higher-priority "
                                          "actions. Blank: none.")},
                },
                "sets": {"state": "running"},
                "help": "Starts the sequence playing from its first step.",
            },
            "sequence_stop": {
                "label": "Stop Sequence",
                "params": {
                    "sequence": {"type": "child_id", "child_type": "sequence", "required": True,
                                 "label": "Sequence", "help": "One of the sequences listed under Objects."},
                },
                "sets": {"state": "stopped"},
                "help": "Stops the sequence.",
            },
            "sequence_pause": {
                "label": "Pause Sequence",
                "params": {
                    "sequence": {"type": "child_id", "child_type": "sequence", "required": True,
                                 "label": "Sequence", "help": "One of the sequences listed under Objects."},
                },
                "sets": {"state": "paused"},
                "help": "Pauses the sequence where it is.",
            },
            "sequence_resume": {
                "label": "Resume Sequence",
                "params": {
                    "sequence": {"type": "child_id", "child_type": "sequence", "required": True,
                                 "label": "Sequence", "help": "One of the sequences listed under Objects."},
                },
                "sets": {"state": "running"},
                "help": "Carries on a paused sequence from where it stopped.",
            },
            "sequence_raise": {
                "label": "Raise Sequence",
                "params": {
                    "sequence": {"type": "child_id", "child_type": "sequence", "required": True,
                                 "label": "Sequence", "help": "One of the sequences listed under Objects."},
                    "amount": {"type": "integer", "required": True, "label": "Amount (%)",
                               "unit": "%", "min": 0, "max": 100, "default": 10},
                },
                "help": "Raises a running sequence's intensity by the amount given.",
            },
            "sequence_lower": {
                "label": "Lower Sequence",
                "params": {
                    "sequence": {"type": "child_id", "child_type": "sequence", "required": True,
                                 "label": "Sequence", "help": "One of the sequences listed under Objects."},
                    "amount": {"type": "integer", "required": True, "label": "Amount (%)",
                               "unit": "%", "min": 0, "max": 100, "default": 10},
                },
                "help": "Lowers a running sequence's intensity by the amount given.",
            },
            "sequence_set_rate": {
                "label": "Set Sequence Rate",
                "params": {
                    "sequence": {"type": "child_id", "child_type": "sequence", "required": True,
                                 "label": "Sequence", "help": "One of the sequences listed under Objects."},
                    "rate": {"type": "number", "required": True, "label": "Rate", "min": 0,
                             "decimals": 2, "default": 1,
                             "help": "1 plays at its programmed timing; 2 twice as fast, 0.5 half."},
                },
                "help": "Changes how fast the sequence plays.",
            },
            "space_off": {
                "label": "Space Off",
                "params": {
                    "space": {"type": "child_id", "child_type": "space", "required": True,
                              "label": "Space", "help": "One of the spaces listed under Objects."},
                    "fade": {"type": "number", "label": "Fade Time (s)", "unit": "s", "min": 0,
                             "decimals": 1, "help": "Seconds, in tenths. Blank: the space's own fade."},
                },
                "help": "Activates the space's Off preset: its lights go out.",
            },
            "space_raise": {
                "label": "Raise Space",
                "params": {
                    "space": {"type": "child_id", "child_type": "space", "required": True,
                              "label": "Space", "help": "One of the spaces listed under Objects."},
                    "amount": {"type": "integer", "required": True, "label": "Amount (%)",
                               "unit": "%", "min": 0, "max": 100, "default": 10},
                    "fade": {"type": "number", "label": "Fade Time (s)", "unit": "s", "min": 0,
                             "decimals": 1, "help": "Seconds, in tenths. Blank: no fade time sent."},
                },
                "help": "Raises every light in the space by the amount given.",
            },
            "space_lower": {
                "label": "Lower Space",
                "params": {
                    "space": {"type": "child_id", "child_type": "space", "required": True,
                              "label": "Space", "help": "One of the spaces listed under Objects."},
                    "amount": {"type": "integer", "required": True, "label": "Amount (%)",
                               "unit": "%", "min": 0, "max": 100, "default": 10},
                    "fade": {"type": "number", "label": "Fade Time (s)", "unit": "s", "min": 0,
                             "decimals": 1, "help": "Seconds, in tenths. Blank: no fade time sent."},
                },
                "help": "Lowers every light in the space by the amount given.",
            },
            "space_set_master": {
                "label": "Set Space Master",
                "params": {
                    "space": {"type": "child_id", "child_type": "space", "required": True,
                              "label": "Space", "help": "One of the spaces listed under Objects."},
                    "level": {"type": "integer", "required": True, "label": "Master (%)",
                              "unit": "%", "min": 0, "max": 100},
                    "fade": {"type": "number", "label": "Fade Time (s)", "unit": "s", "min": 0,
                             "decimals": 1, "help": "Seconds, in tenths. Blank: no fade time sent."},
                },
                "help": "Sets the space's master level, which scales every light in it.",
            },
            "wall_open": {
                "label": "Open Wall",
                "params": {
                    "wall": {"type": "child_id", "child_type": "wall", "required": True,
                             "label": "Wall", "help": "One of the walls listed under Objects."},
                },
                "sets": {"open": True},
                "help": "Opens the wall: the spaces on either side are combined.",
            },
            "wall_close": {
                "label": "Close Wall",
                "params": {
                    "wall": {"type": "child_id", "child_type": "wall", "required": True,
                             "label": "Wall", "help": "One of the walls listed under Objects."},
                },
                "sets": {"open": False},
                "help": "Closes the wall: the spaces on either side are separate again.",
            },
            "wall_toggle": {
                "label": "Toggle Wall",
                "params": {
                    "wall": {"type": "child_id", "child_type": "wall", "required": True,
                             "label": "Wall", "help": "One of the walls listed under Objects."},
                },
                "help": "Closes the wall if it is open, or opens it if it is closed.",
            },
            "macro_on": {
                "label": "Macro On",
                "params": {
                    "macro": {"type": "child_id", "child_type": "macro", "required": True,
                              "label": "Macro", "help": "One of the macros listed under Objects."},
                },
                "help": "Runs the macro's On steps.",
            },
            "macro_off": {
                "label": "Macro Off",
                "params": {
                    "macro": {"type": "child_id", "child_type": "macro", "required": True,
                              "label": "Macro", "help": "One of the macros listed under Objects."},
                },
                "help": "Runs the macro's Off steps.",
            },
            "macro_toggle": {
                "label": "Toggle Macro",
                "params": {
                    "macro": {"type": "child_id", "child_type": "macro", "required": True,
                              "label": "Macro", "help": "One of the macros listed under Objects."},
                },
                "help": "Runs the macro's Off steps if it is on, or its On steps if it is off.",
            },
            "macro_cancel": {
                "label": "Cancel Macro",
                "params": {
                    "macro": {"type": "child_id", "child_type": "macro", "required": True,
                              "label": "Macro", "help": "One of the macros listed under Objects."},
                },
                "help": "Stops a running macro before its remaining steps run.",
            },
            "override_enable": {
                "label": "Enable Override",
                "params": {
                    "override": {"type": "child_id", "child_type": "override", "required": True,
                                 "label": "Override", "help": "One of the overrides listed under Objects."},
                },
                "sets": {"enabled": True},
                "help": "Enables the override: its levels take control of its lights.",
            },
            "override_disable": {
                "label": "Disable Override",
                "params": {
                    "override": {"type": "child_id", "child_type": "override", "required": True,
                                 "label": "Override", "help": "One of the overrides listed under Objects."},
                },
                "sets": {"enabled": False},
                "help": "Disables the override: its lights go back to normal control.",
            },
            "override_toggle": {
                "label": "Toggle Override",
                "params": {
                    "override": {"type": "child_id", "child_type": "override", "required": True,
                                 "label": "Override", "help": "One of the overrides listed under Objects."},
                },
                "help": "Disables the override if it is enabled, or enables it if not.",
            },
            "send_psap": {
                "label": "Send PSAP Command",
                "params": {
                    "command": {"type": "string", "required": True, "label": "Command",
                                "pattern": r"^[^\r\n]{1,500}$",
                                "help": ("One PSAP line without its line ending, such as "
                                         "\"pst act Lobby On, Main Floor\", or a UString.")},
                },
                "help": "Sends one line to the processor as it is typed.",
            },
            "refresh_status": {
                "label": "Read Status Now",
                "params": {},
                "help": "Reads the state of every listed object from the processor now.",
            },
        },
    }

    HEALTH_INTERVAL_S = 30.0
    HEALTH_TIMEOUT_S = 5.0
    HEALTH_FAULT_MESSAGE = "The processor stopped answering PSAP requests."

    def __init__(
        self, device_id: str, config: dict[str, Any], state: Any, events: Any,
    ) -> None:
        super().__init__(device_id, config, state, events)
        self._objects, self._problems = parse_object_rows(self.config.get("objects"))
        self._by_cid: dict[tuple[str, str], ParadigmObject] = {
            (o.kind, o.cid): o for o in self._objects
        }
        self._by_name: dict[tuple[str, str], list[ParadigmObject]] = {}
        for obj in self._objects:
            self._by_name.setdefault((obj.kind, obj.name), []).append(obj)
        self._link = str(self.config.get("transport") or "udp")
        self._lock = asyncio.Lock()
        self._inflight: _Request | None = None
        self._last_rx = 0.0
        self._help_pending = False
        self._help_until = 0.0
        self._poll_cursor = 0
        self._received_count = 0
        self._bg_tasks: set[asyncio.Task] = set()

    # ── Lifecycle ──

    async def _pre_connect(self) -> None:
        self._link = str(self.config.get("transport") or "udp")
        if self._link == "udp" and not str(self.config.get("host", "")).strip():
            raise ConnectionError(f"[{self.device_id}] No host configured")

    def _transport_kwargs(
        self, transport_type: str, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        # The transport actually built: udp, serial, or tcp (a serial line
        # through a bridge, and simulation).
        self._link = transport_type
        return kwargs

    def _create_frame_parser(self) -> CallableFrameParser:
        return CallableFrameParser(split_psap_lines)

    async def _post_connect(self) -> None:
        # A UDP socket opens whether or not anything listens, and a serial
        # port opens with no cable on it: ask before reporting connected.
        try:
            await self._request("?", None, timeout=CONNECT_TIMEOUT_S, help_list=True)
        except TimeoutError as exc:
            raise ConnectionFaultError(self._no_answer_sentence(), code="no_response") from exc

    def _no_answer_sentence(self) -> str:
        if self._link == "udp":
            port = self.config.get("port") or DEFAULT_UDP_PORT
            return (
                f"No answer to a PSAP request on UDP port {port}. The processor's "
                f"UDP Serial Config needs Input UDP Port {port} and Input Handler "
                f"PSAP 6.0.0 (LightDesigner)."
            )
        return (
            "No answer to a PSAP request on the serial line. The processor's "
            "RS232 Serial Config needs Input Handler PSAP 6.0.0 at the same baud "
            "rate (9600 8N1 by default)."
        )

    async def _initial_sync(self) -> None:
        self._register_children()
        for obj in self._objects:
            obj.found = None
        await self._read_many(self._read_list())
        self._report()

    async def _close_session(self) -> None:
        # A request still waiting fails as the dropped link it is, not as a
        # cancellation its caller (a poll) would not expect.
        if self._inflight is not None and not self._inflight.future.done():
            self._inflight.future.set_exception(
                ConnectionError(f"[{self.device_id}] Connection closed"))
        self._inflight = None
        for task in list(self._bg_tasks):
            task.cancel()
        self._bg_tasks.clear()

    def _register_children(self) -> None:
        for obj in self._objects:
            initial: dict[str, Any] = {"name": obj.name}
            if obj.kind in HAS_SPACE:
                initial["space"] = obj.effective_space
            try:
                self.register_child(obj.kind, obj.cid, initial_state=initial)
            except (ValueError, TypeError) as exc:
                log.warning(
                    f"[{self.device_id}] Could not list {obj.kind} {obj.name!r}: {exc}")
        self.set_state("objects_declared", len(self._objects))
        self.set_state("config_problems", " ".join(self._problems))

    async def refresh_children(self) -> dict[str, Any]:
        if self.transport is None or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        self._register_children()
        await self._read_many(self._read_list())
        self._report()
        return {
            "objects": len(self._objects),
            "found": sum(1 for o in self._objects if o.found),
        }

    # ── Wire ──

    def _eom(self) -> bytes:
        return END_OF_MESSAGE.get(str(self.config.get("end_of_message") or "cr"), b"\r")

    async def _write(self, line: str) -> None:
        if self.transport is None or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self.transport.send(line.encode("utf-8") + self._eom())

    async def _request(
        self, line: str, matcher: Callable[[Status], bool] | None, *,
        timeout: float = REQUEST_TIMEOUT_S, help_list: bool = False,
    ) -> Status:
        """Send one line and await the line that answers it: the one
        ``matcher`` accepts, an error line (raised as PsapError), or with no
        matcher any line at all. TimeoutError when nothing answers."""
        async with self._lock:
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._inflight = _Request(matcher, fut)
            self._help_pending = help_list
            try:
                await self._write(line)
                return await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    f"[{self.device_id}] No answer to {line!r} in {timeout:g}s") from exc
            finally:
                self._inflight = None
                self._help_pending = False

    async def _send(self, line: str) -> None:
        """A command: sent behind any request in flight, answered by nothing
        the guide describes."""
        async with self._lock:
            await self._write(line)

    async def on_data_received(self, data: bytes) -> None:
        text = data.decode("utf-8", errors="replace")
        # A UDP datagram is one message and may carry several lines.
        for line in re.split(r"[\r\n]+", text):
            line = line.strip()
            if line:
                self._handle_line(line)

    # ── PSAP Triggers on the Trigger Port (OpenAVC 0.40.0+) ──

    def _trigger_port(self) -> int:
        try:
            return int(self.config.get("trigger_port") or 0)
        except (TypeError, ValueError):
            return 0

    async def _start_push(self) -> None:
        # Trigger Port 0 means none: open nothing and warn about nothing.
        if self._trigger_port() <= 0:
            return
        await super()._start_push()

    async def _handle_push_datagram(
        self, data: bytes, source: tuple[str, int]
    ) -> None:
        text = data.decode("utf-8", errors="replace")
        for line in re.split(r"[\r\n]+", text):
            line = line.strip()
            if line:
                self._handle_line(line, pushed=True)

    def _handle_line(self, line: str, *, pushed: bool = False) -> None:
        # No awaits here: a line is applied before the next one is read.
        # A pushed line (a trigger on the Trigger Port) is applied, but it
        # answers no request and is not the processor answering on the
        # control link: the connect check, the probe and the poll ask that.
        now = time.monotonic()
        if not pushed:
            self._last_rx = now
        status = parse_line(line)
        req = None if pushed else self._inflight
        if status.kind == "error":
            if req is not None and not req.future.done():
                if req.matcher is None:
                    req.future.set_result(status)
                else:
                    req.future.set_exception(PsapError(line))
            else:
                self.set_state("last_error", f"The processor answered \"{line}\".")
            return
        obj = self._find(status) if status.kind else None
        if obj is not None:
            self._apply(obj, status)
        elif not pushed and (self._help_pending or now < self._help_until):
            # A line of the help list (one reads like a status line for no
            # listed object): the next one may follow it.
            self._help_until = now + HELP_GAP_S
        elif not status.kind:
            self._received_count += 1
            self.set_state("last_received", line)
            self.set_state("received_count", self._received_count)
        if req is not None and not req.future.done():
            if req.matcher is None or (status.kind and req.matcher(status)):
                req.future.set_result(status)

    def _find(self, status: Status) -> ParadigmObject | None:
        candidates = self._by_name.get((status.kind, status.name))
        if not candidates:
            return None
        if not status.space:
            return candidates[0]
        for obj in candidates:
            if obj.effective_space == status.space:
                return obj
        for obj in candidates:
            if not obj.space:
                return obj
        return None

    def _apply(self, obj: ParadigmObject, status: Status) -> None:
        update: dict[str, Any] = {}
        if obj.kind in HAS_SPACE and status.space:
            if not obj.space and obj.learned_space != status.space:
                obj.learned_space = status.space
            update["space"] = status.space
        if obj.kind in ("channel", "group") and status.level is not None:
            raw = max(0, min(255, status.level))
            update["level_raw"] = raw
            update["level"] = percent_from_raw(raw)
        elif obj.kind == "preset":
            if status.verb in PRESET_LTP:
                update["state"] = PRESET_LTP[status.verb]
                update["active"] = status.verb == "act"
            elif status.verb in PRESET_HTP:
                update["htp_state"] = PRESET_HTP[status.verb]
                update["htp_active"] = status.verb == "acth"
        elif obj.kind == "sequence" and status.verb in SEQUENCE_STATE:
            update["state"] = SEQUENCE_STATE[status.verb]
        elif obj.kind == "macro" and status.verb in MACRO_STATE:
            update["state"] = MACRO_STATE[status.verb]
        elif obj.kind == "wall" and status.verb in WALL_OPEN:
            update["open"] = WALL_OPEN[status.verb]
        elif obj.kind == "override" and status.verb in OVERRIDE_ENABLED:
            update["enabled"] = OVERRIDE_ENABLED[status.verb]
        if obj.found is not True:
            obj.found = True
            update.update(self.child_fault())
        if update and self.is_child_registered(obj.kind, obj.cid):
            self.set_child_state_batch(obj.kind, obj.cid, update)

    def _mark_missing(self, obj: ParadigmObject, answered: bool) -> None:
        obj.found = False
        sentence = (
            f"The processor has no {obj.kind} \"{obj.name}\""
            + (f" in \"{obj.effective_space}\"" if obj.effective_space else "")
            + ". Check the name and space match LightDesigner, case included."
            if answered else
            "The processor did not answer a status request for this name."
        )
        if self.is_child_registered(obj.kind, obj.cid):
            self.set_child_state_batch(
                obj.kind, obj.cid, self.child_fault("not_responding", sentence))

    # ── Reads ──

    @staticmethod
    def _target(obj: ParadigmObject) -> str:
        """The name with its space, as every command and request sends it."""
        space = obj.effective_space if obj.kind in HAS_SPACE else ""
        return format_address(obj.name, space)

    def _read_list(self) -> list[tuple[ParadigmObject, bool]]:
        htp = bool(self.config.get("read_htp"))
        reads: list[tuple[ParadigmObject, bool]] = []
        for obj in self._objects:
            if obj.kind in READABLE:
                reads.append((obj, False))
                if htp and obj.kind == "preset":
                    reads.append((obj, True))
        return reads

    async def _read(self, obj: ParadigmObject, htp: bool = False) -> bool:
        """Ask for one object's status. False when the processor answered with
        an error; TimeoutError when it answered nothing."""
        word = WIRE[obj.kind]
        verb = "geth" if htp else "get"
        line = f"{word} {verb} {self._target(obj)}"
        wanted = PRESET_HTP if htp else PRESET_LTP

        def matcher(st: Status) -> bool:
            if st.kind != obj.kind or st.name != obj.name:
                return False
            if obj.kind == "preset" and st.verb not in wanted:
                return False
            space = obj.effective_space if obj.kind in HAS_SPACE else ""
            return not (space and st.space and st.space != space)

        try:
            await self._request(line, matcher)
        except PsapError as exc:
            self._mark_missing(obj, answered=True)
            self.set_state(
                "last_error", f"The processor answered \"{exc}\" to \"{line}\".")
            return False
        return True

    async def _read_many(self, reads: list[tuple[ParadigmObject, bool]]) -> None:
        """Read each in turn. A request nothing answers is about a name the
        installer typed, or about a processor that has gone: when nothing at
        all has been heard since it went out, ask "?" (what every processor
        answers, once a batch). No answer to that raises TimeoutError; an
        answer means the name is the one the processor stays silent on."""
        link_known = False
        for obj, htp in reads:
            sent = time.monotonic()
            try:
                await self._read(obj, htp)
            except TimeoutError:
                if not link_known and self._last_rx < sent:
                    await self._request(
                        "?", None, timeout=CONNECT_TIMEOUT_S, help_list=True)
                link_known = True
                self._mark_missing(obj, answered=False)

    async def poll(self) -> None:
        reads = self._read_list()
        if reads:
            start = self._poll_cursor % len(reads)
            batch = (reads[start:] + reads[:start])[:MAX_READS_PER_POLL]
            self._poll_cursor = (start + len(batch)) % len(reads)
            await self._read_many(batch)
        self._report()

    def _report(self) -> None:
        missing = [o for o in self._objects if o.found is False]
        self.set_state("objects_found", sum(1 for o in self._objects if o.found))
        self.set_state("objects_missing", len(missing))
        self.set_state("config_problems", " ".join(self._problems))
        if missing:
            # A standing condition: written again on every poll while it holds.
            names = ", ".join(f"{o.name}" for o in missing[:10])
            more = f" and {len(missing) - 10} more" if len(missing) > 10 else ""
            self.set_state(
                "last_error",
                f"{len(missing)} listed object(s) not found on the processor: "
                f"{names}{more}.",
            )

    async def _liveness_probe(self) -> None:
        if time.monotonic() - self._last_rx < self.HEALTH_INTERVAL_S:
            return  # the processor has said something within the interval
        await self._request(
            "?", None, timeout=self.HEALTH_TIMEOUT_S - 0.5, help_list=True)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _read_later(
        self, delay: float, reads: list[tuple[ParadigmObject, bool]],
    ) -> None:
        try:
            await asyncio.sleep(delay)
            await self._read_many(reads)
        except (TimeoutError, ConnectionError) as exc:
            log.debug(f"[{self.device_id}] Read-back skipped: {exc}")

    def _space_reads(self, space: str, *, skip: ParadigmObject | None = None) -> list[tuple[ParadigmObject, bool]]:
        if not space:
            return []
        return [
            (o, h) for o, h in self._read_list()
            if o is not skip and o.effective_space == space
        ]

    # ── Commands ──

    def _object(self, kind: str, params: dict[str, Any]) -> ParadigmObject:
        cid = str(params.get(kind) or "").strip()
        obj = self._by_cid.get((kind, cid))
        if obj is None:
            raise CommandParamError(
                f"{TYPE_LABEL[kind]} \"{cid}\" is not listed under Objects.")
        return obj

    @staticmethod
    def _percent(value: Any, label: str) -> str:
        try:
            number = int(round(float(value)))
        except (TypeError, ValueError) as exc:
            raise CommandParamError(f"{label} must be a number from 0 to 100.") from exc
        return f"{max(0, min(100, number))}%"

    @staticmethod
    def _fade(params: dict[str, Any]) -> float | None:
        raw = params.get("fade")
        if raw is None or raw == "":
            return None
        try:
            fade = float(raw)
        except (TypeError, ValueError) as exc:
            raise CommandParamError("Fade Time must be a number of seconds.") from exc
        if fade < 0:
            raise CommandParamError("Fade Time cannot be negative.")
        return round(fade, 1)

    def _addressed(self, obj: ParadigmObject, fade: float | None) -> str:
        """The name, its space and a fade time. A fade time is read by its
        place after the space, so it needs one."""
        space = obj.effective_space if obj.kind in HAS_SPACE else ""
        if fade is not None and obj.kind in HAS_SPACE and not space:
            raise CommandParamError(
                f"A fade time needs the space of {obj.kind} \"{obj.name}\": fill in "
                f"its Space under Objects.")
        return format_address(obj.name, space, fade)

    @staticmethod
    def _priority(params: dict[str, Any]) -> str:
        raw = params.get("priority")
        if raw is None or raw == "":
            return ""
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise CommandParamError("Priority must be a whole number from 1 to 200.") from exc
        if not 1 <= value <= 200:
            raise CommandParamError("Priority must be from 1 to 200.")
        return f":{value}"

    async def _command_and_read(
        self, line: str, obj: ParadigmObject, *, htp: bool = False,
        fade: float | None = None, space_too: bool = False,
    ) -> bool:
        await self._send(line)
        if obj.kind in READABLE:
            try:
                ok = await self._read(obj, htp)
            except TimeoutError:
                ok = True  # the command went; the poll reads it later
            if not ok:
                raise CommandRefusedError(
                    f"The processor does not know {obj.kind} \"{obj.name}\""
                    + (f" in \"{obj.effective_space}\"" if obj.effective_space else "")
                    + ". Check the name and space match LightDesigner, case included.")
        later: list[tuple[ParadigmObject, bool]] = []
        if fade and obj.kind in READABLE:
            later.append((obj, htp))
        space = obj.name if obj.kind == "space" else obj.effective_space
        if space_too:
            others = self._space_reads(space, skip=obj)
            if others:
                self._spawn(self._read_later(0.0, others))
            later.extend(others if fade else [])
        if later:
            self._spawn(self._read_later(min(fade or 0.0, MAX_FADE_READ_BACK_S) + 0.5, later))
        return True

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None,
    ) -> Any:
        params = params or {}

        # Channels and groups: int / ras / low / tog, and a channel's min / max.
        for kind, word in (("channel", "chan"), ("group", "grp")):
            prefix = f"{kind}_"
            if not command.startswith(prefix):
                continue
            action = command[len(prefix):]
            obj = self._object(kind, params)
            if action in ("set_level", "raise", "lower"):
                op = {"set_level": "int", "raise": "ras", "lower": "low"}[action]
                key = "level" if action == "set_level" else "amount"
                amount = self._percent(params.get(key), "Level" if key == "level" else "Amount")
                fade = self._fade(params)
                line = f"{word} {op}:{amount} {self._addressed(obj, fade)}"
                return await self._command_and_read(line, obj, fade=fade)
            if action == "toggle":
                fade = self._fade(params)
                return await self._command_and_read(
                    f"{word} tog {self._addressed(obj, fade)}", obj, fade=fade)
            if kind == "channel" and action in ("set_min", "set_max"):
                op = "min" if action == "set_min" else "max"
                level = self._percent(params.get("level"), "Level")
                return await self._command_and_read(
                    f"chan {op}:{level} {self._target(obj)}", obj)

        if command.startswith("preset_"):
            action = command[len("preset_"):]
            obj = self._object("preset", params)
            if action == "record":
                await self._send(f"pst rec {self._target(obj)}")
                return True
            ops = {
                "activate": ("act", True, False), "deactivate": ("dact", False, False),
                "toggle": ("tog", True, False), "activate_htp": ("acth", True, True),
                "deactivate_htp": ("dacth", False, True), "toggle_htp": ("togh", True, True),
            }
            if action in ops:
                op, takes_priority, htp = ops[action]
                prio = self._priority(params) if takes_priority else ""
                fade = self._fade(params)
                line = f"pst {op}{prio} {self._addressed(obj, fade)}"
                return await self._command_and_read(
                    line, obj, htp=htp, fade=fade, space_too=True)

        if command.startswith("sequence_"):
            action = command[len("sequence_"):]
            obj = self._object("sequence", params)
            if action == "start":
                line = f"seq start{self._priority(params)} {self._target(obj)}"
            elif action == "stop":
                line = f"seq stop {self._target(obj)}"
            elif action in ("pause", "resume"):
                # The guide takes no space for these.
                line = f"seq {action} {obj.name}"
            elif action in ("raise", "lower"):
                op = "ras" if action == "raise" else "low"
                line = f"seq {op}:{self._percent(params.get('amount'), 'Amount')} {obj.name}"
            elif action == "set_rate":
                try:
                    rate = float(params.get("rate"))
                except (TypeError, ValueError) as exc:
                    raise CommandParamError("Rate must be a number.") from exc
                if rate <= 0:
                    raise CommandParamError("Rate must be above 0.")
                text = f"{int(rate)}" if rate.is_integer() else f"{rate:g}"
                line = f"seq rate:{text} {obj.name}"
            else:
                line = ""
            if line:
                return await self._command_and_read(line, obj)

        if command.startswith("space_"):
            action = command[len("space_"):]
            obj = self._object("space", params)
            fade = self._fade(params)
            tail = format_address(obj.name, "", fade)
            if action == "off":
                line = f"spc off {tail}"
            elif action in ("raise", "lower"):
                op = "ras" if action == "raise" else "low"
                line = f"spc {op}:{self._percent(params.get('amount'), 'Amount')} {tail}"
            elif action == "set_master":
                line = f"spc master:{self._percent(params.get('level'), 'Master')} {tail}"
            else:
                line = ""
            if line:
                return await self._command_and_read(line, obj, fade=fade, space_too=True)

        if command.startswith("wall_"):
            action = command[len("wall_"):]
            obj = self._object("wall", params)
            op = {"open": "open", "close": "close", "toggle": "tog"}.get(action)
            if op:
                return await self._command_and_read(
                    f"wall {op} {self._target(obj)}", obj, space_too=True)

        if command.startswith("macro_"):
            action = command[len("macro_"):]
            obj = self._object("macro", params)
            op = {"on": "on", "off": "off", "toggle": "tog", "cancel": "cancel"}.get(action)
            if op:
                return await self._command_and_read(f"macro {op} {obj.name}", obj)

        if command.startswith("override_"):
            action = command[len("override_"):]
            obj = self._object("override", params)
            op = {"enable": "enab", "disable": "disab", "toggle": "tog"}.get(action)
            if op:
                return await self._command_and_read(f"ovr {op} {obj.name}", obj)

        if command == "send_psap":
            line = str(params.get("command") or "").strip()
            if not line or "\r" in line or "\n" in line:
                raise CommandParamError("Type one PSAP line, without a line ending.")
            await self._send(line)
            return True

        if command == "refresh_status":
            await self._read_many(self._read_list())
            self._report()
            return True

        log.warning(f"[{self.device_id}] Unknown command: {command}")
        return None
