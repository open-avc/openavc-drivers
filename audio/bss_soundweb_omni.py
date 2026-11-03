"""
BSS Soundweb OMNI Driver.

Controls BSS Soundweb OMNI processors (the 512p and the 256p) over HARMAN's
HControl protocol: one JSON message per line on TCP port 4197, every request
answered by a reply that starts with "@" (``get {...}`` is answered by
``@get {...}``). The protocol has no sign-in. It is not the Soundweb London
Direct Inject protocol (TCP 1023, binary); BLU processors are
`bss_soundweb_london`.

What it reaches
---------------
An OMNI processor is open architecture: its objects and parameters are the
site's design, and the processor's descriptor (the map of every path) is
built from that design. HControl has no request that returns the
descriptor, and the guide says to take each path from AVX Architect, which
copies a control's HControl string from its right-click menu. So the
integrator lists the controls on the device page, one row per path, with
the kind of value it holds. A row can be the path alone (/Device/Input1/Gain)
or the whole string Architect copied; the path and its format are read out
of it.

Rows are grouped into child entities: by the row's Group, else by the
object that holds the parameter (/Device/Input1/Gain and /Device/Input1/Mute
both land in "Input1"). Each control is a state variable on its group.

Kinds and the format each is read and written in:
    number   "number" (a gain in dB, a frequency in Hz, a mute as 0 / 1)
    level    "norm", the parameter's range mapped to 0..1 (a fader position)
    boolean  "number", 1 / 0, stored as a real boolean
    text     "string" (an enum's name, a parameter as its text reads)
    meter    "number", subscribed only while "Follow meters" is on

Push vs poll
------------
Push. Every declared control is subscribed at connect; the processor
answers each subscribe with the current value and then sends a ``publish``
message whenever it changes. The client MUST answer each ``publish`` with
``@publish`` before the processor sends the next one ("Failure to do so will
halt further publish messages", guide p.14), so the reader answers every one
at once, outside the request queue. A ``set`` is answered with the value the
device actually applied (a value above a parameter's maximum comes back as
the maximum, p.8), so that reply is the read-back and goes straight to
state; nothing depends on whether the processor also publishes a change
back to the connection that made it (the guide does not say).

The poll (every 60 s by default) reads every subscribed control again. A
value that changed with no publish means the processor stopped publishing
it, and that control is subscribed again. Meters stream and are not re-read.

Lockstep
--------
"For each request sent, a corresponding reply is received before the next
request is issued" (p.7): one request is in flight at a time, behind a lock.
Replies carry no id, so a reply is matched to the request by its command
word and path. A reply for a path the request did not ask for (one that
arrived after its request timed out) still updates state, and answers no
one.

Liveness
--------
The probe asks /configuration/device/manufacturer, the guide's own example
of a get, which is a device-level question and never a declared path. Any
reply counts, a "Parameter not found" included, and so does any line the
processor sent within the probe interval (a publish, a reply to the poll).

Why Python (not a YAML .avcdriver)
----------------------------------
1) A device-initiated ``publish`` on the control connection must be answered
   ``@publish`` or the processor stops publishing; a YAML response rule
   cannot send anything.
2) Replies carry no id: they are matched to the one request in flight by
   command word and path, and the protocol forbids a second request before
   the first is answered.
3) The control set is the site's design: a declared table of paths and kinds
   becomes dynamic child entities with a schema per group, routed by path.
4) A reply may be an array (a get on an object answers every parameter under
   it) or carry an object value keyed by parameter name (p.14), fanned out
   into many state keys.

Two places the guide disagrees with itself, both handled:
    - p.14 answers a subscribe to /Device/Channel4PEQ/Band_1_Frequency with
      the parent path and a value object ({"Band_1_Frequency": "100Hz"});
      p.15 answers one to /Device/Channel1/Gain with the path itself and a
      plain value. Both forms are read.
    - p.8 says a value beyond a parameter's limit is set to the limit and
      that value returned; p.9 answers the same case with the error "Value
      out of range". A clamp reaches state through the reply; an error is a
      refused command.

Unverified on a unit (no OMNI on the bench): the device-level paths under
/configuration/device and /version come from the guide's example descriptor
(an AMX device's, classes harman.common.*); on a processor without them the
identity values stay empty and the probe still counts the error reply.

Source: HARMAN, "HControl Protocol Integration Guide", version 1.23.2.0
(September 2026), https://bssaudio.com/en-US/site_elements/hcontrol-protocol-integration-guide
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.utils.logger import get_logger

try:  # OpenAVC 0.37.1+; older releases refuse the same way with RuntimeError.
    from openavc.drivers.base import CommandRefusedError
except ImportError:  # pragma: no cover - depends on the platform release
    CommandRefusedError = RuntimeError  # type: ignore[misc,assignment]

log = get_logger(__name__)


DEFAULT_PORT = 4197
LINE_END = b"\n"
REQUEST_TIMEOUT_S = 5.0
DEFAULT_POLL_INTERVAL = 60

# Device-level parameters (guide p.7 and the example descriptor, p.17-18).
MANUFACTURER_PATH = "/configuration/device/manufacturer"
IDENTITY_PATHS = {
    "manufacturer": MANUFACTURER_PATH,
    "model": "/configuration/device/model",
    "device_name": "/configuration/device/name",
    "device_state": "/configuration/device/devicestate",
    "firmware_version": "/version",
}

# Row kind -> the HControl format it is read and written in.
KIND_FORMAT = {
    "number": "number",
    "level": "norm",
    "boolean": "number",
    "text": "string",
    "meter": "number",
}
FORMAT_KIND = {"norm": "level", "string": "text", "number": "number", "variant": "number"}

# Characters a name in a path may not contain (guide p.4), besides "/".
FORBIDDEN_NAME_CHARS = set("$@#^&\"()[]\\:;'<>{}|+=*%!`,?")

# The reserved keys on every group child.
GROUP_SUMMARY_KEYS = ("name", "control_count", "answering")

# Commands a pasted message may not send: the driver owns the subscriptions,
# and a publish acknowledgement only ever answers the processor.
DRIVER_OWNED_COMMANDS = {"subscribe", "unsubscribe", "publish"}

MESSAGE_RE = re.compile(r"^(@?)([A-Za-z_][A-Za-z0-9_]*)\s*(.*)$", re.S)


class HControlError(Exception):
    """An HControl reply carrying an "error" member."""

    def __init__(self, message: str, path: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.path = path


@dataclass
class ControlRow:
    path: str
    fmt: str
    kind: str
    group: str
    name: str
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""


def _safe_id(name: str) -> str:
    """A group name as a child local id (the platform takes ``[A-Za-z0-9_-]``)."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name.strip()) or "group"


def _safe_prop(name: str) -> str:
    """A parameter name as a state key on its group."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name.strip()) or "control"


def _unique(base: str, used: set[str]) -> str:
    candidate = base
    n = 2
    while candidate in used:
        candidate = f"{base}_{n}"
        n += 1
    used.add(candidate)
    return candidate


def path_problem(path: str) -> str:
    """Why a path is not one HControl accepts, or ""."""
    if not path.startswith("/"):
        return "must start with /"
    names = path[1:].split("/")
    if any(not n for n in names):
        return "has an empty name between slashes"
    for n in names:
        bad = sorted(set(n) & FORBIDDEN_NAME_CHARS)
        if bad:
            return f"contains {' '.join(bad)}, which a path name cannot hold"
    return ""


def _optional_number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_path_cell(text: str) -> tuple[str, str]:
    """The path, and the format if one was given, from a Path cell: either a
    bare path or a whole HControl message as AVX Architect copies it
    (``set {"path":"/Device/Input1/Gain","format":"norm","value":0.5}``)."""
    text = (text or "").strip()
    if text.startswith("/"):
        return text, ""
    m = MESSAGE_RE.match(text)
    if m and m.group(3).strip().startswith("{"):
        try:
            body = json.loads(m.group(3))
        except json.JSONDecodeError:
            return text, ""
        if isinstance(body, dict) and isinstance(body.get("path"), str):
            fmt = body.get("format")
            return body["path"].strip(), fmt if isinstance(fmt, str) else ""
    return text, ""


def parse_control_rows(value: Any) -> tuple[list[ControlRow], list[str]]:
    """The Controls table as rows, and a sentence for each row that cannot be
    used."""
    rows: list[ControlRow] = []
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(value if isinstance(value, list) else [], start=1):
        if not isinstance(raw, dict):
            continue
        path, pasted_fmt = read_path_cell(str(raw.get("path") or ""))
        if not path:
            continue
        problem = path_problem(path)
        if problem:
            problems.append(f"Row {index}: the path {path} {problem}.")
            continue
        kind = str(raw.get("type") or "").strip().lower()
        if kind not in KIND_FORMAT:
            kind = FORMAT_KIND.get(pasted_fmt, "number")
        fmt = KIND_FORMAT[kind]
        if (path, fmt) in seen:
            problems.append(f"Row {index}: {path} is listed twice.")
            continue
        seen.add((path, fmt))
        names = path[1:].split("/")
        group = str(raw.get("group") or "").strip()
        if not group:
            group = names[-2] if len(names) > 1 else "Device"
        name = str(raw.get("name") or "").strip() or names[-1]
        rows.append(ControlRow(
            path=path, fmt=fmt, kind=kind, group=group, name=name,
            minimum=_optional_number(raw.get("min")),
            maximum=_optional_number(raw.get("max")),
            unit=str(raw.get("unit") or "").strip(),
        ))
    return rows, problems


def _to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in ("1", "true", "on", "yes"):
        return True
    if text in ("0", "false", "off", "no"):
        return False
    try:
        return float(text) != 0
    except ValueError:
        return None


def _to_number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _wire_number(value: float) -> int | float:
    """A whole number goes out as an integer (the guide writes a mute as 0)."""
    return int(value) if float(value).is_integer() else value


class BssSoundwebOmniDriver(BaseDriver):
    """BSS Soundweb OMNI processor over HARMAN HControl."""

    DRIVER_INFO = {
        "id": "bss_soundweb_omni",
        "name": "BSS Soundweb OMNI",
        "manufacturer": "BSS Audio",
        "category": "audio",
        "version": "1.0.0",
        # Set by hand (the contract check cannot see a method call):
        # BaseDriver.child_fault, 0.29.0.
        "min_platform_version": "0.29.0",
        "author": "OpenAVC",
        "description": (
            "Controls BSS Soundweb OMNI processors (512p, 256p) over HARMAN's "
            "HControl protocol on TCP port 4197. List the controls you need "
            "from the AVX Architect design, by their HControl paths; each "
            "object becomes a child with its controls, updated the moment they "
            "change on the processor."
        ),
        "source_url": "https://bssaudio.com/en-US/site_elements/hcontrol-protocol-integration-guide",
        "tags": ["dsp", "bss", "soundweb", "omni", "hcontrol", "harman", "dante"],
        "verified": False,
        "simulated": True,
        "protocols": ["hcontrol"],
        "ports": [4197],
        "transport": "tcp",
        "discovery": {
            # Any HControl endpoint answers a get of its manufacturer, with
            # the value or with "Parameter not found". That reply proves the
            # HARMAN protocol, not BSS: cross_vendor lets a driver for
            # another HControl product with its own fingerprint win, and the
            # manufacturer is read out of the reply rather than assumed.
            "tcp_probe": {
                "port": 4197,
                "send_ascii": (
                    'get {"format":"string","path":"/configuration/device/manufacturer"}\n'
                ),
                "expect_regex": r'^@get \{.*"path"\s*:\s*"/configuration/device/manufacturer"',
                "cross_vendor": True,
                "timeout_ms": 2000,
                "extract": {
                    "manufacturer": {"regex": r'"value"\s*:\s*"([^"]+)"', "group": 1},
                },
            },
            "port_open": [4197],
            "manufacturer_alias": ["bss", "bss audio", "soundweb omni"],
        },
        "compatible_models": [
            {
                "manufacturer": "BSS Audio",
                "models": ["Soundweb OMNI 512p", "Soundweb OMNI 256p"],
                "confidence": "untested",
                "notes": (
                    "Built from HARMAN's HControl Protocol Integration Guide; "
                    "not yet run on a processor. Not the OMNI 32e / 16e I/O "
                    "expanders: no document gives their HControl paths."
                ),
            },
        ],
        "help": {
            "overview": (
                "Controls a Soundweb OMNI processor over HControl. Each control "
                "you list appears under the device, grouped by the object that "
                "holds it, and updates the moment it changes on the processor, "
                "from AVX, a wall panel or a preset. Set Control, Toggle Control "
                "and Step Control change one; Run Device Command and Send "
                "HControl Message reach anything else in the design."
            ),
            "setup": (
                "1. In AVX Architect, open the design's control panel, right-click "
                "each control you need and copy its HControl string.\n"
                "2. In OpenAVC, enter the processor's HControl IP address (shown "
                "on its front panel under HControl). The port is 4197.\n"
                "3. Paste each string, or just its path, into a row of the "
                "Controls table and pick what it holds: Number (a gain in dB, a "
                "frequency), Level (a fader position from 0 to 1), On/Off (a "
                "mute), Text (a source selector's choice, a name) or Meter.\n"
                "Controls with the same object land in one group, named after "
                "the object; type a Group to choose your own. After a change to "
                "the design, use Refresh on the device's Groups tab."
            ),
            "connection": (
                "HControl needs no login. If the processor closes the "
                "connection, it may already have as many control connections as "
                "it allows, or its security settings may block this computer."
            ),
        },
        "child_entity_types": {
            "group": {
                "label": "Group",
                "label_plural": "Groups",
                "dynamic": True,
                "id_format": {"type": "string", "max_length": 128},
                # Each group's controls are its own schema, published at
                # register_child(schema=...).
                "state_variables": {
                    "name": {"type": "string", "label": "Group"},
                    "control_count": {"type": "integer", "label": "Controls"},
                    "answering": {
                        "type": "integer", "label": "Controls Found",
                        "help": "How many of the group's controls the processor's design has.",
                    },
                },
                "summary_fields": ["name", "control_count", "answering"],
                "label_field": "name",
            },
        },
        "default_config": {
            "host": "",
            "port": 4197,
            "controls": [],
            "follow_meters": False,
            "poll_interval": 60,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {
                "type": "integer", "default": 4197, "min": 1,
                "max": 65535, "label": "Port",
                "description": "The processor's HControl port: 4197.",
            },
            "controls": {
                "type": "table", "label": "Controls", "row_label": "control",
                "columns": {
                    "path": {
                        "type": "string", "label": "Path", "required": True,
                        "help": (
                            "The control's HControl path (/Device/Input1/Gain), or "
                            "the whole HControl string copied from AVX Architect."
                        ),
                    },
                    "type": {
                        "type": "enum", "label": "Holds",
                        "values": [
                            {"value": "number", "label": "Number"},
                            {"value": "level", "label": "Level (0 to 1)"},
                            {"value": "boolean", "label": "On/Off"},
                            {"value": "text", "label": "Text"},
                            {"value": "meter", "label": "Meter"},
                        ],
                        "help": (
                            "Blank: Number, or what the pasted string's format "
                            "says (norm is Level, string is Text)."
                        ),
                    },
                    "group": {
                        "type": "string", "label": "Group",
                        "help": "Blank: the object that holds the control (Input1).",
                    },
                    "name": {
                        "type": "string", "label": "Name",
                        "help": "Blank: the parameter's own name (Gain).",
                    },
                    "min": {"type": "number", "label": "Min", "help": "Number only, for panel sliders."},
                    "max": {"type": "number", "label": "Max", "help": "Number only, for panel sliders."},
                    "unit": {"type": "string", "label": "Unit", "help": "Number only (dB, Hz)."},
                },
                "help": (
                    "Each control to watch or change. A value that changes all "
                    "the time (a meter) is sent as fast as it changes, so list "
                    "meters only where a panel shows them."
                ),
            },
            "follow_meters": {
                "type": "boolean", "default": False, "label": "Follow meters",
                "help": "Off: rows that hold a Meter are not read.",
            },
            "poll_interval": {
                "type": "integer", "default": 60, "min": 0,
                "max": 3600, "label": "Re-read interval (s)", "advanced": True,
                "help": (
                    "How often every control is read again. Changes arrive as "
                    "they happen in between. 0 turns it off."
                ),
            },
        },
        "state_variables": {
            "manufacturer": {"type": "string", "label": "Manufacturer"},
            "model": {"type": "string", "label": "Model"},
            "device_name": {"type": "string", "label": "Device Name"},
            "device_state": {"type": "string", "label": "Device State"},
            "firmware_version": {"type": "string", "label": "Version"},
            # These three describe the Controls table and what the processor
            # has answered on this connection.
            "controls_declared": {
                "type": "integer", "label": "Controls Listed", "min": 0,
                "help": "How many usable rows the Controls table has.",
            },
            "controls_answering": {
                "type": "integer", "label": "Controls Found", "min": 0,
                "help": "How many of them the processor's design has.",
            },
            "config_problems": {
                "type": "string", "label": "Controls Table Problems",
                "help": "Rows that cannot be used, and why. Empty when every row is usable.",
            },
            "last_error": {
                "type": "string", "label": "Last Error",
                "help": "The processor's most recent refusal, in words.",
            },
        },
        "commands": {
            "set_control": {
                "label": "Set Control",
                "params": {
                    "group": {
                        "type": "child_id", "child_type": "group",
                        "required": True, "label": "Group",
                    },
                    "control": {
                        "type": "string", "required": True, "label": "Control",
                        "options_from": {"param": "group", "source": "child_schema"},
                    },
                    "value": {
                        "type": "string", "required": True, "label": "Value",
                        "type_from": {"param": "control"},
                        "help": (
                            "A number for a Number control (dB for a gain), 0 to "
                            "1 for a Level, on or off for an On/Off, the text for "
                            "a Text control."
                        ),
                    },
                },
                "help": "Sets one control on the processor, as if moved in AVX.",
            },
            "toggle_control": {
                "label": "Toggle Control",
                "params": {
                    "group": {
                        "type": "child_id", "child_type": "group",
                        "required": True, "label": "Group",
                    },
                    "control": {
                        "type": "string", "required": True, "label": "Control",
                        "options_from": {
                            "param": "group", "source": "child_schema",
                            "types": ["boolean"],
                        },
                    },
                },
                "help": "Turns an On/Off control (a mute) the other way.",
            },
            "step_control": {
                "label": "Step Control",
                "params": {
                    "group": {
                        "type": "child_id", "child_type": "group",
                        "required": True, "label": "Group",
                    },
                    "control": {
                        "type": "string", "required": True, "label": "Control",
                        "options_from": {
                            "param": "group", "source": "child_schema",
                            "types": ["number"],
                        },
                    },
                    "by": {
                        "type": "number", "required": True, "label": "Change By",
                        "help": "Added to the control's value; negative lowers it (-3 for a 3 dB step).",
                    },
                },
                "help": "Raises or lowers a Number or Level control by the amount given.",
            },
            "exec_command": {
                "label": "Run Device Command",
                "params": {
                    "path": {
                        "type": "string", "required": True, "label": "Path",
                        "help": "The object that defines the command (/configuration/commands).",
                    },
                    "command": {
                        "type": "string", "required": True, "label": "Command",
                    },
                    "arguments": {
                        "type": "string", "required": False, "label": "Arguments",
                        "help": 'The command\'s arguments as JSON ({"level":"Knobs"}). Blank sends none.',
                    },
                },
                "help": "Runs a command the processor defines in its design (HControl exec).",
            },
            "send_message": {
                "label": "Send HControl Message",
                "params": {
                    "message": {
                        "type": "string", "required": True, "label": "Message",
                        "help": (
                            "One HControl message as AVX Architect copies it: "
                            'set {"path":"/Device/Input1/Mute","format":"number","value":1}'
                        ),
                    },
                },
                "help": (
                    "Sends a get, set or exec message exactly as written and shows "
                    "the processor's reply."
                ),
            },
            "refresh_controls": {
                "label": "Read Controls Again",
                "params": {},
                "help": "Reads and follows every listed control again, after a change to the design.",
            },
        },
        "quick_actions": ["set_control", "toggle_control", "step_control"],
    }

    HEALTH_INTERVAL_S = 20.0
    HEALTH_FAULT_MESSAGE = "The processor stopped answering HControl requests."

    def __init__(
        self, device_id: str, config: dict[str, Any], state: Any, events: Any,
    ) -> None:
        super().__init__(device_id, config, state, events)
        # One request in flight at a time: (command word, path, future).
        self._request_lock = asyncio.Lock()
        self._inflight: tuple[str, str | None, asyncio.Future[Any]] | None = None

        # The declared controls.
        #   _route:        (path, format) -> (group child id, state key, row)
        #   _group_name:   group child id -> its name
        #   _group_schema: group child id -> the schema it was registered with
        #   _group_props:  group child id -> {state key: (path, format)}
        self._route: dict[tuple[str, str], tuple[str, str, ControlRow]] = {}
        self._group_name: dict[str, str] = {}
        self._group_schema: dict[str, dict[str, Any]] = {}
        self._group_props: dict[str, dict[str, tuple[str, str]]] = {}
        # Controls the processor answered for, and those subscribed, on this
        # connection.
        self._found: set[tuple[str, str]] = set()
        self._subscribed: set[tuple[str, str]] = set()
        self._missing: dict[tuple[str, str], str] = {}
        self._row_problems: list[str] = []
        self._standing_error = ""
        # The control the poll is reading again, and whether the processor's
        # answer differed from state (a change that was never published).
        self._drift_key: tuple[str, str] | None = None
        self._drifted = False
        # Event-loop time of the last line the processor sent.
        self._last_heard = float("-inf")

    # ── Lifecycle ──

    async def _pre_connect(self) -> None:
        if not str(self.config.get("host", "")).strip():
            raise ConnectionError(f"[{self.device_id}] No host configured")

    def _transport_kwargs(
        self, transport_type: str, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        kwargs["delimiter"] = LINE_END
        return kwargs

    async def _post_connect(self) -> None:
        # A port that opens proves only that something listens on it. Ask the
        # one question every HControl device answers before reporting
        # connected; a refusal is still the protocol answering.
        try:
            await self._request("get", {"format": "string", "path": MANUFACTURER_PATH})
        except HControlError:
            pass
        except TimeoutError as exc:
            raise ConnectionFaultError(
                "The device accepted the connection on its HControl port but "
                "did not answer an HControl request.",
                code="no_response",
            ) from exc

    async def _initial_sync(self) -> None:
        await self._read_identity()
        await self._import_controls(resubscribe=False)

    async def _close_session(self) -> None:
        if self._inflight is not None:
            _word, _path, fut = self._inflight
            if not fut.done():
                fut.cancel()
            self._inflight = None
        self._found.clear()
        self._subscribed.clear()
        self._missing.clear()

    # ── HControl plumbing ──

    async def _send_line(self, line: str) -> None:
        if self.transport is None or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self.transport.send(line.encode("utf-8") + LINE_END)

    async def _request(
        self, word: str, body: Any = None, *, timeout: float = REQUEST_TIMEOUT_S,
    ) -> Any:
        """Send one request and await its reply. Returns the reply body (a
        dict, a list, or None for a bare reply). Raises HControlError on an
        error reply, TimeoutError on none, ConnectionError when down."""
        path = body.get("path") if isinstance(body, dict) else None
        line = word if body is None else (
            f"{word} {json.dumps(body, ensure_ascii=False, separators=(',', ':'))}"
        )
        async with self._request_lock:
            fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            self._inflight = (word, path if isinstance(path, str) else None, fut)
            try:
                await self._send_line(line)
                return await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    f"[{self.device_id}] {word} {path or ''} got no reply in {timeout:g}s"
                ) from exc
            finally:
                self._inflight = None

    async def on_data_received(self, data: bytes) -> None:
        self._last_heard = asyncio.get_running_loop().time()
        text = data.decode("utf-8", errors="replace").strip()
        if not text:
            return
        m = MESSAGE_RE.match(text)
        if m is None:
            # Not HControl. The guide says a refused connection gets "an error
            # message" over the connection, in a form it does not give.
            log.warning(f"[{self.device_id}] Not an HControl message: {text[:200]!r}")
            self.set_state("last_error", f"The processor said: {text[:200]}")
            return
        is_reply, word, rest = m.group(1) == "@", m.group(2), m.group(3).strip()
        body: Any = None
        if rest:
            try:
                body = json.loads(rest)
            except json.JSONDecodeError:
                log.warning(f"[{self.device_id}] Unreadable HControl body: {text[:200]!r}")
                body = None
                if not is_reply:
                    await self._acknowledge(word)
                return

        if not is_reply:
            # A message the processor started. Answer it first: the processor
            # sends no further publish until this one is acknowledged.
            await self._acknowledge(word)
            if word == "publish":
                self._apply(body)
            else:
                log.info(f"[{self.device_id}] Processor sent {word!r}: {text[:200]}")
            return

        error = self._error_of(body)
        inflight = self._inflight
        claimed = (
            inflight is not None and inflight[0] == word
            and self._same_target(inflight[1], body)
        )
        if error is None:
            if claimed and word == "get" and self._drift_key is not None:
                # Weighed against state as it stands when the reply arrives,
                # after every publish and set reply that came before it.
                self._drifted = self._differs(self._drift_key, body)
            self._apply(body)
        if not claimed:
            if error is not None:
                log.warning(f"[{self.device_id}] Unclaimed refusal: {error.message}")
            return
        fut = inflight[2]
        if not fut.done():
            if error is not None:
                fut.set_exception(error)
            else:
                fut.set_result(body)

    async def _acknowledge(self, word: str) -> None:
        try:
            await self._send_line(f"@{word}")
        except ConnectionError:
            pass  # the link is gone; the reconnect subscribes again

    @staticmethod
    def _error_of(body: Any) -> HControlError | None:
        if isinstance(body, dict) and body.get("error") is not None:
            return HControlError(str(body.get("error")), str(body.get("path") or ""))
        if isinstance(body, list):
            for item in body:
                if isinstance(item, dict) and item.get("error") is not None:
                    return HControlError(str(item.get("error")), str(item.get("path") or ""))
        return None

    @staticmethod
    def _same_target(asked: str | None, body: Any) -> bool:
        """A reply belongs to the request in flight when it names the same
        path, the object holding it (p.14's subscribe answer), or something
        under it (a get on an object)."""
        if asked is None or not isinstance(body, dict):
            return True
        said = body.get("path")
        if not isinstance(said, str) or not said:
            return True
        return said == asked or asked.startswith(said + "/") or said.startswith(asked + "/")

    # ── Values into state ──

    def _apply(self, body: Any) -> None:
        items = body if isinstance(body, list) else [body]
        batches: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict) or item.get("error") is not None:
                continue
            path = item.get("path")
            if not isinstance(path, str) or "value" not in item:
                continue
            fmt = str(item.get("format") or "variant")
            value = item.get("value")
            if isinstance(value, dict):
                # p.14: the parent object's path, its parameters by name.
                for name, inner in value.items():
                    self._apply_one(f"{path.rstrip('/')}/{name}", fmt, inner, batches)
            else:
                self._apply_one(path, fmt, value, batches)
        for sid, batch in batches.items():
            if batch and self.is_child_registered("group", sid):
                self.set_child_state_batch("group", sid, batch)

    def _apply_one(
        self, path: str, fmt: str, value: Any, batches: dict[str, dict[str, Any]],
    ) -> None:
        if fmt == "string":
            for key, ident_path in IDENTITY_PATHS.items():
                if ident_path == path:
                    self.set_state(key, "" if value is None else str(value))
        key = (path, fmt)
        route = self._route.get(key)
        if route is None and fmt == "variant":
            # A get on an object answers in variant unless asked otherwise:
            # take it for the one row on that path, if there is only one.
            matches = [k for k in self._route if k[0] == path]
            if len(matches) == 1:
                key = matches[0]
                route = self._route[key]
        if route is None:
            return
        sid, prop, row = route
        converted = self._from_wire(row, value)
        if converted is None:
            log.warning(f"[{self.device_id}] {path}: unreadable value {value!r}")
            return
        batches.setdefault(sid, {})[prop] = converted
        if key not in self._found:
            self._found.add(key)
            self._missing.pop(key, None)

    @staticmethod
    def _from_wire(row: ControlRow, value: Any) -> Any:
        if row.kind == "boolean":
            return _to_bool(value)
        if row.kind == "text":
            return "" if value is None else str(value)
        return _to_number(value)

    # ── Reads ──

    async def _read_identity(self) -> None:
        for key, path in IDENTITY_PATHS.items():
            try:
                await self._request("get", {"format": "string", "path": path})
            except HControlError:
                self.set_state(key, "")

    def _meters_on(self) -> bool:
        return bool(self.config.get("follow_meters"))

    async def _import_controls(self, *, resubscribe: bool) -> None:
        """Group the declared rows into child entities and subscribe each
        control."""
        rows, problems = parse_control_rows(self.config.get("controls"))
        self._row_problems = problems
        self._register_groups(rows)

        if resubscribe and self._subscribed:
            # End every subscription on this connection before starting
            # again, so nothing is followed twice.
            try:
                await self._request("unsubscribe")
            except HControlError as exc:
                log.warning(f"[{self.device_id}] Unsubscribe refused: {exc.message}")
        self._subscribed.clear()
        self._found.clear()
        self._missing.clear()

        for key, (_sid, _prop, row) in list(self._route.items()):
            if row.kind == "meter" and not self._meters_on():
                continue
            await self._subscribe(key)
        self._report()

    async def _subscribe(self, key: tuple[str, str]) -> None:
        path, fmt = key
        try:
            await self._request("subscribe", {"path": path, "format": fmt})
        except HControlError as exc:
            self._missing[key] = exc.message
            return
        self._subscribed.add(key)

    def _register_groups(self, rows: list[ControlRow]) -> None:
        self._route.clear()
        grouped: dict[str, list[ControlRow]] = {}
        for row in rows:
            grouped.setdefault(row.group, []).append(row)

        used_ids: set[str] = set()
        seen: set[str] = set()
        for group, members in grouped.items():
            sid = _unique(_safe_id(group), used_ids)
            seen.add(sid)
            schema: dict[str, dict[str, Any]] = {
                "name": {"type": "string", "label": "Group"},
                "control_count": {"type": "integer", "label": "Controls"},
                "answering": {"type": "integer", "label": "Controls Found"},
            }
            used = set(GROUP_SUMMARY_KEYS)
            props: dict[str, tuple[str, str]] = {}
            for row in members:
                prop = _unique(_safe_prop(row.name), used)
                schema[prop] = self._var_def(row)
                props[prop] = (row.path, row.fmt)
                self._route[(row.path, row.fmt)] = (sid, prop, row)
            initial = {"name": group, "control_count": len(members), "answering": 0}
            if self.is_child_registered("group", sid) and self._group_schema.get(sid) != schema:
                self.deregister_child("group", sid)
            if self.is_child_registered("group", sid):
                self.set_child_state_batch("group", sid, initial)
            else:
                self.register_child("group", sid, initial_state=initial, schema=schema)
            self._group_name[sid] = group
            self._group_schema[sid] = schema
            self._group_props[sid] = props

        for sid in [s for s in self._group_name if s not in seen]:
            self.deregister_child("group", sid)
            self._group_name.pop(sid, None)
            self._group_schema.pop(sid, None)
            self._group_props.pop(sid, None)

    @staticmethod
    def _var_def(row: ControlRow) -> dict[str, Any]:
        label = row.name
        if row.kind == "boolean":
            return {"type": "boolean", "label": label, "control": True}
        if row.kind == "text":
            return {"type": "string", "label": label, "control": True}
        if row.kind == "level":
            return {
                "type": "number", "label": label, "control": True,
                "min": 0, "max": 1, "step": 0.01,
            }
        var: dict[str, Any] = {"type": "number", "label": label}
        if row.kind == "meter":
            var.update({"control": False, "cloud_priority": "low"})
        else:
            var["control"] = True
        if row.minimum is not None:
            var["min"] = row.minimum
        if row.maximum is not None:
            var["max"] = row.maximum
        if row.unit:
            var["unit"] = row.unit
        return var

    def _report(self) -> None:
        """Counts per group, a fault on a group the design has none of, and
        the standing sentence for what is wrong."""
        followed = {
            key for key, (_s, _p, row) in self._route.items()
            if row.kind != "meter" or self._meters_on()
        }
        for sid, props in self._group_props.items():
            keys = [k for k in props.values() if k in followed]
            found = sum(1 for k in keys if k in self._found)
            update: dict[str, Any] = {"answering": found}
            if keys and found == 0 and all(k in self._missing for k in keys):
                update.update(self.child_fault(
                    "not_responding",
                    "None of this group's controls is in the processor's design.",
                ))
            else:
                update.update(self.child_fault())
            if self.is_child_registered("group", sid):
                self.set_child_state_batch("group", sid, update)
        self.set_state("controls_declared", len(self._route))
        self.set_state("controls_answering", len(self._found))
        self.set_state("config_problems", " ".join(self._row_problems))

        sentences = list(self._row_problems)
        if self._missing:
            paths = sorted(path for path, _fmt in self._missing)
            shown = ", ".join(paths[:5]) + (f" and {len(paths) - 5} more" if len(paths) > 5 else "")
            sentences.append(
                f"The processor's design has no control at {shown}."
            )
        self._standing_error = " ".join(sentences)
        if self._standing_error:
            self.set_state("last_error", self._standing_error)

    # ── Poll, liveness, refresh ──

    async def poll(self) -> None:
        """Read every followed control again; subscribe again any whose value
        changed with no publish, and any the design did not have before."""
        for key, (sid, prop, row) in list(self._route.items()):
            if row.kind == "meter":
                continue
            if key in self._missing:
                await self._subscribe(key)
                continue
            if key not in self._subscribed:
                continue
            path, fmt = key
            self._drift_key, self._drifted = key, False
            try:
                await self._request("get", {"path": path, "format": fmt})
            except HControlError as exc:
                # The design changed under the connection.
                self._subscribed.discard(key)
                self._found.discard(key)
                self._missing[key] = exc.message
                continue
            finally:
                self._drift_key = None
            if self._drifted:
                log.info(
                    f"[{self.device_id}] {path} changed with no publish; "
                    f"following it again")
                try:
                    await self._request("unsubscribe", {"path": path})
                except HControlError:
                    pass
                self._subscribed.discard(key)
                await self._subscribe(key)
        self._report()

    def _differs(self, key: tuple[str, str], body: Any) -> bool:
        route = self._route.get(key)
        if route is None or not isinstance(body, dict) or body.get("path") != key[0]:
            return False
        sid, prop, row = route
        current = self.get_child_state("group", sid).get(prop)
        value = self._from_wire(row, body.get("value"))
        return current is not None and value is not None and value != current

    async def _liveness_probe(self) -> None:
        # Anything the processor sent within the interval is an answer. The
        # question would otherwise queue behind a long re-read of a large
        # design and could miss its timeout while the processor answers.
        loop = asyncio.get_running_loop()
        if loop.time() - self._last_heard < self.HEALTH_INTERVAL_S:
            return
        try:
            await self._request("get", {"format": "string", "path": MANUFACTURER_PATH})
        except HControlError:
            return  # a refusal is an answer

    async def refresh_children(self) -> dict[str, Any]:
        if self.transport is None or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self._import_controls(resubscribe=True)
        return {"groups": len(self._group_name), "controls": len(self._route)}

    # ── Commands ──

    def _target(self, params: dict[str, Any]) -> tuple[str, str, ControlRow]:
        """(group id, state key, row) from a group child id and a control's
        state key, either of which may also be typed by name."""
        sid = str(params.get("group") or "").strip()
        if sid not in self._group_props:
            for candidate, name in self._group_name.items():
                if name == sid:
                    sid = candidate
                    break
        props = self._group_props.get(sid)
        if props is None:
            raise ValueError(f'No group "{params.get("group")}" is listed on this device.')
        prop = str(params.get("control") or "").strip()
        if prop not in props:
            for candidate, key in props.items():
                if self._route[key][2].name == prop:
                    prop = candidate
                    break
        key = props.get(prop)
        if key is None:
            raise ValueError(f'Group "{self._group_name[sid]}" has no control "{prop}".')
        return sid, prop, self._route[key][2]

    def _refusal(self, exc: HControlError, row_or_path: ControlRow | str) -> CommandRefusedError:
        path = row_or_path.path if isinstance(row_or_path, ControlRow) else row_or_path
        reason = exc.message.strip()
        if not path:
            sentence = f"The processor refused it: {reason}."
        elif reason.lower() == "parameter not found":
            sentence = f"The processor's design has no control at {path}."
        elif reason.lower() == "value out of range":
            sentence = f"The processor refused the value for {path}: out of range."
        else:
            sentence = f"The processor refused it: {reason}."
        self.set_state("last_error", sentence)
        return CommandRefusedError(sentence)

    def _wire_value(self, row: ControlRow, value: Any) -> Any:
        if row.kind == "meter":
            raise ValueError(f"{row.name} is a meter, which can only be read.")
        if row.kind == "boolean":
            flag = _to_bool(value)
            if flag is None:
                raise ValueError(f"{row.name} takes on or off, not {value!r}.")
            return 1 if flag else 0
        if row.kind == "text":
            return "" if value is None else str(value)
        number = _to_number(value)
        if number is None:
            raise ValueError(f"{row.name} takes a number, not {value!r}.")
        if row.kind == "level" and not 0 <= float(number) <= 1:
            raise ValueError(f"{row.name} is a Level, from 0 to 1.")
        return _wire_number(float(number))

    async def _set(self, row: ControlRow, wire: Any) -> bool:
        try:
            # The reply carries the value the processor applied, which
            # on_data_received has already put into state.
            await self._request("set", {"path": row.path, "format": row.fmt, "value": wire})
        except HControlError as exc:
            raise self._refusal(exc, row) from exc
        return True

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None,
    ) -> Any:
        params = params or {}
        if command == "set_control":
            _sid, _prop, row = self._target(params)
            return await self._set(row, self._wire_value(row, params.get("value")))
        if command == "toggle_control":
            sid, prop, row = self._target(params)
            if row.kind != "boolean":
                raise ValueError(f"{row.name} is not an On/Off control.")
            current = self.get_child_state("group", sid).get(prop)
            if current is None:
                raise ValueError(f"{row.name} has not been read from the processor yet.")
            return await self._set(row, 0 if current else 1)
        if command == "step_control":
            sid, prop, row = self._target(params)
            if row.kind not in ("number", "level"):
                raise ValueError(f"{row.name} is not a Number or Level control.")
            current = self.get_child_state("group", sid).get(prop)
            if current is None:
                raise ValueError(f"{row.name} has not been read from the processor yet.")
            by = _to_number(params.get("by"))
            if by is None:
                raise ValueError("Change By takes a number.")
            target = float(current) + float(by)
            if row.kind == "level":
                target = min(1.0, max(0.0, target))
            else:
                if row.minimum is not None:
                    target = max(row.minimum, target)
                if row.maximum is not None:
                    target = min(row.maximum, target)
            return await self._set(row, _wire_number(round(target, 6)))
        if command == "exec_command":
            path = str(params.get("path") or "").strip()
            problem = path_problem(path)
            if problem:
                raise ValueError(f"The path {path} {problem}.")
            body: dict[str, Any] = {"path": path, "command": str(params.get("command") or "").strip()}
            if not body["command"]:
                raise ValueError("A command name is needed.")
            arguments = str(params.get("arguments") or "").strip()
            if arguments:
                try:
                    body["arguments"] = json.loads(arguments)
                except json.JSONDecodeError:
                    # The guide allows a plain string as the arguments.
                    body["arguments"] = arguments
            try:
                reply = await self._request("exec", body)
            except HControlError as exc:
                raise self._refusal(exc, path) from exc
            # A bare "@exec" is the processor's acknowledgement.
            return reply if reply is not None else True
        if command == "send_message":
            return await self._send_message(str(params.get("message") or ""))
        if command == "refresh_controls":
            return await self.refresh_children()
        log.warning(f"[{self.device_id}] Unknown command: {command}")
        return None

    async def _send_message(self, message: str) -> Any:
        text = message.strip()
        if not text or "\n" in text or "\r" in text:
            raise ValueError("One HControl message on one line is needed.")
        m = MESSAGE_RE.match(text)
        if m is None or m.group(1):
            raise ValueError("A message starts with get, set or exec.")
        word, rest = m.group(2), m.group(3).strip()
        if word in DRIVER_OWNED_COMMANDS:
            raise ValueError(
                f"{word} cannot be sent from here: this device follows its "
                f"controls itself. List the control in the Controls table instead."
            )
        body: Any = None
        if rest:
            try:
                body = json.loads(rest)
            except json.JSONDecodeError as exc:
                raise ValueError(f"The part after {word} is not valid JSON: {exc.msg}.") from exc
        try:
            reply = await self._request(word, body)
        except HControlError as exc:
            asked = body.get("path", "") if isinstance(body, dict) else ""
            raise self._refusal(exc, exc.path or str(asked)) from exc
        return reply if reply is not None else True
