"""
QSC Q-SYS QRC Driver — first-class auto-discovery edition.

Controls QSC Q-SYS Cores over the Q-SYS Remote Control protocol (QRC) on
TCP port 1710. QRC is JSON-RPC 2.0 with null-terminated frames, the
modern replacement for QSC's older External Control Protocol (ECP, port
1702). QSC's docs recommend QRC over ECP: QRC reaches into Components,
Mixers, Snapshots, and the PA Router without wiring every value to a
Named Control.

What makes this driver first-class
-----------------------------------
* **Zero-typing topology import.** On connect the driver enumerates the
  Core's script-accessible Components (`Component.GetComponents`) and
  each one's real control set (`Component.GetControls`), then models
  every Component as a **child entity** with its own discovered, typed
  control schema. The integrator declares nothing — the Core describes
  itself. (Optionally list a few Named Controls, since QRC has no way to
  enumerate those.)
* **Real-time push, not polling.** Every discovered control is added to a
  single Change Group with `ChangeGroup.AutoPoll`; the Core pushes
  deltas, which fan out into per-child state in one atomic batch.
* **Heterogeneous control sets, honestly.** A 4x4 mixer, an 8-output loop
  player, and a boolean-crosspoint router each expose a different control
  surface; the driver builds each child's schema from what the Core
  actually returns rather than assuming control names per category.

Coverage (commands):
    - Component gain / mute / any control (set_gain, set_mute,
      set_component_control, get_component_control)
    - Named Controls (typed: number / boolean / string / trigger)
    - Mixer: input/output gain+mute+solo, crosspoint gain+mute+solo,
      Q-SYS channel-spec grammar (`* 1-6 !3`), ramp
    - Audio Router (per-output input select, via Component.Set)
    - Snapshot bank (load/save by index, optional ramp)
    - Loop Player (start / stop / cancel)
    - PA Router (PARAPI: page submit/start/stop/cancel)
    - Raw escape hatches (send_raw_jsonrpc, set/get_component_control)
    - Runtime change-group subscriptions (subscribe_*)
    - Optional Logon authentication
    - Quick actions: re-import topology, mute/unmute all, keep-alive
    - Offline "test connection / list components" setup wizard

Why Python (not a YAML .avcdriver):
    1) JSON-RPC 2.0 framing with a NUL terminator and request/response
       correlation by `id`.
    2) Runtime-discovered, per-instance child schemas (a 4x4 mixer != an
       8x8 mixer; plugins expose arbitrary controls). YAML child types
       are static; only a Python driver can `register_child(schema=...)`.
    3) Change-group push: one incoming `Changes` array fans out into many
       child state keys via a (Component, Name) -> child route map.

Sources (verified against a live Core 24f):
    - https://q-syshelp.qsc.com/Content/External_Control_APIs/QRC/QRC_Overview.htm
    - https://q-syshelp.qsc.com/Content/External_Control_APIs/QRC/QRC_Commands.htm
    - PARAPI: .../QRC/PARAPI.htm
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import re
from typing import Any

from server.drivers.base import BaseDriver, ConnectionFaultError
from server.utils.logger import get_logger

log = get_logger(__name__)


# ── Wire-protocol constants ──

# QRC default port. Q-SYS Designer also exposes QRC in Emulate mode here.
DEFAULT_PORT = 1710

# Each JSON-RPC frame is terminated by a single 0x00 byte, not a newline.
# The TCP transport's DelimiterFrameParser splits on \x00 for us.
FRAME_TERMINATOR = b"\x00"

# Discovered controls go into change groups. Q-SYS allows at most 4 per
# connection: component controls use one, Named Controls a second (only when
# any are configured), leaving 2+ for runtime subscribe_* commands. The two
# are separate on purpose — a control exposed BOTH as a Named Control and via
# its component is a single underlying object, and the Core reports such a
# change under only ONE name within a single group (the named one shadows the
# component one). Separate groups make the Core push the change to each, so
# both the component child and the named-control child update. (Verified on a
# live Core 24f.)
MAIN_CHANGE_GROUP = "openavc_main"
NAMED_CHANGE_GROUP = "openavc_named"

# Auto-poll rate (seconds). 0.25 s is responsive for fader bargraphs and
# easy on the Core. AutoPoll pushes are core->client only and do NOT count
# as client keep-alive traffic (see HEALTH_INTERVAL_S on the driver class).
DEFAULT_AUTOPOLL_RATE_S = 0.25

# Inter-command delay default — spaces out the connect-time AddControl
# burst on small Cores. QRC's JSON-RPC parser has no documented per-command
# rate limit.
DEFAULT_INTER_COMMAND_DELAY = 0.02

INITIAL_REQUEST_ID = 1

# Per-probe reply deadline for the NoOp keep-alive. Cadence and rationale
# live on the driver class (HEALTH_INTERVAL_S / _liveness_probe).
KEEPALIVE_TIMEOUT_S = 5.0

# QRC error codes. Negative codes are JSON-RPC standard; positive ones are
# QSC-specific (see api-docs/qrc-overview.md §7).
QRC_ERR_LOGON_REQUIRED = 10
QRC_ERR_CHANGE_GROUPS_EXHAUSTED = 5
QRC_ERR_UNKNOWN_CHANGE_GROUP = 6
QRC_ERR_UNKNOWN_COMPONENT = 7
QRC_ERR_UNKNOWN_CONTROL = 8
QRC_ERR_ILLEGAL_MIXER_CHANNEL = 9
QRC_ERR_METHOD_NOT_FOUND = -32601
QRC_ERR_CORE_STANDBY = -32604

# Named-control value-type hints the user may declare in the config list.
NC_TYPE_HINTS = {"number", "boolean", "string", "trigger"}

# Q-SYS control `Type` -> our state-var type. Controls carry a native
# `Value` plus a human `String`; `Trigger` is momentary/write-only and
# carries no readable value.
_QSYS_TYPE_MAP = {
    "Float": "number",
    "Integer": "integer",
    "Boolean": "boolean",
    "State Trigger": "number",   # discrete state value (e.g. snapshot last-loaded)
    "Text": "string",
    "Time": "string",            # value is seconds; the human String is the display
}

# Friendly category per Q-SYS component `Type` (display + nicer summary
# column). Unknown types fall through to a title-cased form.
_CATEGORY_MAP = {
    "gain": "Gain",
    "mixer": "Mixer",
    "router_with_output": "Router",
    "snapshot_controller": "Snapshots",
    "loop_player": "Loop Player",
    "status_combiner": "Status",
    "router": "Router",
}

# Channel-spec grammar help, shared by the mixer command params.
_SPEC_HELP = (
    "Q-SYS channel spec: '*' = all, '1 2 3', '1-6', '1-3 5-9', "
    "'1-8 !3' (all but 3), '* !3-5'."
)

# Columns for the Named Controls `type: table` config field. QRC can't
# enumerate Named Controls, so the integrator declares the ones to monitor as
# rows on the device page (Name + optional value type). Declared once and
# reused in config_schema so the device-page table editor renders the right
# widgets. Blank/omitted type = auto-detect from the Core's first report.
NAMED_CONTROLS_COLUMNS = {
    "name": {
        "type": "string", "label": "Name", "required": True,
        "help": "Named Control name — case-sensitive, must match the Named "
                "Controls list in Q-SYS Designer exactly.",
    },
    "type": {
        "type": "enum", "label": "Type",
        "values": [
            {"value": "number", "label": "Number"},
            {"value": "boolean", "label": "Boolean"},
            {"value": "string", "label": "String"},
            {"value": "trigger", "label": "Trigger (write-only button)"},
        ],
        "help": "Value type. Leave blank to auto-detect from the Core.",
    },
}


# ── Helpers ──

def _safe_id(name: str) -> str:
    """Sanitize a Q-SYS Code Name / control name into a child local-id
    (the platform requires `[A-Za-z0-9_-]`). The original is kept in the
    child's `name` state var + the driver's code-name map.
    """
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def _safe_prop(name: str) -> str:
    """Sanitize a Q-SYS control name into a child state-var key. Keeps dots
    (QRC control names are dotted, e.g. `input.1.gain`, and the platform/IDE
    address child props by the segment after the local id) but strips glob
    metacharacters / whitespace so subscription matching stays literal.
    """
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def _category_for(qsys_type: str) -> str:
    t = (qsys_type or "").strip()
    if t in _CATEGORY_MAP:
        return _CATEGORY_MAP[t]
    return t.replace("_", " ").title() if t else "Component"


def _named_controls_text_to_rows(text: str) -> list[dict[str, Any]]:
    """One-shot converter: the legacy ``<Name> [type]`` textarea -> table rows.

    Named Controls used to be a `type: text` field the integrator hand-typed
    one-per-line; it is now a `type: table`. A project saved before the table
    editor stores a string here — convert it (reusing the old line grammar) so
    it still loads and can be re-authored in the row editor without hand
    migration. ``#``/``;`` comment and blank lines are dropped; an unrecognized
    type token becomes no type (auto-detect).
    """
    rows: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        parts = line.split()
        row: dict[str, Any] = {"name": parts[0]}
        if len(parts) >= 2 and parts[1].lower() in NC_TYPE_HINTS:
            row["type"] = parts[1].lower()
        rows.append(row)
    return rows


def parse_named_controls(value: Any) -> list[tuple[str, str | None]]:
    """Parse the Named Controls config into [(name, type_hint|None)].

    Accepts the `type: table` row list (``[{"name", "type"}, ...]``) and, for a
    project saved before the table editor shipped, a legacy ``<Name> [type]``
    textarea string (converted to rows first). A blank/unknown type is treated
    as no hint (auto-detect); a nameless row is skipped.
    """
    if isinstance(value, str):
        value = _named_controls_text_to_rows(value)
    out: list[tuple[str, str | None]] = []
    if not isinstance(value, list):
        return out
    for row in value:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name", "")).strip()
        if not name:
            continue
        tok = str(row.get("type", "") or "").strip().lower()
        hint = tok if tok in NC_TYPE_HINTS else None
        out.append((name, hint))
    return out


def _coerce_value(raw: Any, type_hint: str) -> Any:
    """Coerce an inbound QRC value into the declared state-var type."""
    if raw is None:
        return None
    if type_hint == "boolean":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        s = str(raw).strip().lower()
        if s in ("true", "1", "on", "yes", "muted"):
            return True
        if s in ("false", "0", "off", "no", "unmuted", "normal"):
            return False
        return None
    if type_hint == "integer":
        try:
            return int(float(raw))
        except (ValueError, TypeError):
            return None
    if type_hint == "number":
        try:
            return float(raw)
        except (ValueError, TypeError):
            return None
    return str(raw)


def _coerce_outgoing(raw: Any) -> Any:
    """Convert a Python/string value into the JSON-typed form Q-SYS expects
    (number-like -> number, 'true'/'false' -> bool, else string).
    """
    if isinstance(raw, (int, float, bool)):
        return raw
    if raw is None:
        return None
    s = str(raw).strip()
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    try:
        if "." in s or "e" in s.lower():
            return float(s)
        return int(s)
    except ValueError:
        return s


def _as_bool(raw: Any) -> bool:
    return str(raw).strip().lower() in ("true", "1", "on", "yes")


def _csv_ints(spec: Any) -> list[int]:
    out: list[int] = []
    for piece in str(spec or "").replace(",", " ").split():
        try:
            out.append(int(piece))
        except ValueError:
            continue
    return out


def _default_for(type_hint: str) -> Any:
    if type_hint == "boolean":
        return False
    if type_hint == "integer":
        return 0
    if type_hint == "number":
        return 0.0
    return ""


# ── The driver ──

class QSCQRCDriver(BaseDriver):
    """QSC Q-SYS QRC driver — auto-discovers the Core's topology and models
    each Component as a typed child entity with real-time push updates."""

    DRIVER_INFO = {
        "id": "qsc_qrc",
        "name": "QSC Q-SYS QRC",
        "manufacturer": "QSC",
        "category": "audio",
        "version": "4.3.0",
        # Requires the `type: table` config-field editor (Named Controls;
        # 0.23.0), which supersedes the typed-connection-faults (0.22.0) and
        # runtime-discovered child-entity (0.19.4) requirements.
        "min_platform_version": "0.23.0",
        "author": "OpenAVC",
        "description": (
            "Controls QSC Q-SYS Cores via QRC (Q-SYS Remote Control) — "
            "JSON-RPC 2.0 over TCP port 1710. Auto-discovers the running "
            "design's Components and their controls on connect (no manual "
            "block list), models each Component as a typed child entity, "
            "and streams real-time state via Change Group auto-poll. Covers "
            "Component control, Named Controls, Mixer, Audio Router, "
            "Snapshot recall/save, Loop Player, and PA Router paging."
        ),
        "source_url": (
            "https://q-syshelp.qsc.com/Content/External_Control_APIs/QRC/"
            "QRC_Commands.htm"
        ),
        "tags": ["dsp", "q-sys", "qrc", "ceiling-mic", "conferencing"],
        "verified": True,
        "simulated": True,
        "protocols": ["qsc_qrc"],
        "ports": [1710],
        "transport": "tcp",
        "discovery": {
            # QRC speaks JSON-RPC over TCP/1710 with NUL framing. `StatusGet`
            # (`params: 0`) is a side-effect-free pull that returns the Core's
            # Platform + DesignName; its `"Platform"` marker also matches the
            # unsolicited EngineStatus the Core pushes on connect, so the probe
            # succeeds whichever arrives first. (Do NOT call `EngineStatus` — it
            # is push-only and returns -32601 Method not found.)
            "tcp_probe": {
                "port": 1710,
                "send_ascii": (
                    '{"jsonrpc":"2.0","id":1,"method":"StatusGet","params":0}\x00'
                ),
                "expect_regex": r'"Platform"\s*:\s*"',
                "extract_manufacturer": "QSC",
                "extract": {
                    "model": {"regex": r'"Platform"\s*:\s*"([^"]+)"'},
                    "device_name": {"regex": r'"DesignName"\s*:\s*"([^"]+)"'},
                },
            },
            "oui": ["00:60:74"],
        },
        "compatible_models": [
            {
                "manufacturer": "QSC",
                "models": ["Core 24f"],
                "confidence": "full",
                "notes": (
                    "Verified end-to-end against a live Core 24f: auto-discovery "
                    "(Component.GetComponents / GetControls), change-group push, "
                    "and round-trips for gain/mute, mixer crosspoint, router, "
                    "snapshot, and Named Controls."
                ),
            },
            {
                "manufacturer": "QSC",
                "models": [
                    "Core 8 Flex",
                    "Core 110f",
                    "Core 510i",
                    "Core 610",
                    "Core Nano",
                    "NV-32-H",
                    "NV-21-HU",
                    "Q-SYS Core series",
                ],
                "confidence": "untested",
                "notes": (
                    "Every Q-SYS Core that runs Q-SYS Designer speaks QRC "
                    "(TCP 1710) — the same protocol verified on the Core 24f — so "
                    "these are expected to work but haven't been tested on real "
                    "hardware. This driver uses QRC, not the legacy ECP "
                    "(TCP 1702). In Designer, set each block's Script Access to "
                    "External (or All) so it appears in auto-discovery; add Named "
                    "Controls to the Named Controls list to monitor them."
                ),
            },
        ],
        "help": {
            "overview": (
                "Comprehensive QSC Q-SYS control over QRC (JSON-RPC 2.0 on "
                "TCP 1710). On connect the driver reads the running design's "
                "Components and their controls straight from the Core and "
                "lists each Component as a child entity under the device — no "
                "block list to type. Every control is subscribed via a Change "
                "Group with auto-poll, so bindings update in real time. Use "
                "the per-component commands (Set Gain, Set Mute, Mixer …, "
                "Recall Snapshot, …) in macros and panels, and 'Re-import "
                "Topology' after you change the Q-SYS design."
            ),
            "setup": (
                "STEP 1 — Expose your blocks in Q-SYS Designer.\n"
                "For each block you want to control (Gain, Mixer, Router, "
                "Snapshot Controller, Loop Player, …), open its Properties and "
                "set 'Script Access' to 'External' (or 'All'). Give it a clear "
                "'Code Name'. Components WITHOUT Script Access = External are "
                "invisible to QRC — this is the #1 setup mistake. Save to Core "
                "& Run (F5) so the change takes effect.\n"
                "\n"
                "STEP 2 — (Optional) Named Controls.\n"
                "Individual controls you drag into Designer's Named Controls "
                "list (e.g. 'Volume', 'Mute') can be monitored too. QRC cannot "
                "enumerate these, so add each one you want to the 'Named "
                "Controls' table on the device page (Name, plus an optional "
                "type — number/boolean/string/trigger). Most rooms need none, "
                "since Components are discovered automatically.\n"
                "\n"
                "STEP 3 — Enter the Core's IP address above.\n"
                "If the Core has authentication enabled (Core Manager → Users), "
                "fill in Username and Password; otherwise leave them blank.\n"
                "\n"
                "STEP 4 — Save. Within a second or two the device connects, the "
                "Core's status (Active/Idle/Standby), model, design name, and "
                "health populate, and each exposed Component appears under the "
                "device's Components tab with its live, typed controls. Bind "
                "child state (e.g. device.<id>.component.PgmGain.gain) to UI "
                "elements, or drive commands like 'Set Gain' / 'Recall "
                "Snapshot' from macros and panel buttons.\n"
                "\n"
                "Troubleshooting:\n"
                "  • No components listed — the blocks aren't exposed. Set "
                "Script Access = External in Designer and redeploy (F5). Use "
                "the 'Test Connection' action to list what QRC currently sees.\n"
                "  • 'Connection refused' on 1710 — the Core isn't in Run mode, "
                "or a VLAN ACL blocks QRC.\n"
                "  • Logon errors — set Username/Password to match a Q-SYS user.\n"
                "  • Design changed — run the 'Re-import Topology' action (or "
                "the Components tab's Refresh) to re-read the control surface.\n"
                "  • Need something the typed commands don't cover — use 'Set "
                "Component Control' or 'Send Raw JSON-RPC'."
            ),
        },
        "quick_actions": [
            "reimport_topology", "mute_all", "unmute_all", "noop",
        ],
        "actions": [
            {"id": "reimport_topology", "kind": "command", "icon": "radar"},
            {"id": "mute_all", "kind": "command", "icon": "volume-x"},
            {"id": "unmute_all", "kind": "command", "icon": "volume-2"},
            {"id": "noop", "kind": "command", "icon": "activity"},
            {
                "id": "test_connection",
                "kind": "setup",
                "label": "Test Connection / List Components",
                "icon": "search",
                "availability": "always",
            },
        ],
        "child_entity_types": {
            "component": {
                "label": "Component",
                "label_plural": "Components",
                "dynamic": True,
                "id_format": {"type": "string", "max_length": 128},
                # Per-child schema is published at register_child(schema=…);
                # the type-level schema only carries the shared summary fields.
                "state_variables": {
                    "name": {"type": "string", "label": "Code Name"},
                    "category": {"type": "string", "label": "Category"},
                    "qsys_type": {"type": "string", "label": "Q-SYS Type"},
                    "control_count": {"type": "integer", "label": "Controls"},
                },
                "summary_fields": ["name", "category", "control_count"],
                "label_field": "name",
            },
            "named_control": {
                "label": "Named Control",
                "label_plural": "Named Controls",
                "dynamic": True,
                "id_format": {"type": "string", "max_length": 128},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "string": {"type": "string", "label": "Display"},
                },
                "summary_fields": ["name", "string"],
                "label_field": "name",
            },
        },
        "default_config": {
            "host": "",
            "port": DEFAULT_PORT,
            "username": "",
            "password": "",
            "named_controls": [],
            "component_filter": "",
            "autopoll_rate_seconds": DEFAULT_AUTOPOLL_RATE_S,
            "inter_command_delay": DEFAULT_INTER_COMMAND_DELAY,
            "poll_interval": 0,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {
                "type": "integer", "default": DEFAULT_PORT, "label": "QRC Port",
                "min": 1, "max": 65535,
                "description": (
                    "QSC's QRC TCP port. Default 1710. Note: 1702 is the legacy "
                    "ECP port (a different protocol) and is not supported."
                ),
            },
            "username": {
                "type": "string", "default": "", "label": "Username",
                "description": (
                    "Q-SYS user (Core Manager → Users). Leave blank if the Core "
                    "has no authentication configured."
                ),
            },
            "password": {
                "type": "string", "default": "", "label": "Password",
                "secret": True,
            },
            "named_controls": {
                "type": "table", "label": "Named Controls",
                "row_label": "control",
                "columns": NAMED_CONTROLS_COLUMNS,
                "help": (
                    "Optional. QRC can't enumerate Named Controls, so add each "
                    "one you want to monitor or bind (Name + optional value "
                    "type). Components are auto-discovered and don't go here."
                ),
            },
            "component_filter": {
                "type": "string", "default": "", "label": "Component Filter",
                "description": (
                    "Optional. Comma/space-separated glob patterns; when set, "
                    "only Components whose Code Name matches are imported (e.g. "
                    "'Pgm* Room*'). Leave blank to import every exposed "
                    "Component."
                ),
            },
            "autopoll_rate_seconds": {
                "type": "number", "default": DEFAULT_AUTOPOLL_RATE_S,
                "min": 0.05, "max": 60.0, "label": "Auto-Poll Rate (sec)",
                "description": (
                    "How often the Core pushes change-group updates. 0.25 "
                    "(250 ms) is responsive for fader UIs; raise for low-traffic "
                    "systems."
                ),
            },
            "inter_command_delay": {
                "type": "number", "default": DEFAULT_INTER_COMMAND_DELAY,
                "min": 0, "label": "Inter-Command Delay (sec)",
                "description": (
                    "Delay between sequential outgoing commands. A small value "
                    "keeps the connect-time discovery burst well-ordered on "
                    "smaller Cores."
                ),
            },
            "poll_interval": {
                "type": "integer", "default": 0, "min": 0,
                "label": "Backstop Poll Interval (sec)",
                "description": (
                    "Optional StatusGet refresh. Auto-poll is the source of "
                    "truth — leave at 0 unless you've seen state drift."
                ),
            },
        },
        "state_variables": {
            "core_state": {
                "type": "string", "label": "Core State",
                "help": "Q-SYS Core engine state: Idle / Active / Standby.",
            },
            "core_platform": {"type": "string", "label": "Core Model"},
            "core_design_name": {"type": "string", "label": "Running Design"},
            "core_design_code": {"type": "string", "label": "Design Code"},
            "core_is_redundant": {"type": "boolean", "label": "Redundant Pair"},
            "core_is_emulator": {"type": "boolean", "label": "Emulator Mode"},
            "core_health": {
                "type": "string", "label": "Core Health",
                "help": "Status string from the Core (e.g. 'OK', "
                        "'Missing - 1 OK, 1 Missing').",
            },
            "core_health_code": {
                "type": "integer", "label": "Core Health Code",
                "help": "Numeric Status.Code; 0 = OK.",
            },
            "core_standby": {
                "type": "boolean", "label": "On Standby",
                "help": "True when this Core is the standby half of a redundant "
                        "pair (commands return error -32604).",
            },
            "logon_ok": {
                "type": "boolean", "label": "Logon Successful",
                "help": "True after a successful Logon, or when no auth is "
                        "required.",
            },
            "topology_loaded": {
                "type": "boolean", "label": "Topology Imported",
                "help": "True once auto-discovery has registered the Core's "
                        "Components.",
            },
            "component_count": {"type": "integer", "label": "Components"},
            "named_control_count": {"type": "integer", "label": "Named Controls"},
            "snapshot_banks": {
                "type": "string", "label": "Snapshot Banks",
                "help": "JSON list of discovered Snapshot bank names (the Code "
                        "Names of Snapshot Controller components). Populates the "
                        "Recall/Save Snapshot 'bank_name' picker.",
            },
            "last_error": {
                "type": "string", "label": "Last QRC Error",
                "help": "Most recent JSON-RPC error message from the Core.",
            },
            "last_query_result": {
                "type": "string", "label": "Last Get Result",
                "help": "JSON of the last raw get result (for debugging).",
            },
        },
        "commands": {},   # populated in __init__ by _build_commands()
    }

    # BaseDriver liveness watchdog tuning. The Core closes any connection
    # whose CLIENT has gone silent for 60 s (verified live on a Core 24f),
    # and an AutoPoll subscription is core->client only, so receiving pushes
    # does NOT reset that timer; the client must still send something. A NoOp
    # every 20 s keeps the session open with a wide margin AND doubles as the
    # liveness probe (see _liveness_probe).
    HEALTH_INTERVAL_S = 20.0
    HEALTH_FAULT_MESSAGE = (
        "Connected, but the Core stopped answering keep-alive probes."
    )

    def __init__(
        self, device_id: str, config: dict[str, Any], state: Any, events: Any,
    ) -> None:
        self.DRIVER_INFO = {
            **type(self).DRIVER_INFO,
            "commands": _build_commands(),
        }
        super().__init__(device_id, config, state, events)

        # JSON-RPC plumbing.
        self._next_id = INITIAL_REQUEST_ID
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._send_lock = asyncio.Lock()

        # Discovered topology.
        #   _component_codename: child local-id (sanitized) -> Q-SYS Code Name
        #   _component_schemas:  child local-id -> last schema (reconcile guard)
        #   _component_controls: child local-id -> [QRC control names subscribed]
        #   _named_codename:     child local-id -> Named Control name
        #   _control_route: (Code Name|None, control name) -> route dict
        self._component_codename: dict[str, str] = {}
        self._component_schemas: dict[str, dict[str, Any]] = {}
        self._component_controls: dict[str, list[str]] = {}
        self._named_codename: dict[str, str] = {}
        self._control_route: dict[tuple[str | None, str], dict[str, Any]] = {}

        # Runtime change-groups created via subscribe_* commands.
        # group_id -> list[(component|None, control, state_key, type_hint)]
        self._runtime_groups: dict[str, list[tuple[str | None, str, str, str]]] = {}

        self._subscribed_count = 0

    # ── Lifecycle ──

    async def connect(self) -> None:
        from server.transport.tcp import TCPTransport
        from server.system_config import get_system_config

        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", DEFAULT_PORT))
        if not host:
            raise ConnectionError(f"[{self.device_id}] No host configured")

        delay = float(self.config.get(
            "inter_command_delay", DEFAULT_INTER_COMMAND_DELAY))
        control_ip = get_system_config().get("network", "control_interface")

        # Clean slate: a custom connect() must do what BaseDriver.connect()
        # does — close any half-open transport from a previous attempt and
        # drop stale fault causes so they can't color this attempt's
        # offline-reason classification.
        self._last_transport_error = ""
        self._last_fault = None
        if self.transport:
            try:
                await self.transport.close()
            except Exception:
                pass
            self.transport = None

        self.transport = await TCPTransport.create(
            host=host, port=port,
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            delimiter=FRAME_TERMINATOR,
            inter_command_delay=delay,
            name=self.device_id,
            local_addr=(control_ip, 0) if control_ip else None,
        )

        # Optional Logon BEFORE reporting connected. Q-SYS uses sticky
        # session auth — once Logon succeeds every command on this socket is
        # authenticated. A rejected logon must fail the attempt outright
        # (close the socket, never emit device.connected); reporting
        # connected first flapped the device online/offline through the
        # reconnect backoff and leaked one socket per attempt.
        username = str(self.config.get("username", "")).strip()
        password = str(self.config.get("password", ""))
        try:
            if username:
                await self._do_logon(username, password)
            else:
                self.set_state("logon_ok", True)
        except Exception:
            self._stash_transport_error()
            if self.transport:
                try:
                    await self.transport.close()
                except Exception:
                    pass
                self.transport = None
            self._connected = False
            raise

        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        log.info(f"[{self.device_id}] Connected to Q-SYS Core at {host}:{port}")

        await self._do_status_get()
        await self._discover_topology()

        poll_interval = int(self.config.get("poll_interval", 0))
        if poll_interval > 0:
            await self.start_polling(poll_interval)

        # Client-side NoOp keep-alive + liveness probe runs for the life of
        # the connection (see HEALTH_INTERVAL_S): AutoPoll is core->client
        # only, so without it the Core closes the socket every 60 s (flap)
        # and a vanished Core is never noticed. Started explicitly because
        # this custom connect() never runs BaseDriver.connect()'s auto-start.
        self._start_health_loop()

    async def disconnect(self) -> None:
        await self.stop_polling()
        self._stop_health_loop()
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

        if self.transport:
            try:
                for gid in (MAIN_CHANGE_GROUP, NAMED_CHANGE_GROUP,
                            *self._runtime_groups.keys()):
                    try:
                        await self._send_jsonrpc(
                            "ChangeGroup.Destroy", {"Id": gid},
                            expect_response=False)
                    except (ConnectionError, OSError):
                        break
            except (ConnectionError, OSError):
                pass
            try:
                await self.transport.close()
            except Exception:
                pass
            self.transport = None

        self._connected = False
        self.set_state("connected", False)
        self.set_state("topology_loaded", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info(f"[{self.device_id}] Disconnected")

    def _handle_transport_disconnect(self) -> None:
        # BaseDriver's disconnect cleanup stops the health loop; this
        # override only cancels the id-correlated response futures.
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        super()._handle_transport_disconnect()

    # ── JSON-RPC plumbing ──

    def _allocate_id(self) -> int:
        rid = self._next_id
        self._next_id += 1
        return rid

    async def _send_jsonrpc(
        self, method: str, params: Any | None = None,
        expect_response: bool = True, timeout: float = 5.0,
    ) -> Any:
        """Send one JSON-RPC frame; await the id-correlated response (or
        fire-and-forget when expect_response is False). Raises QRCError on a
        JSON-RPC error, TimeoutError on no reply, ConnectionError if down."""
        if self.transport is None or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        async with self._send_lock:
            envelope: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                envelope["params"] = params
            if expect_response:
                rid = self._allocate_id()
                envelope["id"] = rid
                fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
                self._pending[rid] = fut
            payload = json.dumps(envelope).encode("utf-8") + FRAME_TERMINATOR
            await self.transport.send(payload)

        if not expect_response:
            return None
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise TimeoutError(
                f"[{self.device_id}] QRC {method} timed out after {timeout}s")
        finally:
            self._pending.pop(rid, None)

    async def on_data_received(self, data: bytes) -> None:
        try:
            text = data.decode("utf-8", errors="replace").strip()
        except Exception:
            log.warning(f"[{self.device_id}] Undecodable frame: {data!r}")
            return
        if not text:
            return
        try:
            msg = json.loads(text)
        except json.JSONDecodeError as exc:
            log.warning(f"[{self.device_id}] Bad JSON frame: {exc}: {text!r}")
            return
        if not isinstance(msg, dict):
            return

        rid = msg.get("id")
        if rid is not None and rid in self._pending:
            fut = self._pending.pop(rid)
            if "error" in msg and msg["error"] is not None:
                err = msg["error"]
                err_str = (err.get("message") if isinstance(err, dict)
                           else str(err))
                self.set_state("last_error", str(err_str))
                if not fut.done():
                    fut.set_exception(QRCError(err))
            else:
                if not fut.done():
                    fut.set_result(msg.get("result", msg.get("response")))
            return

        # Unsolicited frame — route by method.
        method = msg.get("method", "")
        params = msg.get("params", {})
        if method == "EngineStatus":
            self._handle_engine_status(params)
        elif method == "ChangeGroup.Poll":
            self._handle_change_group_changes(params)
        else:
            result = msg.get("result")
            if isinstance(result, dict) and "Changes" in result:
                self._handle_change_group_changes(result)
            elif isinstance(params, dict) and "Changes" in params:
                self._handle_change_group_changes(params)
            else:
                log.debug(f"[{self.device_id}] Unhandled QRC frame: {msg!r}")

    # ── Connection-time setup ──

    async def _do_logon(self, username: str, password: str) -> None:
        try:
            await self._send_jsonrpc(
                "Logon", {"User": username, "Password": password}, timeout=5.0)
            self.set_state("logon_ok", True)
            log.info(f"[{self.device_id}] Logon successful")
        except QRCError as exc:
            self.set_state("logon_ok", False)
            log.error(f"[{self.device_id}] Logon failed: {exc}")
            # Typed fault: the code maps straight to
            # device.<id>.offline_reason=auth_failed.
            raise ConnectionFaultError(
                f"Q-SYS Logon authentication failed: {exc}",
                code="auth_failed") from exc
        except (TimeoutError, ConnectionError, OSError) as exc:
            self.set_state("logon_ok", False)
            log.error(f"[{self.device_id}] Logon error: {exc}")
            raise

    async def _do_status_get(self) -> None:
        try:
            result = await self._send_jsonrpc("StatusGet", 0, timeout=3.0)
        except (QRCError, TimeoutError, ConnectionError, OSError) as exc:
            log.warning(f"[{self.device_id}] StatusGet failed: {exc}")
            return
        if isinstance(result, dict):
            self._update_status_from_payload(result)

    def _update_status_from_payload(self, payload: dict[str, Any]) -> None:
        if "State" in payload:
            self.set_state("core_state", str(payload["State"]))
            self.set_state("core_standby", str(payload["State"]) == "Standby")
        if "Platform" in payload:
            self.set_state("core_platform", str(payload["Platform"]))
        if "DesignName" in payload:
            self.set_state("core_design_name", str(payload["DesignName"]))
        if "DesignCode" in payload:
            self.set_state("core_design_code", str(payload["DesignCode"]))
        if "IsRedundant" in payload:
            self.set_state("core_is_redundant", bool(payload["IsRedundant"]))
        if "IsEmulator" in payload:
            self.set_state("core_is_emulator", bool(payload["IsEmulator"]))
        status = payload.get("Status")
        if isinstance(status, dict):
            self.set_state("core_health", str(status.get("String", "")))
            try:
                self.set_state("core_health_code", int(status.get("Code", 0)))
            except (ValueError, TypeError):
                pass

    def _handle_engine_status(self, params: Any) -> None:
        if isinstance(params, dict):
            self._update_status_from_payload(params)

    # ── Auto-discovery (the headline feature) ──

    async def _discover_topology(self) -> None:
        """Enumerate the Core's Components + their controls, reconcile child
        entities, and (re)build the main change group. Safe to call on
        connect, on reconnect, and from the Re-import action."""
        try:
            components = await self._send_jsonrpc(
                "Component.GetComponents", None, timeout=5.0)
        except QRCError as exc:
            if exc.code == QRC_ERR_CORE_STANDBY:
                # Standby half of a redundant pair — no live design to read.
                self.set_state("core_standby", True)
                self.set_state("core_state", "Standby")
                log.info(f"[{self.device_id}] Core on Standby; skipping topology")
                # The health loop (started in connect) keeps this socket alive
                # and watches for the Core leaving standby / the network.
                return
            log.warning(f"[{self.device_id}] GetComponents failed: {exc}")
            return
        except (TimeoutError, ConnectionError, OSError) as exc:
            log.warning(f"[{self.device_id}] GetComponents failed: {exc}")
            return

        if not isinstance(components, list):
            components = []

        patterns = self._filter_patterns()
        used_ids: set[str] = set()
        seen_component_sids: set[str] = set()
        seen_named_sids: set[str] = set()
        snapshot_banks: list[str] = []
        self._control_route = {}
        self._component_controls = {}

        # --- Components ---
        for comp in components:
            if not isinstance(comp, dict):
                continue
            codename = comp.get("Name")
            if not codename:
                continue
            if patterns and not any(
                fnmatch.fnmatch(codename, p) for p in patterns
            ):
                continue
            qtype = str(comp.get("Type", ""))
            # Snapshot Controllers are addressed by Code Name in Snapshot.Load /
            # Save, so collect their names to populate the bank picker.
            if qtype == "snapshot_controller":
                snapshot_banks.append(str(codename))
            try:
                controls = await self._send_jsonrpc(
                    "Component.GetControls", {"Name": codename}, timeout=5.0)
            except (QRCError, TimeoutError, ConnectionError, OSError) as exc:
                log.warning(
                    f"[{self.device_id}] GetControls({codename}) failed: {exc}")
                continue
            ctrl_list = []
            if isinstance(controls, dict):
                ctrl_list = controls.get("Controls") or []

            sid = self._unique_id(_safe_id(codename), used_ids)
            self._register_component(sid, codename, qtype, ctrl_list)
            seen_component_sids.add(sid)

        # --- Named Controls (user list; QRC can't enumerate them) ---
        # config value is the `type: table` row list (or a legacy string for a
        # project saved before the table editor — parse_named_controls handles
        # both).
        for name, hint in parse_named_controls(
            self.config.get("named_controls", [])
        ):
            if hint == "trigger":
                continue  # write-only; fired via trigger_named_control
            sid = self._unique_id(_safe_id(name), used_ids)
            if await self._register_named_control(sid, name, hint):
                seen_named_sids.add(sid)

        # --- Reconcile: drop children no longer present ---
        for sid in list(self._component_codename):
            if sid not in seen_component_sids:
                self.deregister_child("component", sid)
                self._component_codename.pop(sid, None)
                self._component_schemas.pop(sid, None)
        for sid in list(self._named_codename):
            if sid not in seen_named_sids:
                self.deregister_child("named_control", sid)
                self._named_codename.pop(sid, None)

        self.set_state("component_count", len(seen_component_sids))
        self.set_state("named_control_count", len(seen_named_sids))
        # Publish the discovered Snapshot bank names so the Recall/Save Snapshot
        # commands offer a `bank_name` dropdown (options_state) instead of a
        # free-typed Code Name.
        self.set_state("snapshot_banks", json.dumps(sorted(snapshot_banks)))
        self.set_state("topology_loaded", True)
        log.info(
            f"[{self.device_id}] Topology: {len(seen_component_sids)} components, "
            f"{len(seen_named_sids)} named controls")

        await self._setup_change_group()

    def _filter_patterns(self) -> list[str]:
        raw = str(self.config.get("component_filter", "") or "")
        return [p for p in re.split(r"[,\s]+", raw) if p]

    @staticmethod
    def _unique_id(base: str, used: set[str]) -> str:
        sid = base or "x"
        n = 2
        while sid in used:
            sid = f"{base}_{n}"
            n += 1
        used.add(sid)
        return sid

    def _register_component(
        self, sid: str, codename: str, qtype: str, controls: list[Any],
    ) -> None:
        """Build a component child's per-control schema from GetControls and
        register it (or re-register if its control set changed)."""
        schema: dict[str, dict[str, Any]] = {
            "name": {"type": "string", "label": "Code Name"},
            "category": {"type": "string", "label": "Category"},
            "qsys_type": {"type": "string", "label": "Q-SYS Type"},
            "control_count": {"type": "integer", "label": "Controls"},
        }
        initial: dict[str, Any] = {}
        sub_controls: list[str] = []

        for ctrl in controls:
            if not isinstance(ctrl, dict):
                continue
            cname = ctrl.get("Name")
            if not cname:
                continue
            qctype = str(ctrl.get("Type", ""))
            direction = str(ctrl.get("Direction", "Read/Write"))
            # Skip momentary triggers and write-only controls — no readable
            # state. They remain settable via set_component_control.
            if qctype == "Trigger" or direction == "Write Only":
                continue
            type_hint = _QSYS_TYPE_MAP.get(qctype, "string")
            value_from = "string" if qctype in ("Text", "Time") else "value"
            prop = _safe_prop(cname)
            has_str = value_from == "value"  # numeric/bool carry a human String

            # Mark as a settable control so the set_component_control "control"
            # picker (options_from: child_schema) offers it — and skips the
            # metadata / __str display-mirror vars, which aren't controls.
            var_def: dict[str, Any] = {
                "type": type_hint, "label": cname, "control": True,
            }
            if "ValueMin" in ctrl and type_hint in ("number", "integer"):
                try:
                    var_def["min"] = float(ctrl["ValueMin"])
                    var_def["max"] = float(ctrl["ValueMax"])
                except (ValueError, TypeError, KeyError):
                    pass
            schema[prop] = var_def
            str_prop = None
            if has_str:
                str_prop = f"{prop}__str"
                schema[str_prop] = {"type": "string", "label": f"{cname} (display)"}

            # Seed initial value from the GetControls snapshot.
            raw = ctrl.get("Value") if value_from == "value" else ctrl.get("String")
            coerced = _coerce_value(raw, type_hint)
            if coerced is not None:
                initial[prop] = coerced
            elif type_hint == "string":
                initial[prop] = str(ctrl.get("String", "") or "")
            if str_prop is not None:
                initial[str_prop] = str(ctrl.get("String", "") or "")

            sub_controls.append(cname)
            self._control_route[(codename, cname)] = {
                "ctype": "component", "sid": sid, "prop": prop,
                "type": type_hint, "value_from": value_from,
                "str_prop": str_prop, "pos_prop": None,
            }

        initial["name"] = codename
        initial["category"] = _category_for(qtype)
        initial["qsys_type"] = qtype
        initial["control_count"] = len(sub_controls)

        self._component_codename[sid] = codename
        self._component_controls[sid] = sub_controls

        # Reconcile: re-register only when the schema actually changed (a
        # changed control set), so a steady reconnect doesn't churn state or
        # clobber a user's custom label.
        prev = self._component_schemas.get(sid)
        if prev is not None and prev == schema and \
                self.is_child_registered("component", sid):
            self.register_child("component", sid)  # idempotent no-op
            try:
                self.set_child_state_batch("component", sid, initial)
            except ValueError:
                pass
        else:
            if self.is_child_registered("component", sid):
                self.deregister_child("component", sid)
            self.register_child(
                "component", sid, schema=schema, initial_state=initial)
        self._component_schemas[sid] = schema

    async def _register_named_control(
        self, sid: str, name: str, hint: str | None,
    ) -> bool:
        """Query a Named Control (QRC can't enumerate them), infer its type,
        and register it as a child. Returns False if the Core reports it
        doesn't exist."""
        value: Any = None
        string = ""
        position = 0.0
        try:
            result = await self._send_jsonrpc(
                "Control.Get", [name], timeout=3.0)
        except QRCError as exc:
            if exc.code == QRC_ERR_UNKNOWN_CONTROL:
                log.warning(
                    f"[{self.device_id}] Named control {name!r} not found")
                return False
            log.warning(f"[{self.device_id}] Control.Get({name}) failed: {exc}")
            return False
        except (TimeoutError, ConnectionError, OSError) as exc:
            log.warning(f"[{self.device_id}] Control.Get({name}) failed: {exc}")
            return False

        if isinstance(result, list) and result and isinstance(result[0], dict):
            obj = result[0]
            value = obj.get("Value")
            string = str(obj.get("String", "") or "")
            try:
                position = float(obj.get("Position", 0.0) or 0.0)
            except (ValueError, TypeError):
                position = 0.0

        type_hint = hint or self._infer_nc_type(value)
        # A Named Control is a control by definition — flag its settable vars
        # so the value picker and the child_schema cascade lead with them.
        # Position is the 0..1 normalized mirror (Control.Set Position).
        schema = {
            "name": {"type": "string", "label": "Name"},
            "value": {"type": type_hint, "label": "Value", "control": True},
            "string": {"type": "string", "label": "Display"},
            "position": {"type": "number", "label": "Position",
                         "min": 0.0, "max": 1.0, "control": True},
        }
        initial = {
            "name": name,
            "value": _coerce_value(value, type_hint),
            "string": string,
            "position": position,
        }
        if initial["value"] is None:
            initial["value"] = _default_for(type_hint)

        self._named_codename[sid] = name
        if self.is_child_registered("named_control", sid):
            self.deregister_child("named_control", sid)
        self.register_child(
            "named_control", sid, schema=schema, initial_state=initial)
        self._control_route[(None, name)] = {
            "ctype": "named_control", "sid": sid, "prop": "value",
            "type": type_hint, "value_from": "value",
            "str_prop": "string", "pos_prop": "position",
        }
        return True

    @staticmethod
    def _infer_nc_type(value: Any) -> str:
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, (int, float)):
            return "number"
        return "string"

    # ── Change group + push fan-out ──

    async def _setup_change_group(self) -> None:
        """(Re)build the component + named-control change groups from the
        discovered routes and arm auto-poll. Destroys any prior groups first
        for a clean slate."""
        for gid in (MAIN_CHANGE_GROUP, NAMED_CHANGE_GROUP):
            try:
                await self._send_jsonrpc(
                    "ChangeGroup.Destroy", {"Id": gid}, expect_response=False)
            except (QRCError, TimeoutError, ConnectionError, OSError):
                pass

        rate = float(self.config.get(
            "autopoll_rate_seconds", DEFAULT_AUTOPOLL_RATE_S))
        count = 0

        # Component controls, grouped per component, in the main group.
        comp_added = 0
        for sid, controls in self._component_controls.items():
            if not controls:
                continue
            codename = self._component_codename.get(sid, sid)
            try:
                await self._send_jsonrpc(
                    "ChangeGroup.AddComponentControl",
                    {"Id": MAIN_CHANGE_GROUP, "Component": {
                        "Name": codename,
                        "Controls": [{"Name": c} for c in controls]}},
                    timeout=3.0)
                comp_added += len(controls)
            except (QRCError, TimeoutError) as exc:
                log.warning(
                    f"[{self.device_id}] AddComponentControl({codename}) "
                    f"failed: {exc}")
        if comp_added:
            count += comp_added
            await self._arm_autopoll(MAIN_CHANGE_GROUP, rate)

        # Named controls in their own group (see NAMED_CHANGE_GROUP note).
        named = list(self._named_codename.values())
        if named:
            try:
                await self._send_jsonrpc(
                    "ChangeGroup.AddControl",
                    {"Id": NAMED_CHANGE_GROUP, "Controls": named}, timeout=3.0)
                count += len(named)
                await self._arm_autopoll(NAMED_CHANGE_GROUP, rate)
            except (QRCError, TimeoutError) as exc:
                log.warning(f"[{self.device_id}] AddControl failed: {exc}")

        self._subscribed_count = count
        if count == 0:
            # Nothing to subscribe (empty/unexposed design). The health loop
            # (started in connect) keeps the socket warm so the device stays
            # connected and the integrator can fix Script Access then Re-import.
            return
        log.info(
            f"[{self.device_id}] Subscribed {count} controls (rate {rate}s)")

    async def _arm_autopoll(self, group_id: str, rate: float) -> None:
        """AutoPoll one change group and fan its first (full-state) push in."""
        try:
            initial = await self._send_jsonrpc(
                "ChangeGroup.AutoPoll", {"Id": group_id, "Rate": rate},
                timeout=3.0)
        except (QRCError, TimeoutError) as exc:
            log.warning(f"[{self.device_id}] AutoPoll({group_id}) failed: {exc}")
            return
        if isinstance(initial, dict):
            self._handle_change_group_changes(initial)

    def _handle_change_group_changes(self, payload: Any) -> None:
        """Fan a Poll/AutoPoll Changes array out into child + runtime state in
        one atomic batch."""
        if not isinstance(payload, dict):
            return
        changes = payload.get("Changes")
        if not isinstance(changes, list):
            return
        group_id = payload.get("Id")

        batch: list[tuple[str, str, dict[str, Any]]] = []
        for chg in changes:
            if not isinstance(chg, dict):
                continue
            comp = chg.get("Component")    # None for Named Controls
            cname = chg.get("Name")
            if not cname:
                continue

            # Runtime subscribe_* groups write to flat device state.
            if group_id and group_id in self._runtime_groups:
                if self._apply_runtime_change(group_id, comp, cname, chg):
                    continue

            route = self._control_route.get((comp, cname))
            if route is None:
                continue
            ctype = route["ctype"]
            sid = route["sid"]
            if not self.is_child_registered(ctype, sid):
                continue

            updates: dict[str, Any] = {}
            raw = chg.get("Value") if route["value_from"] == "value" \
                else chg.get("String")
            coerced = _coerce_value(raw, route["type"])
            if coerced is not None:
                updates[route["prop"]] = coerced
            elif route["type"] == "string":
                updates[route["prop"]] = str(chg.get("String", "") or "")
            if route["str_prop"] and "String" in chg:
                updates[route["str_prop"]] = str(chg.get("String", "") or "")
            if route["pos_prop"] and "Position" in chg:
                try:
                    updates[route["pos_prop"]] = float(chg.get("Position") or 0.0)
                except (ValueError, TypeError):
                    pass
            if updates:
                batch.append((ctype, sid, updates))

        if batch:
            try:
                self.set_children_state_batch(batch)
            except ValueError as exc:
                # A control changed schema mid-flight (rare); fall back to
                # per-child writes so one bad prop doesn't drop the batch.
                log.debug(f"[{self.device_id}] batch fan-out fallback: {exc}")
                for ctype, sid, updates in batch:
                    try:
                        self.set_child_state_batch(ctype, sid, updates)
                    except ValueError:
                        pass

    def _apply_runtime_change(
        self, group_id: str, comp: str | None, cname: str, chg: dict[str, Any],
    ) -> bool:
        for rcomp, rname, rkey, rhint in self._runtime_groups[group_id]:
            if rcomp == comp and rname == cname:
                coerced = _coerce_value(chg.get("Value"), rhint)
                if coerced is None and "String" in chg:
                    coerced = str(chg.get("String"))
                if coerced is not None:
                    self.state.set(
                        f"device.{self.device_id}.{rkey}", coerced,
                        source=f"device.{self.device_id}")
                return True
        return False

    # ── Liveness probe (BaseDriver watchdog; always on while connected) ──

    async def _liveness_probe(self) -> None:
        """NoOp the Core and await its response. The NoOp doubles as the
        REQUIRED client->core keep-alive: the Core closes any connection
        whose client has gone silent for 60 s, and AutoPoll pushes are
        core->client only so they do NOT reset that timer. The awaited
        response is the liveness signal -- a Core that vanished without a
        FIN stops answering, and the BaseDriver watchdog tears the transport
        down after consecutive misses (typed no_response fault) so the
        platform reconnects."""
        await self._send_jsonrpc("NoOp", {}, timeout=KEEPALIVE_TIMEOUT_S)

    # ── refresh_children (IDE "Refresh from Device" + reimport action) ──

    async def refresh_children(self) -> dict[str, Any]:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self._discover_topology()
        return {
            "components": len(self._component_codename),
            "named_controls": len(self._named_codename),
        }

    # ── Outgoing command dispatch ──

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None,
    ) -> Any:
        params = params or {}
        handler = _COMMAND_HANDLERS.get(command)
        if handler is None:
            log.warning(f"[{self.device_id}] Unknown command: {command}")
            return None
        return await handler(self, params)

    def _codename_for(self, child_id: Any) -> str:
        """Resolve a component child_id (sanitized local id) back to its Q-SYS
        Code Name; fall back to treating the value as a literal name."""
        s = str(child_id)
        return self._component_codename.get(s, s)

    # -- generic component / named control --

    async def _cmd_set_gain(self, params: dict[str, Any]) -> bool:
        return await self._component_set(
            self._codename_for(params["component"]), "gain",
            float(params["gain_db"]), float(params.get("ramp", 0.0) or 0.0))

    async def _cmd_set_mute(self, params: dict[str, Any]) -> bool:
        return await self._component_set(
            self._codename_for(params["component"]), "mute",
            _as_bool(params["muted"]), float(params.get("ramp", 0.0) or 0.0))

    async def _cmd_set_component_control(self, params: dict[str, Any]) -> bool:
        return await self._component_set(
            self._codename_for(params["component"]), str(params["control"]),
            _coerce_outgoing(params["value"]),
            float(params.get("ramp", 0.0) or 0.0))

    async def _cmd_get_component_control(self, params: dict[str, Any]) -> Any:
        result = await self._send_jsonrpc(
            "Component.Get",
            {"Name": self._codename_for(params["component"]),
             "Controls": [{"Name": str(params["control"])}]})
        self.set_state("last_query_result", json.dumps(result))
        return result

    async def _component_set(
        self, component: str, control: str, value: Any, ramp: float,
    ) -> bool:
        ctrl: dict[str, Any] = {"Name": control, "Value": value}
        if ramp > 0:
            ctrl["Ramp"] = ramp
        await self._send_jsonrpc(
            "Component.Set", {"Name": component, "Controls": [ctrl]})
        return True

    async def _cmd_set_named_control(self, params: dict[str, Any]) -> bool:
        return await self._named_set(
            str(params["name"]), _coerce_outgoing(params["value"]),
            float(params.get("ramp", 0.0) or 0.0))

    async def _cmd_get_named_control(self, params: dict[str, Any]) -> Any:
        result = await self._send_jsonrpc("Control.Get", [str(params["name"])])
        self.set_state("last_query_result", json.dumps(result))
        return result

    async def _cmd_trigger_named_control(self, params: dict[str, Any]) -> bool:
        # Q-SYS treats any value as a momentary fire on a trigger control.
        return await self._named_set(str(params["name"]), True, 0.0)

    async def _named_set(self, name: str, value: Any, ramp: float) -> bool:
        p: dict[str, Any] = {"Name": name, "Value": value}
        if ramp > 0:
            p["Ramp"] = ramp
        await self._send_jsonrpc("Control.Set", p)
        return True

    # -- mixer --

    async def _cmd_mixer(self, method: str, params: dict[str, Any],
                         keys: list[str], value_key: str,
                         is_bool: bool, ramp: bool) -> bool:
        body: dict[str, Any] = {"Name": self._codename_for(params["component"])}
        for k in keys:
            body[k.capitalize()] = str(params[k])   # inputs->Inputs, outputs->Outputs
        val = params[value_key]
        body["Value"] = _as_bool(val) if is_bool else float(val)
        if ramp and float(params.get("ramp", 0.0) or 0.0) > 0:
            body["Ramp"] = float(params["ramp"])
        await self._send_jsonrpc(method, body)
        return True

    # -- snapshot --

    async def _cmd_snapshot_load(self, params: dict[str, Any]) -> bool:
        p: dict[str, Any] = {
            "Name": str(params["bank_name"]), "Bank": int(params["bank"])}
        ramp = float(params.get("ramp", 0.0) or 0.0)
        if ramp > 0:
            p["Ramp"] = ramp
        await self._send_jsonrpc("Snapshot.Load", p, expect_response=False)
        return True

    async def _cmd_snapshot_save(self, params: dict[str, Any]) -> bool:
        await self._send_jsonrpc(
            "Snapshot.Save",
            {"Name": str(params["bank_name"]), "Bank": int(params["bank"])},
            expect_response=False)
        return True

    # -- loop player --

    async def _cmd_loop_start(self, params: dict[str, Any]) -> bool:
        payload: dict[str, Any] = {
            "Name": self._codename_for(params["component"]),
            "Files": [{"Name": str(params["file"]),
                       "Output": int(params.get("output", 1) or 1)}],
            "StartTime": float(params.get("start_time", -1) or -1),
            "Loop": _as_bool(params.get("loop", "false")),
            "Log": True,
        }
        seek = float(params.get("seek", 0.0) or 0.0)
        if seek > 0:
            payload["Seek"] = seek
        await self._send_jsonrpc("LoopPlayer.Start", payload, expect_response=False)
        return True

    async def _cmd_loop_stop(self, params: dict[str, Any]) -> bool:
        return await self._loop_stop(params, "Stop")

    async def _cmd_loop_cancel(self, params: dict[str, Any]) -> bool:
        return await self._loop_stop(params, "Cancel")

    async def _loop_stop(self, params: dict[str, Any], verb: str) -> bool:
        outputs = _csv_ints(params.get("outputs", "1")) or [1]
        await self._send_jsonrpc(
            f"LoopPlayer.{verb}",
            {"Name": self._codename_for(params["component"]),
             "Outputs": outputs, "Log": True}, expect_response=False)
        return True

    # -- PA router (PARAPI) --

    async def _cmd_pa_page_submit(self, params: dict[str, Any]) -> Any:
        body: dict[str, Any] = {"Mode": str(params.get("mode", "live"))}
        zones = _csv_ints(params.get("zones", ""))
        if zones:
            body["Zones"] = zones
        tags = [t for t in re.split(r"[,\s]+", str(params.get("zone_tags", "")))
                if t]
        if tags:
            body["ZoneTags"] = tags
        if str(params.get("priority", "")).strip() != "":
            body["Priority"] = int(params["priority"])
        if str(params.get("station", "")).strip() != "":
            body["Station"] = int(params["station"])
        if str(params.get("max_page_time", "")).strip() != "":
            body["MaxPageTime"] = int(params["max_page_time"])
        if str(params.get("queue_timeout", "")).strip() != "":
            body["QueueTimeout"] = int(params["queue_timeout"])
        result = await self._send_jsonrpc("PA.PageSubmit", body)
        self.set_state("last_query_result", json.dumps(result))
        return result

    async def _cmd_pa_page_start(self, params: dict[str, Any]) -> bool:
        await self._send_jsonrpc(
            "PA.PageStart", {"PageID": int(params["page_id"])},
            expect_response=False)
        return True

    async def _cmd_pa_page_stop(self, params: dict[str, Any]) -> bool:
        await self._send_jsonrpc(
            "PA.PageStop", {"PageID": int(params["page_id"])},
            expect_response=False)
        return True

    async def _cmd_pa_page_cancel(self, params: dict[str, Any]) -> bool:
        await self._send_jsonrpc(
            "PA.PageCancel", {"PageID": int(params["page_id"])},
            expect_response=False)
        return True

    # -- topology / system --

    async def _cmd_reimport_topology(self, params: dict[str, Any]) -> Any:
        await self._discover_topology()
        return {"components": len(self._component_codename),
                "named_controls": len(self._named_codename)}

    async def _cmd_mute_all(self, params: dict[str, Any]) -> int:
        return await self._mute_all(True)

    async def _cmd_unmute_all(self, params: dict[str, Any]) -> int:
        return await self._mute_all(False)

    async def _mute_all(self, muted: bool) -> int:
        """Set `mute` on every discovered component that exposes one."""
        n = 0
        for sid, codename in self._component_codename.items():
            if (codename, "mute") in self._control_route:
                try:
                    await self._component_set(codename, "mute", muted, 0.0)
                    n += 1
                except (QRCError, TimeoutError, ConnectionError, OSError) as exc:
                    log.warning(
                        f"[{self.device_id}] mute_all({codename}) failed: {exc}")
        return n

    async def _cmd_noop(self, params: dict[str, Any]) -> bool:
        await self._send_jsonrpc("NoOp", {}, expect_response=False)
        return True

    async def _cmd_send_raw(self, params: dict[str, Any]) -> Any:
        raw = str(params.get("params_json", "{}") or "")
        try:
            jp = json.loads(raw) if raw.strip() else None
        except json.JSONDecodeError as exc:
            raise ValueError(f"Bad params JSON: {exc}") from exc
        result = await self._send_jsonrpc(str(params["method"]), jp)
        self.set_state("last_query_result", json.dumps(result))
        return result

    # -- runtime change groups --

    async def _cmd_subscribe_named(self, params: dict[str, Any]) -> bool:
        group = str(params["group"])
        name = str(params["name"])
        await self._send_jsonrpc(
            "ChangeGroup.AddControl", {"Id": group, "Controls": [name]})
        await self._send_jsonrpc(
            "ChangeGroup.AutoPoll",
            {"Id": group, "Rate": float(params.get("rate_seconds", 0.5) or 0.5)})
        key = f"sub_{_safe_prop(name)}"
        self._runtime_groups.setdefault(group, []).append(
            (None, name, key, "string"))
        return True

    async def _cmd_subscribe_component(self, params: dict[str, Any]) -> bool:
        group = str(params["group"])
        comp = str(params["component"])
        ctrl = str(params["control"])
        await self._send_jsonrpc(
            "ChangeGroup.AddComponentControl",
            {"Id": group, "Component": {"Name": comp,
                                        "Controls": [{"Name": ctrl}]}})
        await self._send_jsonrpc(
            "ChangeGroup.AutoPoll",
            {"Id": group, "Rate": float(params.get("rate_seconds", 0.5) or 0.5)})
        key = f"sub_{_safe_prop(comp)}_{_safe_prop(ctrl)}"
        self._runtime_groups.setdefault(group, []).append(
            (comp, ctrl, key, "string"))
        return True

    async def _cmd_destroy_group(self, params: dict[str, Any]) -> bool:
        group = str(params["group"])
        await self._send_jsonrpc(
            "ChangeGroup.Destroy", {"Id": group}, expect_response=False)
        self._runtime_groups.pop(group, None)
        return True

    # ── Setup wizard: test connection / list components ──

    async def run_setup_action(self, action_id: str, params: dict[str, Any],
                               progress: Any) -> dict[str, Any]:
        if action_id != "test_connection":
            raise ValueError(f"Unknown setup action: {action_id}")

        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", DEFAULT_PORT))
        if not host:
            raise ValueError("No IP address configured")

        await progress(f"Connecting to {host}:{port}…", 15)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=5.0)
        except (OSError, asyncio.TimeoutError) as exc:
            raise ConnectionError(
                f"Could not reach the Core on {host}:{port} ({exc}). Check the "
                f"IP and that the design is in Run mode.") from exc

        loop = asyncio.get_event_loop()

        async def _req(method: str, p: Any, rid: int) -> dict[str, Any] | None:
            env: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
            if p is not None:
                env["params"] = p
            writer.write(json.dumps(env).encode() + FRAME_TERMINATOR)
            await writer.drain()
            buf = b""
            deadline = loop.time() + 4.0
            while loop.time() < deadline:
                try:
                    chunk = await asyncio.wait_for(reader.read(65536), timeout=2.0)
                except asyncio.TimeoutError:
                    break
                if not chunk:
                    break
                buf += chunk
                while FRAME_TERMINATOR in buf:
                    frame, buf = buf.split(FRAME_TERMINATOR, 1)
                    frame = frame.strip()
                    if not frame:
                        continue
                    try:
                        obj = json.loads(frame.decode("utf-8", "replace"))
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict) and obj.get("id") == rid:
                        return obj
            return None

        try:
            await progress("Reading Core status…", 40)
            status = await _req("StatusGet", 0, 1)
            sresult = (status or {}).get("result", {}) if status else {}
            platform = sresult.get("Platform", "?")
            design = sresult.get("DesignName", "?")

            await progress("Listing exposed components…", 70)
            comps = await _req("Component.GetComponents", None, 2)
            clist = (comps or {}).get("result", []) if comps else []
            names = [c.get("Name") for c in clist if isinstance(c, dict)]

            await progress("Done", 100)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.TimeoutError):
                pass

        hint = ""
        if not names:
            hint = ("No script-accessible Components were found. In Q-SYS "
                    "Designer, set each block's Script Access to 'External' "
                    "(or 'All') and Save to Core & Run (F5).")
        return {
            "reachable": True,
            "platform": platform,
            "design": design,
            "component_count": len(names),
            "components": names,
            "hint": hint,
        }

    # ── Polling backstop ──

    async def poll(self) -> None:
        if not self.transport or not self.transport.connected:
            return
        try:
            await self._do_status_get()
        except (QRCError, TimeoutError, ConnectionError, OSError):
            pass


# ── Command catalog + dispatch table ──

def _component_param(label: str = "Component") -> dict[str, Any]:
    return {"type": "child_id", "child_type": "component",
            "required": True, "label": label}


def _control_param(help: str | None = None) -> dict[str, Any]:
    # Cascades off the sibling `component` child_id param: picking a component
    # populates this with that component's discovered controls (the per-child
    # schema's control: true vars). Stays free-text-forgiving for controls the
    # Core hasn't reported.
    p: dict[str, Any] = {
        "type": "string", "required": True, "label": "Control Name",
        "options_from": {"param": "component", "source": "child_schema"},
    }
    if help:
        p["help"] = help
    return p


def _ramp_param() -> dict[str, Any]:
    return {"type": "number", "required": False, "label": "Ramp (sec)",
            "default": 0.0, "min": 0.0,
            "help": "Optional glide time for fader-style controls."}


def _bool_param(label: str) -> dict[str, Any]:
    return {"type": "enum", "required": True, "label": label,
            "values": ["true", "false"]}


def _build_commands() -> dict[str, dict[str, Any]]:
    spec = {"type": "string", "required": True, "default": "*", "help": _SPEC_HELP}
    gain_db = {"type": "number", "required": True, "label": "Gain (dB)",
               "min": -100, "max": 20}

    cmds: dict[str, dict[str, Any]] = {
        # -- component / named control --
        "set_gain": {
            "label": "Set Gain (dB)",
            "params": {"component": _component_param(),
                       "gain_db": gain_db, "ramp": _ramp_param()},
            "help": "Set the 'gain' control on a discovered component.",
        },
        "set_mute": {
            "label": "Set Mute",
            "params": {"component": _component_param(),
                       "muted": _bool_param("Muted"), "ramp": _ramp_param()},
            "help": "Set the 'mute' control on a discovered component.",
        },
        "set_component_control": {
            "label": "Set Component Control",
            "params": {
                "component": _component_param(),
                "control": _control_param(
                    "QRC control name, e.g. gain, mute, input.1.gain. Pick the "
                    "component above to list its controls, or type one."),
                # The Value field follows the picked control's type (a number
                # spinner with the gain's range, Yes/No for a mute, etc.).
                "value": {"type": "string", "required": True, "label": "Value",
                          "type_from": {"param": "control"},
                          "help": "Numbers and true/false are auto-typed."},
                "ramp": _ramp_param()},
            "help": "Set any control on any discovered component (escape "
                    "hatch).",
        },
        "get_component_control": {
            "label": "Get Component Control",
            "params": {"component": _component_param(),
                       "control": _control_param()},
            "help": "Read a control; result lands in last_query_result.",
        },
        "set_named_control": {
            "label": "Set Named Control",
            "params": {
                "name": {"type": "string", "required": True, "label": "Name"},
                "value": {"type": "string", "required": True, "label": "Value"},
                "ramp": _ramp_param()},
            "help": "Set a Q-SYS Named Control by name.",
        },
        "get_named_control": {
            "label": "Get Named Control",
            "params": {"name": {"type": "string", "required": True,
                                "label": "Name"}},
        },
        "trigger_named_control": {
            "label": "Trigger Named Control",
            "params": {"name": {"type": "string", "required": True,
                                "label": "Name"}},
            "help": "Fire a momentary (button-style) Named Control.",
        },
        # -- mixer --
        "mixer_set_input_gain": {
            "label": "Mixer: Set Input Gain (dB)",
            "params": {"component": _component_param("Mixer"),
                       "inputs": {**spec, "label": "Inputs"},
                       "gain_db": gain_db, "ramp": _ramp_param()},
        },
        "mixer_set_input_mute": {
            "label": "Mixer: Set Input Mute",
            "params": {"component": _component_param("Mixer"),
                       "inputs": {**spec, "label": "Inputs"},
                       "muted": _bool_param("Muted"), "ramp": _ramp_param()},
        },
        "mixer_set_input_solo": {
            "label": "Mixer: Set Input Solo",
            "params": {"component": _component_param("Mixer"),
                       "inputs": {**spec, "label": "Inputs"},
                       "solo": _bool_param("Solo")},
        },
        "mixer_set_output_gain": {
            "label": "Mixer: Set Output Gain (dB)",
            "params": {"component": _component_param("Mixer"),
                       "outputs": {**spec, "label": "Outputs"},
                       "gain_db": gain_db, "ramp": _ramp_param()},
        },
        "mixer_set_output_mute": {
            "label": "Mixer: Set Output Mute",
            "params": {"component": _component_param("Mixer"),
                       "outputs": {**spec, "label": "Outputs"},
                       "muted": _bool_param("Muted"), "ramp": _ramp_param()},
        },
        "mixer_set_crosspoint_gain": {
            "label": "Mixer: Set Crosspoint Gain (dB)",
            "params": {"component": _component_param("Mixer"),
                       "inputs": {**spec, "label": "Inputs"},
                       "outputs": {**spec, "label": "Outputs"},
                       "gain_db": gain_db, "ramp": _ramp_param()},
        },
        "mixer_set_crosspoint_mute": {
            "label": "Mixer: Set Crosspoint Mute",
            "params": {"component": _component_param("Mixer"),
                       "inputs": {**spec, "label": "Inputs"},
                       "outputs": {**spec, "label": "Outputs"},
                       "muted": _bool_param("Muted")},
        },
        "mixer_set_crosspoint_solo": {
            "label": "Mixer: Set Crosspoint Solo",
            "params": {"component": _component_param("Mixer"),
                       "inputs": {**spec, "label": "Inputs"},
                       "outputs": {**spec, "label": "Outputs"},
                       "solo": _bool_param("Solo")},
        },
        # -- snapshot --
        "snapshot_load": {
            "label": "Recall Snapshot",
            "params": {
                "bank_name": {"type": "string", "required": True,
                              "label": "Snapshot Bank Name",
                              "options_state": "snapshot_banks",
                              "help": "The snapshot bank's name in Q-SYS "
                                      "Designer (the controller's Code Name). "
                                      "Discovered banks are offered as a list."},
                "bank": {"type": "integer", "required": True,
                         "label": "Snapshot Number", "min": 1, "max": 24},
                "ramp": _ramp_param()},
        },
        "snapshot_save": {
            "label": "Save Snapshot",
            "params": {
                "bank_name": {"type": "string", "required": True,
                              "label": "Snapshot Bank Name",
                              "options_state": "snapshot_banks"},
                "bank": {"type": "integer", "required": True,
                         "label": "Snapshot Number", "min": 1, "max": 24}},
            "help": "Save current control values into a snapshot slot.",
        },
        # -- loop player --
        "loop_player_start": {
            "label": "Loop Player: Start",
            "params": {
                "component": _component_param("Loop Player"),
                "file": {"type": "string", "required": True, "label": "File",
                         "help": "Path on the Core, e.g. Audio/loop.wav."},
                "output": {"type": "integer", "required": False,
                           "label": "Output", "default": 1, "min": 1,
                           "help": "Track/output number; count is design-dependent."},
                "loop": {**_bool_param("Loop"), "required": False,
                         "default": "false"},
                "start_time": {"type": "number", "required": False,
                               "label": "Start Time", "default": -1,
                               "help": "-1 = now; -2 = queue after current; "
                                       ">0 = absolute time-of-day (sec)."},
                "seek": {"type": "number", "required": False, "label": "Seek (sec)",
                         "default": 0.0, "min": 0.0}},
        },
        "loop_player_stop": {
            "label": "Loop Player: Stop",
            "params": {"component": _component_param("Loop Player"),
                       "outputs": {"type": "string", "required": False,
                                   "label": "Outputs", "default": "1",
                                   "help": "Comma-separated, e.g. 1,3,4."}},
        },
        "loop_player_cancel": {
            "label": "Loop Player: Cancel Queued",
            "params": {"component": _component_param("Loop Player"),
                       "outputs": {"type": "string", "required": False,
                                   "label": "Outputs", "default": "1"}},
        },
        # -- PA router (PARAPI) --
        "pa_page_submit": {
            "label": "PA: Submit Page",
            "params": {
                "mode": {"type": "enum", "required": False, "label": "Mode",
                         "values": ["live", "delay", "auto", "message"],
                         "default": "live"},
                "zones": {"type": "string", "required": False, "label": "Zones",
                          "help": "Comma-separated zone numbers."},
                "zone_tags": {"type": "string", "required": False,
                              "label": "Zone Tags",
                              "help": "Comma/space-separated zone tag names."},
                "priority": {"type": "integer", "required": False,
                             "label": "Priority", "min": 1,
                             "help": "1 = highest; larger numbers are lower priority."},
                "station": {"type": "integer", "required": False,
                            "label": "Station",
                            "help": "PA Router input station number."},
                "max_page_time": {"type": "integer", "required": False,
                                  "label": "Max Page Time (sec)"},
                "queue_timeout": {"type": "integer", "required": False,
                                  "label": "Queue Timeout (sec)"}},
            "help": "Submit a PA Router page request (PARAPI). Returns a "
                    "PageID in last_query_result.",
        },
        "pa_page_start": {
            "label": "PA: Start Page",
            "params": {"page_id": {"type": "integer", "required": True,
                                   "label": "Page ID"}},
        },
        "pa_page_stop": {
            "label": "PA: Stop Page",
            "params": {"page_id": {"type": "integer", "required": True,
                                   "label": "Page ID"}},
        },
        "pa_page_cancel": {
            "label": "PA: Cancel Page",
            "params": {"page_id": {"type": "integer", "required": True,
                                   "label": "Page ID"}},
        },
        # -- topology / system --
        "reimport_topology": {
            "label": "Re-import Topology",
            "params": {},
            "help": "Re-read the Core's Components and controls (run after "
                    "changing the Q-SYS design).",
        },
        "mute_all": {
            "label": "Mute All",
            "params": {},
            "help": "Set mute on every discovered component that has a mute "
                    "control.",
        },
        "unmute_all": {
            "label": "Unmute All",
            "params": {},
        },
        "noop": {
            "label": "Send NoOp (keep-alive)",
            "params": {},
        },
        "send_raw_jsonrpc": {
            "label": "Send Raw JSON-RPC",
            "params": {
                "method": {"type": "string", "required": True, "label": "Method"},
                "params_json": {"type": "string", "required": False,
                                "label": "Params (JSON)", "default": "{}",
                                "help": "JSON object or array; empty for none."}},
            "help": "Last-resort escape hatch for anything the typed commands "
                    "don't cover.",
        },
        # -- runtime change groups --
        "subscribe_named_control": {
            "label": "Subscribe to Named Control (runtime)",
            "params": {
                "group": {"type": "string", "required": True,
                          "label": "Change Group ID", "default": "user_group_1",
                          "help": "Up to 4 change groups per connection."},
                "name": {"type": "string", "required": True, "label": "Name"},
                "rate_seconds": {"type": "number", "required": False,
                                 "label": "Rate (sec)", "default": 0.5,
                                 "min": 0.05, "max": 60.0}},
            "help": "Add a Named Control to a runtime change group; updates "
                    "land in device.<id>.sub_<name>.",
        },
        "subscribe_component_control": {
            "label": "Subscribe to Component Control (runtime)",
            "params": {
                "group": {"type": "string", "required": True,
                          "label": "Change Group ID", "default": "user_group_1"},
                "component": {"type": "string", "required": True,
                              "label": "Component Code Name"},
                "control": {"type": "string", "required": True,
                            "label": "Control Name"},
                "rate_seconds": {"type": "number", "required": False,
                                 "label": "Rate (sec)", "default": 0.5,
                                 "min": 0.05, "max": 60.0}},
        },
        "destroy_change_group": {
            "label": "Destroy Change Group (runtime)",
            "params": {"group": {"type": "string", "required": True,
                                 "label": "Change Group ID"}},
            "help": "Free a change-group slot.",
        },
    }
    return cmds


def _mixer_handler(method: str, keys: list[str], value_key: str,
                   is_bool: bool, ramp: bool):
    async def _h(self: "QSCQRCDriver", params: dict[str, Any]) -> bool:
        return await self._cmd_mixer(method, params, keys, value_key, is_bool, ramp)
    return _h


# command id -> bound coroutine on the driver instance. Built once at import;
# send_command looks each up by name.
_COMMAND_HANDLERS: dict[str, Any] = {
    "set_gain": QSCQRCDriver._cmd_set_gain,
    "set_mute": QSCQRCDriver._cmd_set_mute,
    "set_component_control": QSCQRCDriver._cmd_set_component_control,
    "get_component_control": QSCQRCDriver._cmd_get_component_control,
    "set_named_control": QSCQRCDriver._cmd_set_named_control,
    "get_named_control": QSCQRCDriver._cmd_get_named_control,
    "trigger_named_control": QSCQRCDriver._cmd_trigger_named_control,
    "mixer_set_input_gain": _mixer_handler(
        "Mixer.SetInputGain", ["inputs"], "gain_db", False, True),
    "mixer_set_input_mute": _mixer_handler(
        "Mixer.SetInputMute", ["inputs"], "muted", True, True),
    "mixer_set_input_solo": _mixer_handler(
        "Mixer.SetInputSolo", ["inputs"], "solo", True, False),
    "mixer_set_output_gain": _mixer_handler(
        "Mixer.SetOutputGain", ["outputs"], "gain_db", False, True),
    "mixer_set_output_mute": _mixer_handler(
        "Mixer.SetOutputMute", ["outputs"], "muted", True, False),
    "mixer_set_crosspoint_gain": _mixer_handler(
        "Mixer.SetCrossPointGain", ["inputs", "outputs"], "gain_db", False, True),
    "mixer_set_crosspoint_mute": _mixer_handler(
        "Mixer.SetCrossPointMute", ["inputs", "outputs"], "muted", True, False),
    "mixer_set_crosspoint_solo": _mixer_handler(
        "Mixer.SetCrossPointSolo", ["inputs", "outputs"], "solo", True, False),
    "snapshot_load": QSCQRCDriver._cmd_snapshot_load,
    "snapshot_save": QSCQRCDriver._cmd_snapshot_save,
    "loop_player_start": QSCQRCDriver._cmd_loop_start,
    "loop_player_stop": QSCQRCDriver._cmd_loop_stop,
    "loop_player_cancel": QSCQRCDriver._cmd_loop_cancel,
    "pa_page_submit": QSCQRCDriver._cmd_pa_page_submit,
    "pa_page_start": QSCQRCDriver._cmd_pa_page_start,
    "pa_page_stop": QSCQRCDriver._cmd_pa_page_stop,
    "pa_page_cancel": QSCQRCDriver._cmd_pa_page_cancel,
    "reimport_topology": QSCQRCDriver._cmd_reimport_topology,
    "mute_all": QSCQRCDriver._cmd_mute_all,
    "unmute_all": QSCQRCDriver._cmd_unmute_all,
    "noop": QSCQRCDriver._cmd_noop,
    "send_raw_jsonrpc": QSCQRCDriver._cmd_send_raw,
    "subscribe_named_control": QSCQRCDriver._cmd_subscribe_named,
    "subscribe_component_control": QSCQRCDriver._cmd_subscribe_component,
    "destroy_change_group": QSCQRCDriver._cmd_destroy_group,
}


class QRCError(RuntimeError):
    """Raised when the Core returns a JSON-RPC error response."""

    def __init__(self, error: Any) -> None:
        if isinstance(error, dict):
            self.code = error.get("code")
            self.message = error.get("message", "")
            super().__init__(f"QRC error {self.code}: {self.message}")
        else:
            self.code = None
            self.message = str(error)
            super().__init__(f"QRC error: {self.message}")
