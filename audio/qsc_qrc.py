"""
QSC Q-SYS QRC Driver.

Controls QSC Q-SYS Cores over the Q-SYS Remote Control protocol (QRC) on
TCP port 1710. QRC is JSON-RPC 2.0 with null-terminated frames, the
modern replacement for QSC's older External Control Protocol (ECP, port
1702). QSC's docs explicitly recommend QRC over ECP — ECP requires every
controlled value to be exposed as a Named Control in Q-SYS Designer,
while QRC reaches into Components, Mixers, Snapshots, and the PA Router
without that.

Coverage:
    - Named Controls (typed: number / boolean / string / trigger)
    - Component gain / mute / generic component pass-through
    - Mixer block (per-input mute/gain, per-output mute/gain, crosspoint
      gain/mute, ramp)
    - Audio Router (per-output source select)
    - Snapshot bank (load/save by index, optional ramp)
    - Loop Player (start, stop, cancel)
    - PA Router (page submit, cancel — light pass; full PARAPI deferred)
    - Read-only meter
    - Optional Logon authentication

Why Python (rules.md Principle 9):
    Three independent reasons, any one of which forces it:
    1) JSON-RPC 2.0 framing with null-terminator and request/response
       correlation by `id` — ConfigurableDriver's line-delimited text
       dispatch can't express this.
    2) Per-instance dynamic state vars / commands from a user-declared
       topology (same Symetrix Composer / Biamp Tesira pattern).
    3) Change-group push: one incoming `Changes` array fans out into N
       state vars; needs a (Component, Name) -> state-var map that
       YAML's response patterns can't reach.

Why subscriptions, not polling (rules.md Principle 2):
    QRC supports change groups. The driver registers every declared
    control into a single auto-poll change group ("openavc_main") on
    connect, then reacts to the periodic `ChangeGroup.Poll` results
    pushed by the Core. Polling is also AutoPoll's keep-alive mechanism
    (the Core closes any session quiet for >60s), so this kills two
    birds with one wire.

    Q-SYS imposes a hard limit of 4 change groups per connection. The
    driver claims one for declared blocks, leaving 3 for runtime
    `subscribe_*` commands to use freely.

Sources:
    - https://q-syshelp.qsc.com/Content/External_Control_APIs/QRC/QRC_Overview.htm
    - https://q-syshelp.qsc.com/Content/External_Control_APIs/QRC/QRC_Commands.htm

Out of scope (deferred, documented in setup help — not silently dropped):
    - TLS / SSL transport (1710-over-TLS)
    - Redundant-Core dual-connection routing
    - Q-SYS Reflect cloud
    - Q-SYS Lua plugin SDK (a different surface entirely)
    - Full PARAPI deep paging (light pass only — PageSubmit / PageCancel)
    - Auth tracked under "Drivers shipped Python pending auth-extension
      support" in driver-roadmap/rules.md as a Python driver that uses
      jsonrpc_login but is also Python for other reasons.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from server.drivers.base import BaseDriver
from server.utils.logger import get_logger

log = get_logger(__name__)


# ── Wire-protocol constants ──

# QRC default port. Q-SYS Designer also exposes QRC in Emulate mode on
# the same port.
DEFAULT_PORT = 1710

# Each JSON-RPC frame is terminated by a single 0x00 byte, not a
# newline. The TCP transport's DelimiterFrameParser handles this for
# us when we hand it `delimiter=b"\x00"`.
FRAME_TERMINATOR = b"\x00"

# Default change-group ID used for all declared blocks. Q-SYS limits
# concurrent change groups to 4, so we deliberately use just one for
# the declared topology and leave the other three for runtime
# subscribe_* commands.
MAIN_CHANGE_GROUP = "openavc_main"

# Auto-poll rate in seconds. 0.25s matches the Tesira default and is
# Q-SYS's documented sweet spot — fast enough for fader bargraphs, slow
# enough not to flood the wire on big change groups.
DEFAULT_AUTOPOLL_RATE_S = 0.25

# Per-method id allocator floor. We start at 1 and increment; the
# documented protocol just requires id to be a number that round-trips,
# so any int works.
INITIAL_REQUEST_ID = 1

# Inter-command delay default. QRC's parser is JSON-RPC and has no
# documented per-command rate-limit; we keep a tiny default to space
# out the on-connect AddControl burst on small Cores.
DEFAULT_INTER_COMMAND_DELAY = 0.02

# QRC error codes (negative codes are JSON-RPC standard, positive ones
# are QSC-specific). We surface the message in last_error for visibility.
QRC_ERROR_LOGON_REQUIRED = 10
QRC_ERROR_UNKNOWN_COMPONENT = 7
QRC_ERROR_UNKNOWN_CONTROL = 8

# Block-type vocabulary the textarea grammar accepts. Keys are
# user-visible type tokens (case-insensitive on parse). Values describe
# how to expand the block into state vars, change-group entries, and
# per-block commands.
BLOCK_TYPES = {
    "named_control",
    "nc",                # alias
    "gain",              # Component gain block
    "mute",              # Component mute block
    "mixer",
    "router",
    "snapshot",
    "loop_player",
    "pa_zone",
    "meter",
    "component",         # opaque pass-through; subscribes to all controls
}

# Named-control type hints the user can declare in the third column.
NC_TYPE_HINTS = {"number", "boolean", "string", "trigger"}


# ── Block-list parsing ──

def _safe_token(s: str) -> str:
    """Sanitize a Q-SYS code-name for use as a state-key segment.

    Q-SYS code names can contain spaces, dots, and other punctuation
    (the integrator picks them in Designer). We collapse anything
    outside [A-Za-z0-9_] to underscore so resulting state keys are
    grep-able and don't collide with the platform's key format.
    """
    return re.sub(r"[^A-Za-z0-9_]", "_", s)


def _parse_matrix_spec(spec: str | None) -> tuple[int, int]:
    """Parse '8x4' -> (8, 4). Returns (0, 0) for invalid input."""
    if not spec:
        return 0, 0
    m = re.match(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$", spec)
    if not m:
        return 0, 0
    return int(m.group(1)), int(m.group(2))


def _parse_int_spec(spec: str | None, default: int = 1) -> int:
    if not spec:
        return default
    try:
        return int(spec)
    except ValueError:
        return default


def parse_blocks_config(text: str) -> list[dict[str, Any]]:
    """Parse the `blocks` textarea into a list of block dicts.

    Format: one block per line, whitespace-separated.
        <CODE_NAME> <BLOCK_TYPE> [SPEC]

    Comments start with '#' or ';'. Blank lines are ignored.

    Returns a list of dicts:
        {"name": "MainGain", "type": "gain", "spec": "", "extra": {...}}

    For mixers, "extra" carries {"inputs": 8, "outputs": 4}. For
    routers, {"outputs": 4}. For snapshot banks, {"banks": 8}. For
    named_control, {"value_type": "number"|"boolean"|"string"|"trigger"}.
    Unknown block types are returned with type="unknown" so the driver
    can log a warning rather than crashing on a typo.
    """
    blocks: list[dict[str, Any]] = []
    if not text:
        return blocks

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue

        # Strip inline comments after the body, but only when preceded
        # by whitespace so '#' inside a code name (rare but possible)
        # stays put.
        comment_idx = -1
        for marker in (" #", "\t#", " ;", "\t;"):
            idx = line.find(marker)
            if idx >= 0 and (comment_idx < 0 or idx < comment_idx):
                comment_idx = idx
        if comment_idx >= 0:
            line = line[:comment_idx].strip()
        if not line:
            continue

        parts = line.split()
        if len(parts) < 2:
            log.warning(f"QSC blocks parse: line too short, skipping: {raw_line!r}")
            continue

        name = parts[0]
        type_token = parts[1].lower()
        spec = parts[2] if len(parts) >= 3 else ""

        if type_token not in BLOCK_TYPES:
            log.warning(
                f"QSC blocks parse: unknown block type {type_token!r} on "
                f"line {raw_line!r}; valid types: {sorted(BLOCK_TYPES)}"
            )
            blocks.append({
                "name": name, "type": "unknown", "raw_type": type_token,
                "spec": spec, "extra": {},
            })
            continue

        canonical_type = "named_control" if type_token == "nc" else type_token
        block: dict[str, Any] = {
            "name": name, "type": canonical_type, "spec": spec, "extra": {},
        }

        if canonical_type == "mixer":
            inputs, outputs = _parse_matrix_spec(spec)
            if inputs == 0 or outputs == 0:
                inputs, outputs = 4, 4
                log.warning(
                    f"QSC blocks parse: mixer {name!r} missing NxM spec, "
                    f"defaulting to 4x4"
                )
            block["extra"] = {"inputs": inputs, "outputs": outputs}
        elif canonical_type == "router":
            block["extra"] = {"outputs": _parse_int_spec(spec, 4)}
        elif canonical_type == "snapshot":
            block["extra"] = {"banks": _parse_int_spec(spec, 8)}
        elif canonical_type == "named_control":
            value_type = (spec or "number").lower()
            if value_type not in NC_TYPE_HINTS:
                log.warning(
                    f"QSC blocks parse: named_control {name!r} unknown type "
                    f"{value_type!r}, defaulting to number"
                )
                value_type = "number"
            block["extra"] = {"value_type": value_type}

        blocks.append(block)

    return blocks


# ── State-variable / change-group / command expansion ──

# Common Q-SYS Component control names by block type. These match the
# names Q-SYS Designer exposes on stock blocks; integrators using
# custom plugins or renamed properties should use type=component
# instead.
GAIN_CONTROLS = {
    "gain": "number",
    "mute": "boolean",
    "invert": "boolean",
    "bypass": "boolean",
}
MUTE_CONTROLS = {
    "mute": "boolean",
    "bypass": "boolean",
}
METER_CONTROLS = {
    # The "Audio Meter" / "Peak/RMS Meter" components in Q-SYS expose
    # `meter.1`, `meter.2`, ... — but those names vary by component
    # type. We model the simplest one-channel surface; multi-channel
    # users should declare a `component` block to enumerate dynamically.
    "meter.1": "number",
}


def _state_var(block: dict[str, Any], suffix: str) -> str:
    return f"{_safe_token(block['name'])}_{suffix}"


def expand_state_variables(
    blocks: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Build a state_variables dict for the driver, given parsed blocks.

    Always includes a small set of system-level state vars. Then
    appends per-block state vars based on type.
    """
    out: dict[str, dict[str, Any]] = {
        "core_state": {
            "type": "string",
            "label": "Core State",
            "help": "EngineStatus from the Q-SYS Core: Idle / Active / Standby.",
        },
        "core_platform": {"type": "string", "label": "Core Model"},
        "core_design_name": {"type": "string", "label": "Running Design"},
        "core_design_code": {"type": "string", "label": "Design Code"},
        "core_is_redundant": {"type": "boolean", "label": "Redundant Pair"},
        "core_is_emulator": {"type": "boolean", "label": "Emulator Mode"},
        "core_status_string": {"type": "string", "label": "Core Status Text"},
        "logon_ok": {
            "type": "boolean",
            "label": "Logon Successful",
            "help": "True after a successful Logon, or when no auth is required.",
        },
        "last_error": {
            "type": "string",
            "label": "Last QRC Error",
            "help": "Most recent JSON-RPC error message from the Core.",
        },
        "last_query_result": {
            "type": "string",
            "label": "Last Get Result",
            "help": "JSON of the last raw get result (for debugging).",
        },
    }

    for blk in blocks:
        bt = blk["type"]
        name = blk["name"]
        extra = blk["extra"]

        if bt == "named_control":
            value_type = extra.get("value_type", "number")
            if value_type == "trigger":
                # Triggers are write-only in Q-SYS — no state var.
                continue
            state_type = {
                "number": "number",
                "boolean": "boolean",
                "string": "string",
            }.get(value_type, "string")
            out[_state_var(blk, "value")] = {
                "type": state_type,
                "label": f"{name} (Named Control)",
            }
            out[_state_var(blk, "string")] = {
                "type": "string",
                "label": f"{name} (formatted string)",
                "help": "Q-SYS's formatted display string for this control "
                        "(e.g., '-12.0dB', 'muted'). Updated alongside the "
                        "raw value.",
            }
        elif bt == "gain":
            out[_state_var(blk, "gain")] = {
                "type": "number", "label": f"{name} Gain (dB)",
                "min": -100, "max": 20,
            }
            out[_state_var(blk, "gain_string")] = {
                "type": "string", "label": f"{name} Gain (display)",
            }
            out[_state_var(blk, "mute")] = {
                "type": "boolean", "label": f"{name} Mute",
            }
            out[_state_var(blk, "invert")] = {
                "type": "boolean", "label": f"{name} Polarity Invert",
            }
            out[_state_var(blk, "bypass")] = {
                "type": "boolean", "label": f"{name} Bypass",
            }
        elif bt == "mute":
            out[_state_var(blk, "mute")] = {
                "type": "boolean", "label": f"{name} Mute",
            }
            out[_state_var(blk, "bypass")] = {
                "type": "boolean", "label": f"{name} Bypass",
            }
        elif bt == "mixer":
            inputs = extra.get("inputs", 4)
            outputs = extra.get("outputs", 4)
            for i in range(1, inputs + 1):
                out[_state_var(blk, f"input_mute_{i}")] = {
                    "type": "boolean", "label": f"{name} Input {i} Mute",
                }
                out[_state_var(blk, f"input_gain_{i}")] = {
                    "type": "number", "label": f"{name} Input {i} Gain (dB)",
                    "min": -100, "max": 20,
                }
            for o in range(1, outputs + 1):
                out[_state_var(blk, f"output_mute_{o}")] = {
                    "type": "boolean", "label": f"{name} Output {o} Mute",
                }
                out[_state_var(blk, f"output_gain_{o}")] = {
                    "type": "number", "label": f"{name} Output {o} Gain (dB)",
                    "min": -100, "max": 20,
                }
            # Per-crosspoint state grid. Skip on huge mixers — set/get
            # commands still work, runtime subscribes are the escape
            # hatch.
            if inputs * outputs <= 64:
                for i in range(1, inputs + 1):
                    for o in range(1, outputs + 1):
                        out[_state_var(blk, f"xpoint_mute_{i}_{o}")] = {
                            "type": "boolean",
                            "label": f"{name} XP {i}->{o} Mute",
                        }
                        out[_state_var(blk, f"xpoint_gain_{i}_{o}")] = {
                            "type": "number",
                            "label": f"{name} XP {i}->{o} Gain (dB)",
                            "min": -100, "max": 20,
                        }
        elif bt == "router":
            outputs = extra.get("outputs", 4)
            for o in range(1, outputs + 1):
                out[_state_var(blk, f"output_{o}")] = {
                    "type": "integer",
                    "label": f"{name} Output {o} Source",
                    "min": 0, "max": 256,
                    "help": "0 = no source selected; 1..N = source index.",
                }
        elif bt == "snapshot":
            out[_state_var(blk, "last_loaded")] = {
                "type": "integer",
                "label": f"{name} Last Loaded Snapshot",
                "min": 0,
            }
        elif bt == "loop_player":
            out[_state_var(blk, "playing")] = {
                "type": "boolean", "label": f"{name} Playing",
            }
            out[_state_var(blk, "last_file")] = {
                "type": "string", "label": f"{name} Last File",
            }
        elif bt == "pa_zone":
            out[_state_var(blk, "page_status")] = {
                "type": "string",
                "label": f"{name} Page Status",
                "help": "Last page status reported by the PA Router.",
            }
        elif bt == "meter":
            for ctrl_name in METER_CONTROLS:
                key = _safe_token(ctrl_name)
                out[_state_var(blk, key)] = {
                    "type": "number", "label": f"{name} {ctrl_name} (dB)",
                }
        # 'component' is a runtime expand — populated after
        # Component.GetControls returns. We declare a placeholder so
        # writes don't fail validation.
        elif bt == "component":
            out[_state_var(blk, "_components_loaded")] = {
                "type": "boolean",
                "label": f"{name} Controls Loaded",
                "help": "True after Component.GetControls completes for this "
                        "block on connect. Per-control state vars are "
                        "registered dynamically; bind via "
                        f"device.<id>.{_safe_token(name)}_<control_name>.",
            }

    return out


def expand_change_group_entries(
    blocks: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Build the list of (Named Controls, Component-control specs) we
    register into the main change group on connect.

    Returns a tuple:
        named_controls: list of Named Control name strings
        component_controls: list of {"name": str, "controls": [str, ...]}

    Blocks of type=component are NOT expanded here — they're handled
    after `Component.GetControls` resolves at runtime, since we don't
    know the control list until the Core tells us.
    """
    named: list[str] = []
    component: list[dict[str, Any]] = []

    for blk in blocks:
        bt = blk["type"]
        name = blk["name"]
        extra = blk["extra"]

        if bt == "named_control":
            if extra.get("value_type") != "trigger":
                # Triggers are write-only.
                named.append(name)
        elif bt == "gain":
            component.append({
                "name": name, "controls": list(GAIN_CONTROLS.keys()),
            })
        elif bt == "mute":
            component.append({
                "name": name, "controls": list(MUTE_CONTROLS.keys()),
            })
        elif bt == "mixer":
            inputs = extra.get("inputs", 4)
            outputs = extra.get("outputs", 4)
            controls: list[str] = []
            for i in range(1, inputs + 1):
                controls.append(f"input.{i}.mute")
                controls.append(f"input.{i}.gain")
            for o in range(1, outputs + 1):
                controls.append(f"output.{o}.mute")
                controls.append(f"output.{o}.gain")
            if inputs * outputs <= 64:
                for i in range(1, inputs + 1):
                    for o in range(1, outputs + 1):
                        controls.append(f"input.{i}.output.{o}.mute")
                        controls.append(f"input.{i}.output.{o}.gain")
            component.append({"name": name, "controls": controls})
        elif bt == "router":
            outputs = extra.get("outputs", 4)
            controls = [f"select.{o}" for o in range(1, outputs + 1)]
            component.append({"name": name, "controls": controls})
        elif bt == "meter":
            component.append({
                "name": name, "controls": list(METER_CONTROLS.keys()),
            })
        # snapshot / loop_player / pa_zone don't have routine state to
        # subscribe to — they're command-driven. component blocks
        # register their controls dynamically after GetControls.

    return named, component


def expand_commands(
    blocks: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Build the per-instance commands dict.

    Always includes generic escape hatches. Per-block convenience
    commands are added for each declared block so the Programmer IDE's
    macros editor surfaces them by name.
    """
    cmds: dict[str, dict[str, Any]] = {
        "set_named_control": {
            "label": "Set Named Control (raw)",
            "params": {
                "name": {"type": "string", "required": True, "label": "Name"},
                "value": {"type": "string", "required": True, "label": "Value",
                          "help": "Numbers and booleans are auto-coerced; "
                                  "anything else passes through as a string."},
                "ramp": {"type": "number", "required": False, "label": "Ramp (sec)",
                         "default": 0.0, "min": 0.0,
                         "help": "Optional ramp time for fader-style controls."},
            },
            "help": "Set a Q-SYS Named Control by name. Use this for any "
                    "Named Control not declared in the block list.",
        },
        "get_named_control": {
            "label": "Get Named Control (raw)",
            "params": {
                "name": {"type": "string", "required": True, "label": "Name"},
            },
            "help": "Read a Named Control. Result lands in last_query_result.",
        },
        "trigger_named_control": {
            "label": "Trigger Named Control",
            "params": {
                "name": {"type": "string", "required": True, "label": "Name"},
            },
            "help": "Fire a trigger Named Control (button-style controls). "
                    "Q-SYS triggers are momentary — no value is held.",
        },
        "set_component_control": {
            "label": "Set Component Control (raw)",
            "params": {
                "component": {"type": "string", "required": True, "label": "Component Name"},
                "control": {"type": "string", "required": True, "label": "Control Name"},
                "value": {"type": "string", "required": True, "label": "Value"},
                "ramp": {"type": "number", "required": False, "label": "Ramp (sec)",
                         "default": 0.0, "min": 0.0},
            },
            "help": "Set any control on any Q-SYS Component by code name.",
        },
        "get_component_control": {
            "label": "Get Component Control (raw)",
            "params": {
                "component": {"type": "string", "required": True, "label": "Component Name"},
                "control": {"type": "string", "required": True, "label": "Control Name"},
            },
        },
        "recall_snapshot": {
            "label": "Recall Snapshot",
            "params": {
                "bank_name": {"type": "string", "required": True, "label": "Snapshot Bank Name",
                              "help": "Name of the snapshot bank as defined "
                                      "in Q-SYS Designer."},
                "bank": {"type": "integer", "required": True, "label": "Snapshot Number",
                         "min": 1, "max": 64,
                         "help": "Snapshot index within the bank (1-based)."},
                "ramp": {"type": "number", "required": False, "label": "Ramp (sec)",
                         "default": 0.0, "min": 0.0},
            },
        },
        "save_snapshot": {
            "label": "Save Snapshot",
            "params": {
                "bank_name": {"type": "string", "required": True, "label": "Snapshot Bank Name"},
                "bank": {"type": "integer", "required": True, "label": "Snapshot Number",
                         "min": 1, "max": 64},
            },
            "help": "Saves the current state into the specified snapshot slot.",
        },
        "send_raw_jsonrpc": {
            "label": "Send Raw JSON-RPC",
            "params": {
                "method": {"type": "string", "required": True, "label": "Method"},
                "params_json": {"type": "string", "required": False,
                                "label": "Params (JSON)", "default": "{}",
                                "help": "JSON object or array. Empty for no params."},
            },
            "help": "Last-resort escape hatch — covers anything the typed "
                    "commands don't.",
        },
        "subscribe_named_control": {
            "label": "Subscribe to Named Control (runtime)",
            "params": {
                "group": {"type": "string", "required": True, "label": "Change Group ID",
                          "default": "user_group_1",
                          "help": "Pick any name; controls in the same group "
                                  "share an auto-poll. Q-SYS allows up to "
                                  "4 change groups per connection."},
                "name": {"type": "string", "required": True, "label": "Control Name"},
                "rate_seconds": {"type": "number", "required": False,
                                 "label": "Auto-Poll Rate (sec)", "default": 0.5,
                                 "min": 0.05, "max": 60.0},
            },
            "help": "Add a Named Control to a runtime change group with "
                    "auto-poll. Updates land in device.<id>.<safe_name>_value.",
        },
        "subscribe_component_control": {
            "label": "Subscribe to Component Control (runtime)",
            "params": {
                "group": {"type": "string", "required": True, "label": "Change Group ID",
                          "default": "user_group_1"},
                "component": {"type": "string", "required": True, "label": "Component Name"},
                "control": {"type": "string", "required": True, "label": "Control Name"},
                "rate_seconds": {"type": "number", "required": False,
                                 "label": "Auto-Poll Rate (sec)", "default": 0.5,
                                 "min": 0.05, "max": 60.0},
            },
        },
        "destroy_change_group": {
            "label": "Destroy Change Group (runtime)",
            "params": {
                "group": {"type": "string", "required": True, "label": "Change Group ID"},
            },
            "help": "Frees a change-group slot. Useful if you've hit the "
                    "4-group limit.",
        },
        "noop": {
            "label": "Send NoOp (keep-alive)",
            "params": {},
            "help": "Manually fire a keep-alive. Auto-poll already keeps "
                    "the session warm; only needed for diagnostics.",
        },
    }

    for blk in blocks:
        bt = blk["type"]
        name = blk["name"]
        safe = _safe_token(name)
        extra = blk["extra"]

        if bt == "named_control":
            value_type = extra.get("value_type", "number")
            if value_type == "trigger":
                cmds[f"{safe}_trigger"] = {
                    "label": f"{name}: Trigger",
                    "params": {},
                }
            else:
                pt = {"number": "number", "boolean": "enum", "string": "string"}.get(
                    value_type, "string"
                )
                value_param: dict[str, Any] = {
                    "type": pt, "required": True, "label": "Value",
                }
                if pt == "enum":
                    value_param["values"] = ["true", "false"]
                cmds[f"{safe}_set"] = {
                    "label": f"{name}: Set Value",
                    "params": {
                        "value": value_param,
                        "ramp": {"type": "number", "required": False,
                                 "label": "Ramp (sec)", "default": 0.0, "min": 0.0},
                    },
                }
        elif bt == "gain":
            cmds[f"{safe}_set_gain"] = {
                "label": f"{name}: Set Gain (dB)",
                "params": {
                    "gain_db": {"type": "number", "required": True,
                                "label": "Gain (dB)", "min": -100, "max": 20},
                    "ramp": {"type": "number", "required": False,
                             "label": "Ramp (sec)", "default": 0.0, "min": 0.0},
                },
            }
            cmds[f"{safe}_set_mute"] = {
                "label": f"{name}: Set Mute",
                "params": {
                    "value": {"type": "enum", "required": True, "label": "State",
                              "values": ["true", "false"]},
                },
            }
            cmds[f"{safe}_set_bypass"] = {
                "label": f"{name}: Set Bypass",
                "params": {
                    "value": {"type": "enum", "required": True, "label": "State",
                              "values": ["true", "false"]},
                },
            }
        elif bt == "mute":
            cmds[f"{safe}_set_mute"] = {
                "label": f"{name}: Set Mute",
                "params": {
                    "value": {"type": "enum", "required": True, "label": "State",
                              "values": ["true", "false"]},
                },
            }
        elif bt == "mixer":
            inputs = extra.get("inputs", 4)
            outputs = extra.get("outputs", 4)
            cmds[f"{safe}_set_input_gain"] = {
                "label": f"{name}: Set Input Gain (dB)",
                "params": {
                    "inputs": {"type": "string", "required": True,
                               "label": "Inputs",
                               "default": "*",
                               "help": "Q-SYS spec: '*' all, '1 2 3', '1-6', "
                                       "'1-3 5-9', '* !3-5'."},
                    "gain_db": {"type": "number", "required": True,
                                "label": "Gain (dB)", "min": -100, "max": 20},
                    "ramp": {"type": "number", "required": False,
                             "label": "Ramp (sec)", "default": 0.0, "min": 0.0},
                },
            }
            cmds[f"{safe}_set_input_mute"] = {
                "label": f"{name}: Set Input Mute",
                "params": {
                    "inputs": {"type": "string", "required": True,
                               "label": "Inputs", "default": "*"},
                    "value": {"type": "enum", "required": True, "label": "State",
                              "values": ["true", "false"]},
                },
            }
            cmds[f"{safe}_set_output_gain"] = {
                "label": f"{name}: Set Output Gain (dB)",
                "params": {
                    "outputs": {"type": "string", "required": True,
                                "label": "Outputs", "default": "*"},
                    "gain_db": {"type": "number", "required": True,
                                "label": "Gain (dB)", "min": -100, "max": 20},
                    "ramp": {"type": "number", "required": False,
                             "label": "Ramp (sec)", "default": 0.0, "min": 0.0},
                },
            }
            cmds[f"{safe}_set_output_mute"] = {
                "label": f"{name}: Set Output Mute",
                "params": {
                    "outputs": {"type": "string", "required": True,
                                "label": "Outputs", "default": "*"},
                    "value": {"type": "enum", "required": True, "label": "State",
                              "values": ["true", "false"]},
                },
            }
            cmds[f"{safe}_set_xpoint_gain"] = {
                "label": f"{name}: Set Crosspoint Gain",
                "params": {
                    "inputs": {"type": "string", "required": True,
                               "label": "Inputs", "default": "*"},
                    "outputs": {"type": "string", "required": True,
                                "label": "Outputs", "default": "*"},
                    "gain_db": {"type": "number", "required": True,
                                "label": "Gain (dB)", "min": -100, "max": 20},
                    "ramp": {"type": "number", "required": False,
                             "label": "Ramp (sec)", "default": 0.0, "min": 0.0},
                },
            }
            cmds[f"{safe}_set_xpoint_mute"] = {
                "label": f"{name}: Set Crosspoint Mute",
                "params": {
                    "inputs": {"type": "string", "required": True,
                               "label": "Inputs", "default": "*"},
                    "outputs": {"type": "string", "required": True,
                                "label": "Outputs", "default": "*"},
                    "value": {"type": "enum", "required": True, "label": "State",
                              "values": ["true", "false"]},
                },
            }
        elif bt == "router":
            outputs = extra.get("outputs", 4)
            cmds[f"{safe}_route"] = {
                "label": f"{name}: Route Source to Output",
                "params": {
                    "output_n": {"type": "integer", "required": True,
                                 "label": "Output", "min": 1, "max": outputs},
                    "source": {"type": "integer", "required": True,
                               "label": "Source",
                               "min": 0, "max": 256,
                               "help": "0 = disconnect; otherwise pick source N."},
                },
            }
        elif bt == "snapshot":
            banks = extra.get("banks", 8)
            cmds[f"{safe}_recall"] = {
                "label": f"{name}: Recall Snapshot",
                "params": {
                    "bank": {"type": "integer", "required": True,
                             "label": "Snapshot Number",
                             "min": 1, "max": banks},
                    "ramp": {"type": "number", "required": False,
                             "label": "Ramp (sec)", "default": 0.0, "min": 0.0},
                },
            }
            cmds[f"{safe}_save"] = {
                "label": f"{name}: Save Current State as Snapshot",
                "params": {
                    "bank": {"type": "integer", "required": True,
                             "label": "Snapshot Number",
                             "min": 1, "max": banks},
                },
            }
        elif bt == "loop_player":
            cmds[f"{safe}_start"] = {
                "label": f"{name}: Start Playback",
                "params": {
                    "file": {"type": "string", "required": True,
                             "label": "File Path",
                             "help": "Path on the Q-SYS Core, e.g. "
                                     "'Audio/announcement.wav'."},
                    "output": {"type": "integer", "required": False,
                               "label": "Output", "default": 1, "min": 1, "max": 64},
                    "loop": {"type": "enum", "required": False, "label": "Loop",
                             "values": ["true", "false"], "default": "false"},
                    "start_time": {"type": "number", "required": False,
                                   "label": "Start Time", "default": -1,
                                   "help": "-1 = immediate; -2 = queue after "
                                           "current; >0 = absolute time-of-day "
                                           "in seconds."},
                },
            }
            cmds[f"{safe}_stop"] = {
                "label": f"{name}: Stop Playback",
                "params": {
                    "outputs": {"type": "string", "required": False,
                                "label": "Outputs", "default": "1",
                                "help": "Comma-separated output numbers, "
                                        "e.g. '1,3,4'."},
                },
            }
            cmds[f"{safe}_cancel"] = {
                "label": f"{name}: Cancel Queued Playback",
                "params": {
                    "outputs": {"type": "string", "required": False,
                                "label": "Outputs", "default": "1"},
                },
            }
        elif bt == "pa_zone":
            cmds[f"{safe}_page_submit"] = {
                "label": f"{name}: Submit Page Request",
                "params": {
                    "zones": {"type": "string", "required": True,
                              "label": "Zone IDs",
                              "help": "Comma-separated zone IDs (PA Router "
                                      "zones)."},
                    "priority": {"type": "integer", "required": False,
                                 "label": "Priority",
                                 "default": 1, "min": 1, "max": 8},
                    "preamble": {"type": "string", "required": False,
                                 "label": "Preamble Sound",
                                 "default": "",
                                 "help": "Optional preamble audio file."},
                    "message": {"type": "string", "required": False,
                                "label": "Message File",
                                "default": "",
                                "help": "Optional pre-recorded message."},
                },
                "help": "Submits a PA Router page. Light pass — full PARAPI "
                        "assembly is out of scope.",
            }
            cmds[f"{safe}_page_cancel"] = {
                "label": f"{name}: Cancel Page",
                "params": {
                    "page_id": {"type": "string", "required": True,
                                "label": "Page Request ID"},
                },
            }

    return cmds


# Default block list seeded into the textarea so the Add Device dialog
# isn't empty. Covers a typical conference room: program gain + mute +
# source select + a snapshot bank for room scenes.
DEFAULT_BLOCKS_TEXT = """\
# Q-SYS Block List — one block per line.
# Format:  <CODE_NAME>  <TYPE>  [SPEC]
# Lines starting with '#' or ';' are comments and are ignored.
#
# CODE_NAME comes from the "Code Name" property of any block, Named
# Control, mixer, router, snapshot bank, etc. in your Q-SYS Designer
# file. Right-click the item in the schematic, choose Properties, and
# look for "Code Name". Code names are case-sensitive — match exactly.
#
# Block types:
#   named_control [number|boolean|string|trigger]
#                       Any Named Control. Default type is 'number'.
#   gain                Component gain block (gain dB, mute, invert, bypass).
#   mute                Component mute block (mute, bypass).
#   mixer NxM           Mixer block. NxM is INPUTS x OUTPUTS, e.g. 8x4.
#                       Per-input gain/mute, per-output gain/mute, and
#                       per-crosspoint gain/mute (skipped for >64 cells).
#   router N            Audio Router with N outputs (per-output source
#                       select).
#   snapshot N          Snapshot bank with N snapshots. Recall/save by
#                       index. The CODE_NAME is the bank name in Designer.
#   loop_player         Loop Player component.
#   pa_zone             PA Router zone — light pass for page submit/cancel.
#   meter               Audio Meter / Peak-RMS Meter (read-only).
#   component           Opaque pass-through. Subscribes to ALL controls
#                       of the named component (Component.GetControls
#                       at connect time).
#
# Replace the example blocks below with the Code Names from YOUR Q-SYS
# design. Delete the lines you don't need; add more later by editing
# the device.

# Example: typical conference room
PgmGain     gain                # main program gain block
RoomMute    mute                # whole-room mute
PgmRouter   router    4         # 4-output source router
RoomScenes  snapshot  8         # room-scene snapshot bank
Volume      named_control number   # raw volume Named Control
Mute        named_control boolean  # raw mute Named Control
"""


# ── The driver ──

class QSCQRCDriver(BaseDriver):
    """QSC Q-SYS QRC driver — comprehensive coverage."""

    DRIVER_INFO = {
        "id": "qsc_qrc",
        "name": "QSC Q-SYS QRC",
        "manufacturer": "QSC",
        "category": "audio",
        "version": "3.0.0",
        "min_platform_version": "0.11.0",
        "author": "OpenAVC",
        "description": (
            "Controls QSC Q-SYS Cores via QRC (Q-SYS Remote Control) — "
            "JSON-RPC 2.0 over TCP port 1710. QRC is QSC's modern "
            "control protocol; ECP (port 1702) is legacy. Supports "
            "Named Controls, Component gain/mute, Mixer (per-input, "
            "per-output, crosspoint), Audio Router, Snapshot recall / "
            "save, Loop Player, and PA Router page submission. Uses "
            "Change Groups with auto-poll for push-style state updates "
            "instead of polling. Declare your Q-SYS blocks once in the "
            "device config — the driver expands per-block state "
            "variables and commands automatically."
        ),
        "source_url": (
            "https://q-syshelp.qsc.com/Content/External_Control_APIs/QRC/"
            "QRC_Commands.htm"
        ),
        "tags": ["dsp", "q-sys", "qrc", "ceiling-mic", "conferencing"],
        "verified": False,
        "simulated": True,
        "protocols": ["qsc_qrc"],
        "ports": [1710],
        "transport": "tcp",
        "discovery": {
            "ports": [1710],
            "mac_prefixes": ["00:0c:4d", "7c:2e:0d"],
        },
        "compatible_models": [
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
                    "Every Q-SYS Core that runs Q-SYS Designer software "
                    "supports QRC. The driver speaks QRC (TCP 1710), not "
                    "the legacy ECP (TCP 1702) — make sure the Core's "
                    "Q-SYS firmware supports QRC (any version since "
                    "Q-SYS 5.x). Setup: open your design in Q-SYS "
                    "Designer, find the Code Names of the Components, "
                    "Named Controls, Mixers, Routers, and Snapshots you "
                    "want to control, then declare them in the block "
                    "list below."
                ),
            },
        ],
        "help": {
            "overview": (
                "Comprehensive QSC Q-SYS control over QRC (JSON-RPC 2.0 "
                "on TCP 1710). The driver subscribes to every declared "
                "block via a Change Group with auto-poll — UI bindings "
                "update in real-time without polling. Declare every "
                "Q-SYS block you want to monitor or control in the "
                "'Q-SYS Block List' field below; the driver expands one "
                "state variable per typed control and surfaces typed "
                "commands named after each block."
            ),
            "setup": (
                "STEP 1 — Make sure QRC is reachable.\n"
                "QRC is enabled by default on Q-SYS Cores running a "
                "design in Run mode. The default port is 1710. If your "
                "Core has admin authentication enabled (Users → "
                "Administrator), enter Username and Password below; "
                "otherwise leave them blank.\n"
                "\n"
                "STEP 2 — Find your Code Names.\n"
                "Open the Q-SYS design (.qsys file) for this Core in "
                "Q-SYS Designer. Each block (Gain, Mute, Mixer, Router, "
                "Snapshot Bank, Loop Player, etc.) has a Code Name on "
                "its Properties pane — that's the name QRC uses to "
                "address it. Right-click any block → Properties → "
                "'Code Name'. Code names are case-sensitive: 'PgmGain' "
                "and 'pgmgain' are NOT the same.\n"
                "\n"
                "Named Controls live in the Named Controls pane (Tools "
                "→ Show Named Controls). The 'Name' column is what you "
                "declare here.\n"
                "\n"
                "STEP 3 — Enter the Core's IP address above.\n"
                "\n"
                "STEP 4 — Declare your blocks in the 'Q-SYS Block List' "
                "field below.\n"
                "Format: one block per line, whitespace-separated\n"
                "    <CODE_NAME> <TYPE> [SPEC]\n"
                "\n"
                "Examples (typical conferencing room):\n"
                "    PgmGain     gain                # program gain block\n"
                "    RoomMute    mute                # whole-room mute\n"
                "    PgmRouter   router    4         # 4-output source router\n"
                "    RoomScenes  snapshot  8         # 8-snapshot bank\n"
                "    BgmLoop     loop_player         # background-music loop\n"
                "    Volume      named_control number   # volume Named Control\n"
                "    Mute        named_control boolean  # mute Named Control\n"
                "    DoorBell    named_control trigger  # trigger button\n"
                "    PgmMix      mixer     8x4       # 8 inputs x 4 outputs\n"
                "    LobbyZone   pa_zone             # PA Router zone\n"
                "\n"
                "Notes on the syntax:\n"
                "    • SPEC is type-dependent — 'NxM' for mixers, the "
                "output count for routers, the snapshot count for "
                "snapshot banks, the value type for named_control.\n"
                "    • named_control type can be number / boolean / "
                "string / trigger. Default: number.\n"
                "    • Lines starting with '#' or ';' are comments.\n"
                "    • The pre-filled textarea has a complete syntax "
                "guide.\n"
                "\n"
                "STEP 5 — Save.\n"
                "Within seconds the device should show 'Connected'. The "
                "Core's status (Active / Idle / Standby), platform "
                "model, design name, and design code populate "
                "core_state, core_platform, etc. automatically. Open "
                "Variables → Devices and you'll see one state variable "
                "per typed control on each declared block — e.g. "
                "'PgmGain_gain' (dB), 'PgmGain_mute' (boolean), "
                "'PgmMix_xpoint_gain_3_2' (dB). Bind those state keys "
                "to UI elements, or use the auto-named per-block "
                "commands ('PgmGain: Set Gain (dB)') in macros and "
                "panel buttons.\n"
                "\n"
                "Troubleshooting:\n"
                "    • 'Connection refused' on port 1710 — verify the "
                "Core is in Run mode and that QRC isn't blocked by a "
                "VLAN ACL.\n"
                "    • 'Logon required' errors — set Username and "
                "Password above to match a user defined under Q-SYS "
                "Administrator.\n"
                "    • Some controls don't appear — check that the "
                "Code Names match exactly (case matters), and that the "
                "block exists in the currently-running design.\n"
                "    • Need a block type the driver doesn't handle? "
                "Use 'set_component_control' or 'send_raw_jsonrpc' — "
                "anything QRC supports will work."
            ),
        },
        "default_config": {
            "host": "",
            "port": DEFAULT_PORT,
            "username": "",
            "password": "",
            "blocks": DEFAULT_BLOCKS_TEXT,
            "autopoll_rate_seconds": DEFAULT_AUTOPOLL_RATE_S,
            "inter_command_delay": DEFAULT_INTER_COMMAND_DELAY,
            "poll_interval": 0,
        },
        "config_schema": {
            "host": {
                "type": "string", "required": True, "label": "IP Address",
            },
            "port": {
                "type": "integer", "default": DEFAULT_PORT,
                "label": "QRC Port",
                "min": 1, "max": 65535,
                "description": (
                    "QSC's QRC TCP port. Default 1710 — leave it unless "
                    "you know your Core has been re-configured. Note: "
                    "1702 is the legacy ECP port (different protocol) "
                    "and is not supported by this driver."
                ),
            },
            "username": {
                "type": "string", "default": "", "label": "Username",
                "description": (
                    "Q-SYS Administrator username. Leave blank if your "
                    "Core has no authentication configured."
                ),
            },
            "password": {
                "type": "string", "default": "", "label": "Password",
                "secret": True,
            },
            "blocks": {
                "type": "text",
                "label": "Q-SYS Block List",
                "default": DEFAULT_BLOCKS_TEXT,
                "description": (
                    "List the Q-SYS blocks you want to monitor or "
                    "control — one per line. Format: "
                    "<CODE_NAME> <TYPE> [SPEC]. The pre-filled textarea "
                    "has a full syntax guide; see Setup above for a "
                    "step-by-step walkthrough including how to find "
                    "Code Names in Q-SYS Designer."
                ),
            },
            "autopoll_rate_seconds": {
                "type": "number",
                "default": DEFAULT_AUTOPOLL_RATE_S,
                "min": 0.05, "max": 60.0,
                "label": "Auto-Poll Rate (sec)",
                "description": (
                    "How often the Core pushes change-group updates. "
                    "0.25 (250 ms) is responsive for fader UIs; raise "
                    "for low-traffic systems."
                ),
            },
            "inter_command_delay": {
                "type": "number",
                "default": DEFAULT_INTER_COMMAND_DELAY,
                "min": 0,
                "label": "Inter-Command Delay (sec)",
                "description": (
                    "Delay between sequential outgoing commands. "
                    "Q-SYS's parser tolerates rapid bursts; a small "
                    "delay keeps the connect-time AddControl burst "
                    "well-ordered on smaller Cores."
                ),
            },
            "poll_interval": {
                "type": "integer",
                "default": 0, "min": 0,
                "label": "Backstop Poll Interval (sec)",
                "description": (
                    "Refresh poll. Auto-poll is the source of truth — "
                    "leave at 0 unless you've seen state drift."
                ),
            },
        },
        "state_variables": {},
        "commands": {},
    }

    def __init__(
        self,
        device_id: str,
        config: dict[str, Any],
        state: Any,
        events: Any,
    ) -> None:
        blocks_text = str(config.get("blocks", DEFAULT_BLOCKS_TEXT))
        self._blocks: list[dict[str, Any]] = parse_blocks_config(blocks_text)
        # Index blocks by name for fast lookup in send_command dispatch.
        self._block_by_name: dict[str, dict[str, Any]] = {
            b["name"]: b for b in self._blocks
        }
        # Build the lists of Named Controls and Component-control specs
        # that will go into the main change group on connect.
        self._main_named, self._main_component = expand_change_group_entries(self._blocks)

        # Map (component_name_or_None, control_name) -> (state_key, type_hint)
        # for routing change-group push results back to state vars. Built
        # up alongside the change-group entries.
        self._control_route: dict[tuple[str | None, str], tuple[str, str]] = {}
        self._build_control_route()

        self.DRIVER_INFO = {
            **type(self).DRIVER_INFO,
            "state_variables": expand_state_variables(self._blocks),
            "commands": expand_commands(self._blocks),
        }

        super().__init__(device_id, config, state, events)

        # JSON-RPC request id allocator + pending response futures.
        self._next_id = INITIAL_REQUEST_ID
        self._pending: dict[int, asyncio.Future[Any]] = {}
        # Tracking for runtime change-groups created via subscribe_*.
        # Key: group_id -> list[(component_or_None, control_name, state_key, type_hint)]
        self._runtime_groups: dict[str, list[tuple[str | None, str, str, str]]] = {}

        # Send queue serialization — one outgoing JSON-RPC frame at a
        # time, with inter_command_delay between sends.
        self._send_lock = asyncio.Lock()

    # ── Lifecycle ──

    async def connect(self) -> None:
        from server.transport.tcp import TCPTransport
        from server.system_config import get_system_config

        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", DEFAULT_PORT))
        if not host:
            raise ConnectionError(f"[{self.device_id}] No host configured")

        delay = float(self.config.get(
            "inter_command_delay", DEFAULT_INTER_COMMAND_DELAY
        ))
        control_ip = get_system_config().get("network", "control_interface")

        # QRC framing is null-terminated. The transport's
        # DelimiterFrameParser splits on \x00 and strips it before
        # delivering each frame to on_data_received.
        self.transport = await TCPTransport.create(
            host=host, port=port,
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            delimiter=FRAME_TERMINATOR,
            inter_command_delay=delay,
            name=self.device_id,
            local_addr=(control_ip, 0) if control_ip else None,
        )

        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        log.info(f"[{self.device_id}] Connected to Q-SYS Core at {host}:{port}")

        # Optional Logon. Q-SYS uses sticky session auth — once Logon
        # succeeds, every subsequent command on this socket is
        # authenticated until disconnect.
        username = str(self.config.get("username", "")).strip()
        password = str(self.config.get("password", ""))
        if username:
            await self._do_logon(username, password)
        else:
            self.set_state("logon_ok", True)

        # Pull initial system status. The Core also auto-pushes
        # EngineStatus on connect, but StatusGet is the contract method
        # that gives us Platform too.
        await self._do_status_get()

        # Set up the main change group. Order matters: AddControl /
        # AddComponentControl populate the group; AutoPoll registers
        # the auto-push timer that fires Changes back at us.
        await self._setup_main_change_group()

        # Resolve `component` blocks dynamically — Component.GetControls
        # tells us the available control list, then we register them
        # into the main change group.
        for blk in self._blocks:
            if blk["type"] == "component":
                await self._resolve_component_block(blk)

        poll_interval = int(self.config.get("poll_interval", 0))
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def disconnect(self) -> None:
        await self.stop_polling()
        # Cancel any in-flight requests so awaiters don't hang.
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

        if self.transport:
            try:
                # Polite cleanup: destroy our change groups so the Core
                # frees the slots.
                await self._send_jsonrpc("ChangeGroup.Destroy",
                                          {"Id": MAIN_CHANGE_GROUP},
                                          expect_response=False)
                for gid in list(self._runtime_groups.keys()):
                    try:
                        await self._send_jsonrpc(
                            "ChangeGroup.Destroy", {"Id": gid},
                            expect_response=False,
                        )
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
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info(f"[{self.device_id}] Disconnected")

    def _handle_transport_disconnect(self) -> None:
        # Clear any pending response futures so awaiters fail fast
        # instead of hanging until their per-call timeout. Then defer
        # to the base-class implementation (which stops polling, sets
        # state, and emits the disconnect event).
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
        """Send one JSON-RPC frame.

        If `expect_response` is True, register a future keyed by the
        request id and wait up to `timeout` seconds for the matching
        response. Otherwise the frame is fire-and-forget (used for
        pure event-style methods like AutoPoll setup, and for
        teardown during disconnect when the socket is going away).

        Returns the result payload (whatever was in `result`), or
        raises on JSON-RPC error / timeout / connection failure.
        """
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
                f"[{self.device_id}] QRC {method} timed out after {timeout}s"
            )
        finally:
            self._pending.pop(rid, None)

    async def on_data_received(self, data: bytes) -> None:
        """Handle a single JSON-RPC frame from the Core."""
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
            log.warning(f"[{self.device_id}] Non-object JSON frame: {msg!r}")
            return

        # Response frames have an `id` matching one of our requests.
        rid = msg.get("id")
        if rid is not None and rid in self._pending:
            fut = self._pending.pop(rid)
            if "error" in msg and msg["error"] is not None:
                err = msg["error"]
                err_str = (err.get("message") if isinstance(err, dict)
                           else str(err))
                self.set_state("last_error", str(err_str))
                fut.set_exception(QRCError(err))
            else:
                # Some QSC responses use 'result', others use 'response'.
                # Handle both.
                fut.set_result(msg.get("result", msg.get("response")))
            return

        # Otherwise it's an event / unsolicited message — route by method.
        method = msg.get("method", "")
        params = msg.get("params", {})
        if method == "EngineStatus":
            self._handle_engine_status(params)
        elif method == "ChangeGroup.Poll":
            # Some firmware variants report AutoPoll deltas as method
            # invocations rather than id-correlated responses. Handle
            # both shapes.
            self._handle_change_group_changes(params)
        else:
            # Could be an unsolicited result — peek for ChangeGroup.Poll
            # shape (Id + Changes array).
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
                "Logon", {"User": username, "Password": password},
                timeout=5.0,
            )
            self.set_state("logon_ok", True)
            log.info(f"[{self.device_id}] Logon successful")
        except QRCError as exc:
            self.set_state("logon_ok", False)
            log.error(f"[{self.device_id}] Logon failed: {exc}")
            raise ConnectionError(f"Logon rejected: {exc}") from exc
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
            self.set_state("core_status_string", str(status.get("String", "")))

    def _handle_engine_status(self, params: Any) -> None:
        if isinstance(params, dict):
            self._update_status_from_payload(params)

    async def _setup_main_change_group(self) -> None:
        """Add every declared block's controls to the main change group
        and register auto-poll. Called on connect (and after every
        reconnect — change groups are session-scoped)."""
        # Named Controls
        if self._main_named:
            try:
                await self._send_jsonrpc(
                    "ChangeGroup.AddControl",
                    {"Id": MAIN_CHANGE_GROUP, "Controls": self._main_named},
                    timeout=3.0,
                )
            except (QRCError, TimeoutError) as exc:
                log.warning(
                    f"[{self.device_id}] AddControl failed: {exc}"
                )

        # Per-component bundles
        for spec in self._main_component:
            try:
                await self._send_jsonrpc(
                    "ChangeGroup.AddComponentControl",
                    {
                        "Id": MAIN_CHANGE_GROUP,
                        "Component": {
                            "Name": spec["name"],
                            "Controls": [{"Name": c} for c in spec["controls"]],
                        },
                    },
                    timeout=3.0,
                )
            except (QRCError, TimeoutError) as exc:
                log.warning(
                    f"[{self.device_id}] AddComponentControl({spec['name']}) "
                    f"failed: {exc}"
                )

        # Auto-poll for the main change group. Once registered, the
        # Core pushes Changes back at us at the configured rate. This
        # also serves as the >60s keep-alive (the Core closes idle
        # connections).
        rate = float(self.config.get(
            "autopoll_rate_seconds", DEFAULT_AUTOPOLL_RATE_S
        ))
        try:
            initial = await self._send_jsonrpc(
                "ChangeGroup.AutoPoll",
                {"Id": MAIN_CHANGE_GROUP, "Rate": rate},
                timeout=3.0,
            )
        except (QRCError, TimeoutError) as exc:
            log.warning(f"[{self.device_id}] AutoPoll register failed: {exc}")
            return

        # The first AutoPoll response carries every control's current
        # value (per QSC docs: "If the Change Group is being polled
        # for the first time, the Core responds with all the controls
        # in the Change Group and their states.").
        if isinstance(initial, dict):
            self._handle_change_group_changes(initial)

        log.info(
            f"[{self.device_id}] Subscribed: {len(self._main_named)} named "
            f"controls, {sum(len(s['controls']) for s in self._main_component)} "
            f"component controls (rate {rate}s)"
        )

    async def _resolve_component_block(self, blk: dict[str, Any]) -> None:
        """For type=component blocks: query the Core for the available
        control list and register every Read/Write control into the
        main change group. New state vars are created dynamically."""
        name = blk["name"]
        try:
            payload = await self._send_jsonrpc(
                "Component.GetControls", {"Name": name}, timeout=3.0,
            )
        except (QRCError, TimeoutError) as exc:
            log.warning(
                f"[{self.device_id}] Component.GetControls({name}) "
                f"failed: {exc}"
            )
            return
        if not isinstance(payload, dict):
            return
        controls = payload.get("Controls", [])
        if not isinstance(controls, list):
            return

        added: list[dict[str, str]] = []
        for ctrl in controls:
            if not isinstance(ctrl, dict):
                continue
            cname = ctrl.get("Name")
            if not cname:
                continue
            ctype = ctrl.get("Type", "")
            type_hint = self._qsys_type_to_hint(ctype)
            state_key = self._state_var_for(blk, _safe_token(cname))
            self._control_route[(name, cname)] = (state_key, type_hint)
            added.append({"Name": cname})

        if added:
            try:
                await self._send_jsonrpc(
                    "ChangeGroup.AddComponentControl",
                    {
                        "Id": MAIN_CHANGE_GROUP,
                        "Component": {"Name": name, "Controls": added},
                    },
                    timeout=3.0,
                )
            except (QRCError, TimeoutError) as exc:
                log.warning(
                    f"[{self.device_id}] AddComponentControl({name}) "
                    f"runtime registration failed: {exc}"
                )

        self.set_state(
            self._state_var_for(blk, "_components_loaded"), True,
        )

    @staticmethod
    def _qsys_type_to_hint(qsys_type: str) -> str:
        qt = (qsys_type or "").lower()
        if qt == "boolean":
            return "boolean"
        if qt in ("integer", "int"):
            return "integer"
        if qt in ("float", "number", "double"):
            return "number"
        if qt == "string":
            return "string"
        return "string"

    # ── Control routing ──

    def _build_control_route(self) -> None:
        """Populate the (component, control) -> (state_key, hint) map
        for every block we expand statically. Component blocks are
        added at runtime when their controls resolve."""
        for blk in self._blocks:
            bt = blk["type"]
            name = blk["name"]
            extra = blk["extra"]

            if bt == "named_control":
                value_type = extra.get("value_type", "number")
                if value_type == "trigger":
                    continue
                hint = {
                    "number": "number", "boolean": "boolean",
                    "string": "string",
                }.get(value_type, "string")
                self._control_route[(None, name)] = (
                    _state_var(blk, "value"), hint,
                )
            elif bt == "gain":
                self._add_component_route(blk, "gain", "number")
                self._add_component_route(blk, "mute", "boolean")
                self._add_component_route(blk, "invert", "boolean")
                self._add_component_route(blk, "bypass", "boolean")
            elif bt == "mute":
                self._add_component_route(blk, "mute", "boolean")
                self._add_component_route(blk, "bypass", "boolean")
            elif bt == "mixer":
                inputs = extra.get("inputs", 4)
                outputs = extra.get("outputs", 4)
                for i in range(1, inputs + 1):
                    self._control_route[(blk["name"], f"input.{i}.mute")] = (
                        _state_var(blk, f"input_mute_{i}"), "boolean",
                    )
                    self._control_route[(blk["name"], f"input.{i}.gain")] = (
                        _state_var(blk, f"input_gain_{i}"), "number",
                    )
                for o in range(1, outputs + 1):
                    self._control_route[(blk["name"], f"output.{o}.mute")] = (
                        _state_var(blk, f"output_mute_{o}"), "boolean",
                    )
                    self._control_route[(blk["name"], f"output.{o}.gain")] = (
                        _state_var(blk, f"output_gain_{o}"), "number",
                    )
                if inputs * outputs <= 64:
                    for i in range(1, inputs + 1):
                        for o in range(1, outputs + 1):
                            self._control_route[
                                (blk["name"], f"input.{i}.output.{o}.mute")
                            ] = (
                                _state_var(blk, f"xpoint_mute_{i}_{o}"),
                                "boolean",
                            )
                            self._control_route[
                                (blk["name"], f"input.{i}.output.{o}.gain")
                            ] = (
                                _state_var(blk, f"xpoint_gain_{i}_{o}"),
                                "number",
                            )
            elif bt == "router":
                outputs = extra.get("outputs", 4)
                for o in range(1, outputs + 1):
                    self._control_route[(blk["name"], f"select.{o}")] = (
                        _state_var(blk, f"output_{o}"), "integer",
                    )
            elif bt == "meter":
                for ctrl_name in METER_CONTROLS:
                    self._control_route[(blk["name"], ctrl_name)] = (
                        _state_var(blk, _safe_token(ctrl_name)),
                        METER_CONTROLS[ctrl_name],
                    )

    def _add_component_route(
        self, blk: dict[str, Any], control_name: str, hint: str,
    ) -> None:
        self._control_route[(blk["name"], control_name)] = (
            _state_var(blk, _safe_token(control_name)), hint,
        )

    @staticmethod
    def _state_var_for(blk: dict[str, Any], suffix: str) -> str:
        return _state_var(blk, suffix)

    # ── Change-group push handler ──

    def _handle_change_group_changes(self, payload: Any) -> None:
        """Process a ChangeGroup.Poll or AutoPoll payload, fanning the
        Changes array out into state-var updates."""
        if not isinstance(payload, dict):
            return
        changes = payload.get("Changes")
        if not isinstance(changes, list):
            return
        group_id = payload.get("Id")
        for chg in changes:
            if not isinstance(chg, dict):
                continue
            comp = chg.get("Component")  # None for Named Controls
            cname = chg.get("Name")
            if not cname:
                continue
            value = chg.get("Value")
            string_repr = chg.get("String", "")

            # Look up the state-var binding. Try the runtime-group
            # bindings first (subscribe_* commands write here), then
            # the static bindings built at __init__.
            state_key: str | None = None
            type_hint = "string"
            if group_id and group_id in self._runtime_groups:
                for rcomp, rname, rkey, rhint in self._runtime_groups[group_id]:
                    if rcomp == comp and rname == cname:
                        state_key = rkey
                        type_hint = rhint
                        break

            if state_key is None:
                route = self._control_route.get((comp, cname))
                if route is not None:
                    state_key, type_hint = route

            if state_key is None:
                # Component block fan-out — comp+cname not in static
                # map but we may have registered it dynamically. Use
                # a sanitized synthetic key.
                if comp:
                    state_key = f"{_safe_token(comp)}_{_safe_token(cname)}"
                else:
                    state_key = f"{_safe_token(cname)}_value"
                type_hint = "string"

            coerced = self._coerce_value(value, type_hint)
            if coerced is not None:
                # Write through state.set so synthetic keys (not in
                # DRIVER_INFO) bypass set_state's validation.
                if state_key in self.DRIVER_INFO.get("state_variables", {}):
                    self.set_state(state_key, coerced)
                else:
                    self.state.set(
                        f"device.{self.device_id}.{state_key}", coerced,
                    )

            # Mirror the formatted string for Named Controls (they
            # have a *_string state var).
            if comp is None:
                blk = self._block_by_name.get(cname)
                if blk and blk["type"] == "named_control":
                    skey = _state_var(blk, "string")
                    if skey in self.DRIVER_INFO.get("state_variables", {}):
                        self.set_state(skey, str(string_repr))

    @staticmethod
    def _coerce_value(raw: Any, type_hint: str) -> Any:
        if raw is None:
            return None
        if type_hint == "boolean":
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, (int, float)):
                return bool(raw)
            s = str(raw).strip().lower()
            if s in ("true", "1", "on", "yes"):
                return True
            if s in ("false", "0", "off", "no"):
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
        # string / fallback
        return str(raw)

    # ── Outgoing command dispatch ──

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None,
    ) -> Any:
        params = params or {}

        # Generic escape hatches
        if command == "set_named_control":
            return await self._cmd_set_named_control(
                params["name"], params["value"],
                float(params.get("ramp", 0.0) or 0.0),
            )
        if command == "get_named_control":
            return await self._cmd_get_named_control(params["name"])
        if command == "trigger_named_control":
            # Trigger controls in Q-SYS expect a boolean true (the
            # Core treats any value as a momentary fire).
            return await self._cmd_set_named_control(params["name"], True, 0.0)
        if command == "set_component_control":
            return await self._cmd_set_component_control(
                params["component"], params["control"], params["value"],
                float(params.get("ramp", 0.0) or 0.0),
            )
        if command == "get_component_control":
            return await self._cmd_get_component_control(
                params["component"], params["control"],
            )
        if command == "recall_snapshot":
            return await self._cmd_snapshot_load(
                params["bank_name"], int(params["bank"]),
                float(params.get("ramp", 0.0) or 0.0),
            )
        if command == "save_snapshot":
            return await self._cmd_snapshot_save(
                params["bank_name"], int(params["bank"]),
            )
        if command == "send_raw_jsonrpc":
            return await self._cmd_raw_jsonrpc(
                params["method"], params.get("params_json", "{}"),
            )
        if command == "subscribe_named_control":
            return await self._cmd_subscribe_named_control(
                params["group"], params["name"],
                float(params.get("rate_seconds", 0.5)),
            )
        if command == "subscribe_component_control":
            return await self._cmd_subscribe_component_control(
                params["group"], params["component"], params["control"],
                float(params.get("rate_seconds", 0.5)),
            )
        if command == "destroy_change_group":
            return await self._cmd_destroy_change_group(params["group"])
        if command == "noop":
            await self._send_jsonrpc("NoOp", {}, expect_response=False)
            return True

        # Per-block typed commands
        return await self._dispatch_per_block_command(command, params)

    # ── Generic-command implementations ──

    @staticmethod
    def _coerce_outgoing_value(raw: Any) -> Any:
        """Convert a Python or string value into the JSON-typed form Q-SYS
        expects. Number-like strings become numbers; 'true' / 'false'
        become booleans; everything else stays as a string."""
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

    async def _cmd_set_named_control(
        self, name: str, value: Any, ramp: float,
    ) -> bool:
        params: dict[str, Any] = {
            "Name": name, "Value": self._coerce_outgoing_value(value),
        }
        if ramp > 0:
            params["Ramp"] = ramp
        await self._send_jsonrpc("Control.Set", params)
        return True

    async def _cmd_get_named_control(self, name: str) -> Any:
        result = await self._send_jsonrpc("Control.Get", [name])
        self.set_state("last_query_result", json.dumps(result))
        return result

    async def _cmd_set_component_control(
        self, component: str, control: str, value: Any, ramp: float,
    ) -> bool:
        ctrl: dict[str, Any] = {
            "Name": control, "Value": self._coerce_outgoing_value(value),
        }
        if ramp > 0:
            ctrl["Ramp"] = ramp
        await self._send_jsonrpc(
            "Component.Set",
            {"Name": component, "Controls": [ctrl]},
        )
        return True

    async def _cmd_get_component_control(
        self, component: str, control: str,
    ) -> Any:
        result = await self._send_jsonrpc(
            "Component.Get",
            {"Name": component, "Controls": [{"Name": control}]},
        )
        self.set_state("last_query_result", json.dumps(result))
        return result

    async def _cmd_snapshot_load(
        self, bank_name: str, bank: int, ramp: float,
    ) -> bool:
        params: dict[str, Any] = {"Name": bank_name, "Bank": bank}
        if ramp > 0:
            params["Ramp"] = ramp
        await self._send_jsonrpc("Snapshot.Load", params, expect_response=False)
        # Mirror to per-bank-block state if a matching block was declared.
        for blk in self._blocks:
            if blk["type"] == "snapshot" and blk["name"] == bank_name:
                self.set_state(_state_var(blk, "last_loaded"), bank)
                break
        return True

    async def _cmd_snapshot_save(self, bank_name: str, bank: int) -> bool:
        await self._send_jsonrpc(
            "Snapshot.Save", {"Name": bank_name, "Bank": bank},
            expect_response=False,
        )
        return True

    async def _cmd_raw_jsonrpc(
        self, method: str, params_json: str,
    ) -> Any:
        params: Any
        try:
            params = json.loads(params_json) if params_json.strip() else None
        except json.JSONDecodeError as exc:
            raise ValueError(f"Bad params JSON: {exc}") from exc
        result = await self._send_jsonrpc(method, params)
        self.set_state("last_query_result", json.dumps(result))
        return result

    async def _cmd_subscribe_named_control(
        self, group: str, name: str, rate: float,
    ) -> bool:
        await self._send_jsonrpc(
            "ChangeGroup.AddControl",
            {"Id": group, "Controls": [name]},
        )
        await self._send_jsonrpc(
            "ChangeGroup.AutoPoll", {"Id": group, "Rate": rate},
        )
        state_key = f"{_safe_token(name)}_value"
        self._runtime_groups.setdefault(group, []).append(
            (None, name, state_key, "string"),
        )
        return True

    async def _cmd_subscribe_component_control(
        self, group: str, component: str, control: str, rate: float,
    ) -> bool:
        await self._send_jsonrpc(
            "ChangeGroup.AddComponentControl",
            {
                "Id": group,
                "Component": {
                    "Name": component, "Controls": [{"Name": control}],
                },
            },
        )
        await self._send_jsonrpc(
            "ChangeGroup.AutoPoll", {"Id": group, "Rate": rate},
        )
        state_key = f"{_safe_token(component)}_{_safe_token(control)}"
        self._runtime_groups.setdefault(group, []).append(
            (component, control, state_key, "string"),
        )
        return True

    async def _cmd_destroy_change_group(self, group: str) -> bool:
        await self._send_jsonrpc(
            "ChangeGroup.Destroy", {"Id": group}, expect_response=False,
        )
        self._runtime_groups.pop(group, None)
        return True

    # ── Per-block typed-command dispatch ──

    async def _dispatch_per_block_command(
        self, command: str, params: dict[str, Any],
    ) -> Any:
        # Match block by command prefix (longest-match wins, in case
        # one safe-tag is a prefix of another).
        candidates = [
            blk for blk in self._blocks
            if command.startswith(_safe_token(blk["name"]) + "_")
        ]
        if not candidates:
            log.warning(f"[{self.device_id}] Unknown command: {command}")
            return None
        candidates.sort(key=lambda b: len(b["name"]), reverse=True)
        blk = candidates[0]
        safe = _safe_token(blk["name"])
        action = command[len(safe) + 1:]
        return await self._dispatch_block_action(blk, action, params)

    async def _dispatch_block_action(
        self, blk: dict[str, Any], action: str, params: dict[str, Any],
    ) -> Any:
        bt = blk["type"]
        name = blk["name"]
        extra = blk["extra"]

        if bt == "named_control":
            value_type = extra.get("value_type", "number")
            if action == "trigger" and value_type == "trigger":
                return await self._cmd_set_named_control(name, True, 0.0)
            if action == "set":
                ramp = float(params.get("ramp", 0.0) or 0.0)
                return await self._cmd_set_named_control(
                    name, params["value"], ramp,
                )
        elif bt == "gain":
            if action == "set_gain":
                return await self._cmd_set_component_control(
                    name, "gain", float(params["gain_db"]),
                    float(params.get("ramp", 0.0) or 0.0),
                )
            if action == "set_mute":
                return await self._cmd_set_component_control(
                    name, "mute", params["value"], 0.0,
                )
            if action == "set_bypass":
                return await self._cmd_set_component_control(
                    name, "bypass", params["value"], 0.0,
                )
        elif bt == "mute":
            if action == "set_mute":
                return await self._cmd_set_component_control(
                    name, "mute", params["value"], 0.0,
                )
        elif bt == "mixer":
            return await self._dispatch_mixer_action(blk, action, params)
        elif bt == "router":
            if action == "route":
                output_n = int(params["output_n"])
                return await self._cmd_set_component_control(
                    name, f"select.{output_n}", int(params["source"]), 0.0,
                )
        elif bt == "snapshot":
            if action == "recall":
                return await self._cmd_snapshot_load(
                    name, int(params["bank"]),
                    float(params.get("ramp", 0.0) or 0.0),
                )
            if action == "save":
                return await self._cmd_snapshot_save(
                    name, int(params["bank"]),
                )
        elif bt == "loop_player":
            if action == "start":
                return await self._dispatch_loop_player_start(blk, params)
            if action == "stop":
                return await self._dispatch_loop_player_stop(blk, params, "Stop")
            if action == "cancel":
                return await self._dispatch_loop_player_stop(blk, params, "Cancel")
        elif bt == "pa_zone":
            if action == "page_submit":
                return await self._dispatch_pa_page_submit(blk, params)
            if action == "page_cancel":
                return await self._dispatch_pa_page_cancel(blk, params)

        log.warning(
            f"[{self.device_id}] Unhandled per-block action: "
            f"block={name} type={bt} action={action}"
        )
        return None

    async def _dispatch_mixer_action(
        self, blk: dict[str, Any], action: str, params: dict[str, Any],
    ) -> Any:
        name = blk["name"]
        if action == "set_input_gain":
            return await self._send_mixer(
                "Mixer.SetInputGain",
                {"Name": name, "Inputs": str(params["inputs"]),
                 "Value": float(params["gain_db"])},
                ramp=float(params.get("ramp", 0.0) or 0.0),
            )
        if action == "set_input_mute":
            return await self._send_mixer(
                "Mixer.SetInputMute",
                {"Name": name, "Inputs": str(params["inputs"]),
                 "Value": self._coerce_outgoing_value(params["value"])},
            )
        if action == "set_output_gain":
            return await self._send_mixer(
                "Mixer.SetOutputGain",
                {"Name": name, "Outputs": str(params["outputs"]),
                 "Value": float(params["gain_db"])},
                ramp=float(params.get("ramp", 0.0) or 0.0),
            )
        if action == "set_output_mute":
            return await self._send_mixer(
                "Mixer.SetOutputMute",
                {"Name": name, "Outputs": str(params["outputs"]),
                 "Value": self._coerce_outgoing_value(params["value"])},
            )
        if action == "set_xpoint_gain":
            return await self._send_mixer(
                "Mixer.SetCrossPointGain",
                {"Name": name,
                 "Inputs": str(params["inputs"]),
                 "Outputs": str(params["outputs"]),
                 "Value": float(params["gain_db"])},
                ramp=float(params.get("ramp", 0.0) or 0.0),
            )
        if action == "set_xpoint_mute":
            return await self._send_mixer(
                "Mixer.SetCrossPointMute",
                {"Name": name,
                 "Inputs": str(params["inputs"]),
                 "Outputs": str(params["outputs"]),
                 "Value": self._coerce_outgoing_value(params["value"])},
            )
        return None

    async def _send_mixer(
        self, method: str, base: dict[str, Any], ramp: float = 0.0,
    ) -> bool:
        if ramp > 0:
            base["Ramp"] = ramp
        await self._send_jsonrpc(method, base)
        return True

    async def _dispatch_loop_player_start(
        self, blk: dict[str, Any], params: dict[str, Any],
    ) -> bool:
        loop_param = params.get("loop", "false")
        loop_bool = (str(loop_param).lower() == "true")
        payload: dict[str, Any] = {
            "Name": blk["name"],
            "Files": [{
                "Name": str(params["file"]),
                "Output": int(params.get("output", 1)),
            }],
            "StartTime": float(params.get("start_time", -1)),
            "Loop": loop_bool,
            "Log": True,
        }
        await self._send_jsonrpc(
            "LoopPlayer.Start", payload, expect_response=False,
        )
        self.set_state(_state_var(blk, "playing"), True)
        self.set_state(_state_var(blk, "last_file"), str(params["file"]))
        return True

    async def _dispatch_loop_player_stop(
        self, blk: dict[str, Any], params: dict[str, Any], verb: str,
    ) -> bool:
        outputs_str = str(params.get("outputs", "1"))
        try:
            outputs = [int(x.strip()) for x in outputs_str.split(",") if x.strip()]
        except ValueError:
            outputs = [1]
        await self._send_jsonrpc(
            f"LoopPlayer.{verb}",
            {"Name": blk["name"], "Outputs": outputs, "Log": True},
            expect_response=False,
        )
        if verb == "Stop":
            self.set_state(_state_var(blk, "playing"), False)
        return True

    async def _dispatch_pa_page_submit(
        self, blk: dict[str, Any], params: dict[str, Any],
    ) -> Any:
        zones_str = str(params.get("zones", ""))
        try:
            zones = [int(x.strip()) for x in zones_str.split(",") if x.strip()]
        except ValueError:
            zones = []
        page_params: dict[str, Any] = {
            "Zones": zones,
            "Priority": int(params.get("priority", 1)),
        }
        preamble = str(params.get("preamble", "") or "")
        if preamble:
            page_params["Preamble"] = preamble
        message = str(params.get("message", "") or "")
        if message:
            page_params["Message"] = message
        result = await self._send_jsonrpc("PA.PageSubmit", page_params)
        if isinstance(result, dict):
            self.set_state(
                _state_var(blk, "page_status"),
                str(result.get("PageID", "submitted")),
            )
        return result

    async def _dispatch_pa_page_cancel(
        self, blk: dict[str, Any], params: dict[str, Any],
    ) -> bool:
        await self._send_jsonrpc(
            "PA.PageCancel",
            {"PageID": str(params["page_id"])},
            expect_response=False,
        )
        self.set_state(_state_var(blk, "page_status"), "cancelled")
        return True

    # ── Polling backstop ──

    async def poll(self) -> None:
        """Re-issue StatusGet as a refresh backstop. Auto-poll on the
        main change group is the source of truth — this only fires
        when poll_interval > 0 and gives the integrator a way to opt
        in to extra paranoia."""
        if not self.transport or not self.transport.connected:
            return
        try:
            await self._do_status_get()
        except (QRCError, TimeoutError, ConnectionError, OSError):
            pass


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
